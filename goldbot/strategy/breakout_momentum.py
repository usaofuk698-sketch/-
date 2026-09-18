"""Momentum-breakout scalper for XAUUSD on the 5-minute chart.

Built around one piece of arithmetic. With a 0.29 USD/oz round-trip cost, the
hit rate a system needs merely to break even is ``(stop + cost) / (target +
stop)``:

    target 1.00, stop 1.00  ->  64.5%   (the intuitive "take a quick dollar")
    target 3.00, stop 1.00  ->  32.2%   (this strategy)

Both keep the tight stop. Only the exit differs, and it moves the required hit
rate by more than thirty points. On a spread-heavy instrument the profitable
direction is *larger* targets, not smaller ones -- the opposite of what quick
scalping suggests.

So: enter fast on a decisive impulse, risk little, and aim far. Most breakouts
fail, which is why the hit rate is designed to be low. The few that work have to
be allowed to run the full distance, and everything below is arranged to protect
that.

Why the entry conditions are what they are
------------------------------------------
* **Squeeze first.** The range must be compressed relative to ATR before the
  break. A break out of an already-wide range has no stored energy behind it
  and is the most common false signal.
* **Decisive bar.** The breakout bar's own range must be a meaningful fraction
  of ATR. A level drifted through on a doji is not an impulse.
* **Closed beyond, not merely touched.** The bar must close past the level.
  Intrabar pokes through a level are exactly what stop-hunting produces.
* **Closed near its extreme.** A breakout bar that closes back in the middle of
  its range has already been rejected.

The break-even trap
-------------------
:attr:`BreakoutMomentumParams.breakeven_at_r` defaults to ``None``, and that is
deliberate. Moving the stop to break-even early feels prudent and is fatal here:
at a ~32% hit rate the maths only works if winners reach the full target, and an
early break-even stop converts a large share of them into scratches. The time
stop is the safety valve instead -- a breakout that has not moved within a few
bars has failed, and failing fast is cheaper than being stopped at break-even
after giving back a real move.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..backtest.types import Position, Side, Signal
from .base import Strategy, StrategyParams


@dataclass
class BreakoutMomentumParams(StrategyParams):
    # -- the range being broken
    range_lookback: int = 12          # bars forming the range, excluding this one
    squeeze_max_atr: float = 4.00     # range width must be <= this x ATR
    #   2.2 is a hard squeeze and yields ~1.3 trades/day; 4.0 yields ~7.1 at
    #   the same measured expectancy, so the tighter setting buys nothing but
    #   a smaller sample. Above ~6 the filter stops doing anything at all.
    break_margin_atr: float = 0.05    # close must clear the level by this much
    # -- impulse quality
    atr_period: int = 14
    min_bar_range_atr: float = 0.60   # the breakout bar must be decisive
    close_position_min: float = 0.60  # and close near its own extreme
    # -- regime
    regime_lookback: int = 288
    atr_min_mult: float = 0.55
    atr_max_mult: float = 3.00
    adx_period: int = 14
    adx_min: float = 0.0              # 0 = off; breakouts precede ADX rising
    # -- trend alignment (optional)
    trend_ema_period: int = 100
    require_trend_alignment: bool = False
    # -- stop and target: tight risk, distant reward
    stop_atr_buffer: float = 0.20     # behind the broken level
    min_stop_atr_mult: float = 1.00   # floor on stop distance
    reward_risk: float = 4.00         # the whole point
    min_target_spread_ratio: float = 8.0
    # -- management
    breakeven_at_r: float | None = None   # see "The break-even trap" above
    breakeven_offset_atr: float = 0.05
    trail_at_r: float | None = None


class BreakoutMomentum(Strategy):
    name = "breakout_momentum"

    _FEATURE_COLS = (
        "open", "high", "low", "close", "atr", "adx", "atr_med",
        "range_high", "range_low", "range_width", "close_pos", "bar_range",
        "trend_ema",
    )

    def __init__(self, params: BreakoutMomentumParams | None = None):
        self.params = params or BreakoutMomentumParams()
        self._a: dict[str, np.ndarray] = {}
        self._n: int = -1

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p.regime_lookback, p.trend_ema_period, p.range_lookback) + 50

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

        out["atr"] = ind.atr(h, l, c, p.atr_period)
        out["adx"], _, _ = ind.adx(h, l, c, p.adx_period)
        out["atr_med"] = (
            out["atr"].rolling(p.regime_lookback, min_periods=p.regime_lookback).median()
        )

        # The range is formed by the bars BEFORE this one. Without the shift the
        # current bar would be part of the level it is supposed to break, and
        # the condition could never trigger honestly.
        out["range_high"] = ind.rolling_high(h, p.range_lookback).shift(1)
        out["range_low"] = ind.rolling_low(l, p.range_lookback).shift(1)
        out["range_width"] = out["range_high"] - out["range_low"]

        rng = (h - l).replace(0.0, np.nan)
        out["close_pos"] = (c - l) / rng
        out["bar_range"] = h - l
        out["trend_ema"] = ind.ema(c, p.trend_ema_period)
        return out

    # ---------------------------------------------------------------- entry
    def entry(self, i: int, feat: pd.DataFrame) -> Signal | None:
        p = self.params
        self._bind(feat)
        a = self._a

        atr_v, med, adx_v = a["atr"][i], a["atr_med"][i], a["adx"][i]
        close, high_v, low_v = a["close"][i], a["high"][i], a["low"][i]
        rh, rl, width = a["range_high"][i], a["range_low"][i], a["range_width"][i]
        close_pos, bar_range = a["close_pos"][i], a["bar_range"][i]
        trend = a["trend_ema"][i]

        if not (
            np.isfinite(atr_v) and np.isfinite(med) and np.isfinite(rh)
            and np.isfinite(rl) and np.isfinite(close_pos) and np.isfinite(trend)
        ):
            return None
        if atr_v <= 0 or med <= 0:
            return None

        if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
            return None
        if p.adx_min > 0 and (not np.isfinite(adx_v) or adx_v < p.adx_min):
            return None

        # Energy has to be stored before it can be released.
        if width > p.squeeze_max_atr * atr_v:
            return None

        # The impulse itself must be decisive, not a drift through the level.
        if bar_range < p.min_bar_range_atr * atr_v:
            return None

        margin = p.break_margin_atr * atr_v
        broke_up = close > rh + margin
        broke_down = close < rl - margin

        up_ok = broke_up and close_pos >= p.close_position_min
        down_ok = broke_down and (1.0 - close_pos) >= p.close_position_min
        if p.require_trend_alignment:
            up_ok = up_ok and close > trend
            down_ok = down_ok and close < trend
        if up_ok == down_ok:
            return None

        if up_ok:
            # Stop sits back inside the broken range: if price returns there the
            # breakout is void, and that is the cheapest possible place to be wrong.
            sl = min(rh - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
            if sl >= close:
                return None
            tp = close + p.reward_risk * (close - sl)
            side = Side.LONG
        else:
            sl = max(rl + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
            if sl <= close:
                return None
            tp = close - p.reward_risk * (sl - close)
            side = Side.SHORT

        return Signal(
            side=side,
            stop_loss=float(sl),
            take_profit=float(tp),
            reason="range_break_impulse",
            meta={
                "atr": float(atr_v),
                "range_width_atr": float(width / atr_v),
                "bar_range_atr": float(bar_range / atr_v),
                "ref_close": float(close),
            },
        )

    # --------------------------------------------------------------- manage
    def manage(self, i: int, feat: pd.DataFrame, pos: Position) -> tuple[float, float | None]:
        """Deliberately does almost nothing.

        At this hit rate the winners must be allowed to reach the target. Any
        stop management that activates before then trades a small number of
        large wins for a large number of scratches, which inverts the edge the
        strategy is built on. Break-even is available but off by default.
        """
        p = self.params
        if p.breakeven_at_r is None:
            return pos.stop_loss, pos.take_profit

        self._bind(feat)
        atr_v = self._a["atr"][i]
        atr_v = float(atr_v) if np.isfinite(atr_v) else 0.0
        close = float(self._a["close"][i])
        risk = pos.initial_risk
        if risk <= 0 or atr_v <= 0:
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
