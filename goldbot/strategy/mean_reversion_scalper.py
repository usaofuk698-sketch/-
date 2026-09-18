"""Mean-reversion scalper for XAUUSD on the 5-minute chart.

This is the deliberate complement of :mod:`goldbot.strategy.trend_pullback`.
That system waits for a trend and refuses to trade chop, which on M5 gold means
standing aside roughly 99% of the time and taking about one trade a day. This
one does the opposite: it trades *into* the chop, because on this instrument and
timeframe range-bound behaviour is the common state, and that is where the
frequency lives.

The idea
--------
Price on M5 gold oscillates around a short moving average. When it stretches an
unusual distance away from that average and momentum is exhausted, the next move
is more often back toward the average than further away. The strategy fades the
stretch and takes profit near the mean.

Four conditions, all required:

1. **Not a strong trend.** ADX below a ceiling. This is the single most
   important filter in a mean-reversion system and the one most often left out:
   fading a genuine trend is how this style of strategy dies. A strong trend
   keeps stretching, and each "extreme" is followed by a more extreme one.
2. **Stretched from the mean.** Distance from the EMA, measured in ATR rather
   than in dollars, so the threshold means the same thing in quiet and volatile
   markets.
3. **Momentum exhausted.** A fast RSI at an extreme.
4. **Reversal has begun.** The bar closes back in the direction of the mean.
   Entering while price is still falling is catching a knife; waiting for the
   turn costs a little of the move and removes most of the worst entries.

Cost discipline
---------------
A scalper lives or dies on the spread, because it pays it on every one of many
trades. Where the trend system risks ~3.00 USD/oz per trade, this one risks a
fraction of that, so the same 0.26 spread is a far larger share of each trade's
risk. :attr:`MeanReversionScalperParams.min_target_spread_ratio` refuses any
setup whose target is not a sufficient multiple of the current spread, which is
what stops the strategy from trading itself to death in costs.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..backtest.types import Position, Side, Signal
from .base import Strategy, StrategyParams


@dataclass
class MeanReversionScalperParams(StrategyParams):
    # -- the mean
    ema_period: int = 20
    # -- regime guard: above this ADX the market is trending, do not fade it
    adx_period: int = 14
    adx_max: float = 50.0
    # -- volatility reference
    atr_period: int = 14
    regime_lookback: int = 288
    atr_min_mult: float = 0.55        # skip dead tape the spread would eat
    atr_max_mult: float = 3.00        # skip post-news chaos
    # -- stretch trigger
    stretch_atr: float = 0.60         # distance from the EMA, in ATR
    rsi_period: int = 7               # fast RSI: this is a scalper
    rsi_oversold: float = 38.0
    rsi_overbought: float = 62.0
    # -- stop and target
    swing_lookback: int = 6
    stop_atr_buffer: float = 0.25
    min_stop_atr_mult: float = 1.00
    reward_risk: float = 1.20         # mean reversion: high hit rate, modest R
    min_target_spread_ratio: float = 5.0   # target must be >= N x the spread
    # -- management
    breakeven_at_r: float | None = 0.8
    breakeven_offset_atr: float = 0.05
    trail_at_r: float | None = None   # scalps are too short for a trail


class MeanReversionScalper(Strategy):
    name = "mean_reversion_scalper"

    _FEATURE_COLS = (
        "open", "high", "low", "close", "ema", "atr", "adx", "rsi",
        "atr_med", "swing_low", "swing_high",
    )

    def __init__(self, params: MeanReversionScalperParams | None = None):
        self.params = params or MeanReversionScalperParams()
        self._a: dict[str, np.ndarray] = {}
        self._n: int = -1

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p.regime_lookback, p.ema_period, p.adx_period) + 50

    def _bind(self, feat: pd.DataFrame) -> None:
        if self._n == len(feat) and self._a:
            return
        self._a = {c: feat[c].to_numpy(dtype=float) for c in self._FEATURE_COLS}
        self._n = len(feat)

    # ------------------------------------------------------------- features
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        h, l, c = out["high"], out["low"], out["close"]

        out["ema"] = ind.ema(c, p.ema_period)
        out["atr"] = ind.atr(h, l, c, p.atr_period)
        out["adx"], _, _ = ind.adx(h, l, c, p.adx_period)
        out["rsi"] = ind.rsi(c, p.rsi_period)
        out["atr_med"] = (
            out["atr"].rolling(p.regime_lookback, min_periods=p.regime_lookback).median()
        )
        out["swing_low"] = ind.rolling_low(l, p.swing_lookback)
        out["swing_high"] = ind.rolling_high(h, p.swing_lookback)

        self._a = {col: out[col].to_numpy(dtype=float) for col in self._FEATURE_COLS}
        self._n = len(out)
        return out

    # ---------------------------------------------------------------- entry
    def entry(self, i: int, feat: pd.DataFrame) -> Signal | None:
        p = self.params
        self._bind(feat)
        a = self._a

        atr_v, med = a["atr"][i], a["atr_med"][i]
        adx_v, rsi_v = a["adx"][i], a["rsi"][i]
        ema_v, close, open_ = a["ema"][i], a["close"][i], a["open"][i]
        swing_lo, swing_hi = a["swing_low"][i], a["swing_high"][i]

        if not (
            np.isfinite(atr_v) and np.isfinite(med) and np.isfinite(adx_v)
            and np.isfinite(rsi_v) and np.isfinite(ema_v)
            and np.isfinite(swing_lo) and np.isfinite(swing_hi)
        ):
            return None
        if atr_v <= 0 or med <= 0:
            return None

        # Volatility regime: too quiet and the spread dominates, too wild and
        # the mean stops being a magnet at all.
        if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
            return None

        # The guard that keeps a mean-reversion system alive. In a real trend
        # every extreme is followed by a further extreme, and fading it loses
        # repeatedly and in the same direction.
        if adx_v >= p.adx_max:
            return None

        stretch = (close - ema_v) / atr_v

        long_ok = (
            stretch <= -p.stretch_atr
            and rsi_v <= p.rsi_oversold
            and close > open_          # the turn has started
        )
        short_ok = (
            stretch >= p.stretch_atr
            and rsi_v >= p.rsi_overbought
            and close < open_
        )
        if long_ok == short_ok:
            return None

        if long_ok:
            sl = min(swing_lo - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
            if sl >= close:
                return None
            tp = close + p.reward_risk * (close - sl)
            side = Side.LONG
        else:
            sl = max(swing_hi + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
            if sl <= close:
                return None
            tp = close - p.reward_risk * (sl - close)
            side = Side.SHORT

        return Signal(
            side=side,
            stop_loss=float(sl),
            take_profit=float(tp),
            reason="stretch_fade",
            meta={
                "atr": float(atr_v),
                "adx": float(adx_v),
                "stretch": float(stretch),
                "ref_close": float(close),
                # Carried so the engine can reject the setup when the target is
                # not worth the spread; see min_target_spread_ratio.
                "target_distance": float(abs(tp - close)),
            },
        )

    # --------------------------------------------------------------- manage
    def manage(self, i: int, feat: pd.DataFrame, pos: Position) -> tuple[float, float | None]:
        p = self.params
        self._bind(feat)
        atr_v = self._a["atr"][i]
        atr_v = float(atr_v) if np.isfinite(atr_v) else 0.0
        close = float(self._a["close"][i])
        risk = pos.initial_risk
        if risk <= 0 or atr_v <= 0 or p.breakeven_at_r is None:
            return pos.stop_loss, pos.take_profit

        sl = pos.stop_loss
        if pos.side is Side.LONG:
            if (close - pos.entry_price) / risk >= p.breakeven_at_r:
                sl = max(sl, pos.entry_price + p.breakeven_offset_atr * atr_v)
            sl = min(sl, close)
        else:
            if (pos.entry_price - close) / risk >= p.breakeven_at_r:
                sl = min(sl, pos.entry_price - p.breakeven_offset_atr * atr_v)
            sl = max(sl, close)
        return sl, pos.take_profit
