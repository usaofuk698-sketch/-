"""Breakout-then-retest continuation scalper for XAUUSD on the 5-minute chart.

This is a third temperament, distinct from the other two breakout-adjacent
strategies in this repository:

* :mod:`breakout_momentum` (XauBreak) enters *immediately* when a level breaks,
  betting the break is real. Most are not, so it survives on a low hit rate and
  a large reward-to-risk.
* :mod:`volume_profile_wyckoff` (XauWyck) enters *against* a break, betting it
  is a trap (a spring or upthrust) that snaps back to the point of control.
* This strategy enters *after* a break, once price has come back to prove the
  broken level and held. Former resistance acting as support (or the mirror)
  is the classic technical-analysis pattern usually called "break and retest".

The three cannot agree with each other on the same bar by construction: one
trades with the break, one trades against it, and this one waits to see who is
right before trading at all. That patience is the entire edge and the entire
cost -- a lot of valid breaks never come back to retest, and this strategy
simply never takes them.

Two stages, both required
--------------------------
1. **Breakout.** A decisive bar (real range, not a doji) closes beyond a recent
   range by a clear margin. This arms a *pending* level -- the broken range
   edge -- and starts a clock.
2. **Retest.** Within a bounded number of bars, price returns close enough to
   the broken level to count as a genuine test, without closing back through
   it, and the bar that does so closes strongly back in the breakout
   direction. That bar is the entry.

If price closes back through the level before a retest arrives, the pending
breakout is void -- it was not real, or it already failed, and there is
nothing left here to trade. If the clock runs out first, the level is
considered stale: a level nobody has come back to test in that time is not one
the market is still trading against.

Why this needs a loop, not a rolling window
--------------------------------------------
Every other strategy in this package computes its features with vectorised
pandas operations because each bar's condition only looks at a fixed trailing
window. This one cannot: "the nearest unfailed breakout, and whether this bar
is its first valid retest" is state that persists across an *unknown* number
of bars, carried forward or torn up by what happens on each one in between.
That is a sequential scan, not a window, so :meth:`prepare` runs one explicit
pass over the bars. The scan is still causal -- the state recorded for bar
``i`` reflects only bars ``0..i-1``; bar ``i``'s own breakout (if it has one)
is folded in afterwards and only affects bar ``i+1`` onward. A bar can
therefore never retest the very breakout it just made.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..backtest.types import Position, Side, Signal
from .base import Strategy, StrategyParams


@dataclass
class BreakoutRetestParams(StrategyParams):
    # -- the level being broken
    range_lookback: int = 20
    break_margin_atr: float = 0.10
    min_bar_range_atr: float = 0.50   # the breakout bar must be decisive
    # -- regime
    atr_period: int = 14
    regime_lookback: int = 288
    atr_min_mult: float = 0.55        # skip dead tape the spread would eat
    atr_max_mult: float = 3.00        # skip post-news chaos
    adx_period: int = 14
    adx_min: float = 15.0             # a continuation trade wants some trend behind it
    # -- the retest
    retest_max_bars: int = 12         # a level nobody retests in this long is stale
    retest_tolerance_atr: float = 0.25   # how close price must come back, in ATR
    retest_close_position_min: float = 0.55   # the retest bar must reject the level cleanly
    # -- stop and target
    stop_atr_buffer: float = 0.20     # beyond the retest bar's own extreme
    min_stop_atr_mult: float = 1.00
    reward_risk: float = 2.00
    min_target_spread_ratio: float = 6.0
    # -- management
    breakeven_at_r: float | None = 1.0
    breakeven_offset_atr: float = 0.05
    trail_at_r: float | None = None


class BreakoutRetest(Strategy):
    name = "breakout_retest"

    _FEATURE_COLS = (
        "open", "high", "low", "close", "atr", "adx", "atr_med",
        "close_pos", "bar_range", "retest_level", "retest_side",
    )

    def __init__(self, params: BreakoutRetestParams | None = None):
        self.params = params or BreakoutRetestParams()
        self._a: dict[str, np.ndarray] = {}
        self._n: int = -1

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p.regime_lookback, p.range_lookback) + p.retest_max_bars + 50

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

        # The range excludes the bar being judged, exactly like XauBreak: a bar
        # cannot break a level that is partly made of itself.
        range_high = ind.rolling_high(h, p.range_lookback).shift(1)
        range_low = ind.rolling_low(l, p.range_lookback).shift(1)

        rng = (h - l).replace(0.0, np.nan)
        out["close_pos"] = (c - l) / rng
        out["bar_range"] = h - l

        atr_a = out["atr"].to_numpy(float)
        close_a = c.to_numpy(float)
        rh_a = range_high.to_numpy(float)
        rl_a = range_low.to_numpy(float)
        bar_range_a = out["bar_range"].to_numpy(float)
        close_pos_a = out["close_pos"].to_numpy(float)

        n = len(out)
        level = np.full(n, np.nan)
        side = np.zeros(n, dtype=float)

        pending_level = float("nan")
        pending_side = 0
        pending_age = 0

        for i in range(n):
            # State as carried INTO this bar -- what entry() at i is allowed to
            # see. Recorded before anything below reacts to bar i itself.
            level[i] = pending_level
            side[i] = float(pending_side)

            if pending_side != 0:
                pending_age += 1
                closed_through = (
                    np.isfinite(close_a[i])
                    and (
                        (pending_side == 1 and close_a[i] < pending_level)
                        or (pending_side == -1 and close_a[i] > pending_level)
                    )
                )
                if pending_age > p.retest_max_bars or closed_through:
                    pending_side = 0
                    pending_level = float("nan")

            a_v = atr_a[i]
            rh, rl = rh_a[i], rl_a[i]
            if np.isfinite(a_v) and a_v > 0 and np.isfinite(rh) and np.isfinite(rl):
                decisive = bar_range_a[i] >= p.min_bar_range_atr * a_v
                margin = p.break_margin_atr * a_v
                broke_up = decisive and close_a[i] > rh + margin and close_pos_a[i] >= 0.5
                broke_down = decisive and close_a[i] < rl - margin and (1.0 - close_pos_a[i]) >= 0.5
                if broke_up and not broke_down:
                    pending_side, pending_level, pending_age = 1, rh, 0
                elif broke_down and not broke_up:
                    pending_side, pending_level, pending_age = -1, rl, 0

        out["retest_level"] = level
        out["retest_side"] = side
        self._a = {col: out[col].to_numpy(dtype=float) for col in self._FEATURE_COLS}
        self._n = n
        return out

    # ---------------------------------------------------------------- entry
    def entry(self, i: int, feat: pd.DataFrame) -> Signal | None:
        p = self.params
        self._bind(feat)
        a = self._a

        level, side_v = a["retest_level"][i], a["retest_side"][i]
        if side_v == 0.0 or not np.isfinite(level):
            return None

        atr_v, med, adx_v = a["atr"][i], a["atr_med"][i], a["adx"][i]
        close, high_v, low_v = a["close"][i], a["high"][i], a["low"][i]
        close_pos = a["close_pos"][i]

        if not (np.isfinite(atr_v) and np.isfinite(med)):
            return None
        if atr_v <= 0 or med <= 0:
            return None
        if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
            return None
        if p.adx_min > 0.0 and (not np.isfinite(adx_v) or adx_v < p.adx_min):
            return None

        tol = p.retest_tolerance_atr * atr_v

        if side_v > 0:
            # Former resistance, now expected to hold as support.
            retest_ok = (
                low_v <= level + tol
                and close > level
                and close_pos >= p.retest_close_position_min
            )
            if not retest_ok:
                return None
            sl = min(low_v - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
            if sl >= close:
                return None
            tp = close + p.reward_risk * (close - sl)
            side = Side.LONG
        else:
            # Former support, now expected to hold as resistance.
            retest_ok = (
                high_v >= level - tol
                and close < level
                and (1.0 - close_pos) >= p.retest_close_position_min
            )
            if not retest_ok:
                return None
            sl = max(high_v + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
            if sl <= close:
                return None
            tp = close - p.reward_risk * (sl - close)
            side = Side.SHORT

        return Signal(
            side=side,
            stop_loss=float(sl),
            take_profit=float(tp),
            reason="breakout_retest",
            meta={
                "atr": float(atr_v),
                "level": float(level),
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
