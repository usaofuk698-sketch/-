"""Session trend-pullback strategy for XAUUSD on the 5-minute chart.

Design rationale
----------------
Gold on M5 is dominated by noise. Any edge has to come from *declining to
trade* most of the time, so the strategy stacks four independent filters and
only then looks for a trigger:

1. **Direction** -- a fast/slow EMA cross plus price on the correct side of a
   long EMA. Trades the prevailing move rather than predicting a turn.
2. **Trend strength** -- ADX above a floor. This is the chop filter; gold spends
   most of its session ranging, and a pullback system bleeds to death there.
3. **Volatility regime** -- ATR compared to its own rolling median, with a floor
   *and a ceiling*. The floor skips dead tape where the spread eats the range;
   the ceiling skips post-news expansion where stop distance explodes and fills
   go unreliable.
4. **Pullback then resumption** -- RSI must have dipped into pullback territory
   within a lookback window and then recovered out of it, with the bar closing
   beyond the prior bar's extreme. Entering on the resumption rather than the
   dip avoids catching a trend that is actually ending.

Stops sit beyond recent market structure (a swing extreme) with an ATR buffer,
never at a fixed distance -- a fixed stop is arbitrary and gets swept. Targets
are a multiple of the realised stop distance, so the reward:risk ratio is a
property of the design rather than of the day's volatility.

None of this makes the strategy profitable by construction. It makes it
*testable*: every filter is a named parameter that walk-forward analysis can
find worthless.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..backtest.types import Position, Side, Signal
from .base import Strategy, StrategyParams


@dataclass
class TrendPullbackParams(StrategyParams):
    # -- direction
    ema_fast: int = 21
    ema_slow: int = 55
    ema_trend: int = 200
    # -- filters
    adx_period: int = 14
    adx_min: float = 22.0
    atr_period: int = 14
    regime_lookback: int = 288          # one 24h session of M5 bars
    atr_min_mult: float = 0.80          # vs rolling median ATR
    atr_max_mult: float = 2.50
    # -- pullback trigger
    rsi_period: int = 14
    rsi_pullback_long: float = 45.0
    rsi_pullback_short: float = 55.0
    pullback_lookback: int = 8
    # -- stop / target placement
    swing_lookback: int = 12
    stop_atr_buffer: float = 0.35       # padding beyond the swing extreme
    min_stop_atr_mult: float = 1.00     # floor on stop distance, in ATR
    reward_risk: float = 1.8
    # -- in-trade management
    breakeven_at_r: float | None = 1.0
    breakeven_offset_atr: float = 0.10  # lock in enough to cover costs
    trail_at_r: float | None = 1.5
    trail_atr_mult: float = 1.5


class TrendPullback(Strategy):
    name = "trend_pullback"

    #: Feature columns lifted into numpy for the per-bar hot path. Pandas
    #: row access (``.iloc[i]``) costs ~100us and the engine does it once per
    #: bar; over a few hundred thousand bars that dominates the whole backtest.
    _FEATURE_COLS = (
        "open", "high", "low", "close", "ema_fast", "ema_slow", "ema_trend",
        "atr", "adx", "rsi", "atr_med", "swing_low", "swing_high",
        "prev_high", "prev_low", "rsi_min", "rsi_max",
    )

    def __init__(self, params: TrendPullbackParams | None = None):
        self.params = params or TrendPullbackParams()
        self._a: dict[str, np.ndarray] = {}
        self._n: int = -1

    def _bind(self, feat: pd.DataFrame) -> None:
        """Refresh the numpy cache if ``feat`` is not what we last prepared."""
        if self._n == len(feat) and self._a:
            return
        self._a = {c: feat[c].to_numpy(dtype=float) for c in self._FEATURE_COLS}
        self._n = len(feat)

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p.ema_trend, p.regime_lookback) + 50

    # ------------------------------------------------------------- features
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        o, h, l, c = out["open"], out["high"], out["low"], out["close"]

        out["ema_fast"] = ind.ema(c, p.ema_fast)
        out["ema_slow"] = ind.ema(c, p.ema_slow)
        out["ema_trend"] = ind.ema(c, p.ema_trend)
        out["atr"] = ind.atr(h, l, c, p.atr_period)
        out["adx"], _, _ = ind.adx(h, l, c, p.adx_period)
        out["rsi"] = ind.rsi(c, p.rsi_period)

        # Regime reference: ATR against its own recent median, so the filter
        # adapts to gold's secular volatility instead of a hard-coded level.
        out["atr_med"] = (
            out["atr"].rolling(p.regime_lookback, min_periods=p.regime_lookback).median()
        )
        out["swing_low"] = ind.rolling_low(l, p.swing_lookback)
        out["swing_high"] = ind.rolling_high(h, p.swing_lookback)
        out["prev_high"] = h.shift(1)
        out["prev_low"] = l.shift(1)
        out["rsi_min"] = out["rsi"].rolling(p.pullback_lookback, min_periods=p.pullback_lookback).min()
        out["rsi_max"] = out["rsi"].rolling(p.pullback_lookback, min_periods=p.pullback_lookback).max()
        out["bar_up"] = c > o
        out["bar_dn"] = c < o

        self._a = {col: out[col].to_numpy(dtype=float) for col in self._FEATURE_COLS}
        self._n = len(out)
        return out

    # ---------------------------------------------------------------- entry
    def entry(self, i: int, feat: pd.DataFrame) -> Signal | None:
        p = self.params
        self._bind(feat)
        a = self._a

        atr_v = a["atr"][i]
        med = a["atr_med"][i]
        adx_v = a["adx"][i]
        rsi_v = a["rsi"][i]
        close = a["close"][i]
        ema_f, ema_s, ema_t = a["ema_fast"][i], a["ema_slow"][i], a["ema_trend"][i]
        swing_lo, swing_hi = a["swing_low"][i], a["swing_high"][i]
        prev_hi, prev_lo = a["prev_high"][i], a["prev_low"][i]
        rsi_lo, rsi_hi = a["rsi_min"][i], a["rsi_max"][i]
        open_ = a["open"][i]

        # Warm-up and feed gaps surface as NaN; refuse to trade rather than let
        # a comparison against NaN silently evaluate to False and look like a
        # deliberate "no trade".
        if not (
            np.isfinite(atr_v) and np.isfinite(med) and np.isfinite(adx_v)
            and np.isfinite(rsi_v) and np.isfinite(ema_f) and np.isfinite(ema_s)
            and np.isfinite(ema_t) and np.isfinite(swing_lo) and np.isfinite(swing_hi)
            and np.isfinite(prev_hi) and np.isfinite(prev_lo)
            and np.isfinite(rsi_lo) and np.isfinite(rsi_hi)
        ):
            return None
        if atr_v <= 0 or med <= 0:
            return None

        # Volatility regime gate -- shared by both directions.
        if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
            return None
        if adx_v < p.adx_min:
            return None

        long_ok = (
            ema_f > ema_s
            and close > ema_t
            and rsi_lo <= p.rsi_pullback_long
            and rsi_v > p.rsi_pullback_long
            and close > prev_hi
            and close > open_
        )
        short_ok = (
            ema_f < ema_s
            and close < ema_t
            and rsi_hi >= p.rsi_pullback_short
            and rsi_v < p.rsi_pullback_short
            and close < prev_lo
            and close < open_
        )
        if long_ok == short_ok:  # neither, or (impossible) both
            return None

        if long_ok:
            sl = min(swing_lo - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
            if sl >= close:
                return None
            return Signal(
                side=Side.LONG,
                stop_loss=float(sl),
                take_profit=float(close + p.reward_risk * (close - sl)),
                reason="trend_up_pullback_resume",
                meta={"atr": float(atr_v), "adx": float(adx_v), "ref_close": float(close)},
            )

        sl = max(swing_hi + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
        if sl <= close:
            return None
        return Signal(
            side=Side.SHORT,
            stop_loss=float(sl),
            take_profit=float(close - p.reward_risk * (sl - close)),
            reason="trend_dn_pullback_resume",
            meta={"atr": float(atr_v), "adx": float(adx_v), "ref_close": float(close)},
        )

    # --------------------------------------------------------------- manage
    def manage(self, i: int, feat: pd.DataFrame, pos: Position) -> tuple[float, float | None]:
        """Break-even then ATR trail. Only ever tightens, never loosens."""
        p = self.params
        self._bind(feat)
        atr_v = self._a["atr"][i]
        atr_v = float(atr_v) if np.isfinite(atr_v) else 0.0
        close = float(self._a["close"][i])
        risk = pos.initial_risk
        if risk <= 0 or atr_v <= 0:
            return pos.stop_loss, pos.take_profit

        sl = pos.stop_loss
        if pos.side is Side.LONG:
            gained_r = (close - pos.entry_price) / risk
            if p.breakeven_at_r is not None and gained_r >= p.breakeven_at_r:
                sl = max(sl, pos.entry_price + p.breakeven_offset_atr * atr_v)
            if p.trail_at_r is not None and gained_r >= p.trail_at_r:
                sl = max(sl, close - p.trail_atr_mult * atr_v)
            sl = min(sl, close)  # never place a stop through the current price
        else:
            gained_r = (pos.entry_price - close) / risk
            if p.breakeven_at_r is not None and gained_r >= p.breakeven_at_r:
                sl = min(sl, pos.entry_price - p.breakeven_offset_atr * atr_v)
            if p.trail_at_r is not None and gained_r >= p.trail_at_r:
                sl = min(sl, close + p.trail_atr_mult * atr_v)
            sl = max(sl, close)
        return sl, pos.take_profit
