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

Quality scales the size of the ONE trade, never the count of trades
---------------------------------------------------------------------
A setup that clears every filter by a wide margin is not the same as one that
barely scrapes past them, and it is tempting to reward the strong one with an
extra position. That temptation is refused on purpose: this engine enforces
one position at a time across every strategy in the repository, and stacking
a second, separately-sized position on the same signal is not something this
backtester can honestly price or this validation suite can honestly test --
"probably fine" is not good enough for money. What the same conviction CAN
buy, safely and inside the existing one-trade rule, is a larger size on that
one trade and a farther target: :attr:`quality_max_risk_mult` scales the risk
fraction (via ``Signal.meta["risk_multiplier"]``, read by
:meth:`goldbot.risk.RiskManager.size`) and the reward-to-risk floor scales
between :attr:`quality_min_reward_risk` and :attr:`quality_max_reward_risk`.
Both are driven by a single 0-1 quality score built from four independent,
already-computed signals -- breakout decisiveness, trend strength, retest
precision, and rejection strength -- averaged so no one of them can push the
score near 1.0 by itself.

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
    # A local M5 breakout can still be fighting the broader trend -- ADX
    # measures how strong the LOCAL move is, not which way the bigger picture
    # points. Approximated with a long EMA on this same timeframe rather than
    # by resampling to H1, matching the same technique already used by
    # mean_reversion_scalper.py's htf_ema: one series, identical arithmetic in
    # Python and MQL5, no bar-alignment question to get wrong. A retest is
    # only taken when price sits on the side of this EMA that agrees with the
    # trade direction. This is a standard, established technique, not a
    # tuned-on-one-sample guess -- but whether it actually improves THIS
    # strategy still needs the same real-data proof as everything else here.
    htf_ema_period: int = 100
    require_htf_alignment: bool = True
    # -- the retest
    retest_max_bars: int = 12         # a level nobody retests in this long is stale
    # Both tightened from an initial 0.25/0.55 after a live Strategy Tester run
    # (2026.09.01-17, XAUUSDm, 100% real ticks) showed the dominant failure
    # mode: 23 of 36 trades hit a real stop, many within 2-15 minutes (1-3
    # M5 bars) of entry. A retest bar that barely clears close_position_min
    # is not a rejection, it is noise that happened to close on the right
    # side -- and tolerance_atr=0.25 accepted "in the neighbourhood" of the
    # level as a retest instead of requiring it be genuinely close. Neither
    # number is proven optimal; they are a direct, evidence-driven response
    # to a specific observed failure mode, not a blind guess.
    retest_tolerance_atr: float = 0.15   # how close price must come back, in ATR
    retest_close_position_min: float = 0.68   # the retest bar must reject the level cleanly
    # A screenshot from the same run that motivated the two settings above
    # showed a second, distinct failure: a trade entered 35 minutes (7 bars)
    # after the prior one closed, buying right into the top of a fast rally
    # that had already run far past the level being "retested" -- a classic
    # chase, and the run's second-worst loss. Reusing retest_max_bars as the
    # cooldown is deliberate, not a new arbitrary number: it says a fresh
    # setup needs the same minimum breathing room a retest is given to form,
    # rather than firing again the moment the last trade's slot frees up.
    cooldown_bars: int = 12           # bars after ANY entry before the next one is allowed
    # -- stop and target
    stop_atr_buffer: float = 0.20     # beyond the retest bar's own extreme
    min_stop_atr_mult: float = 1.00
    min_target_spread_ratio: float = 6.0
    # -- quality score (0-1): how much better than bare-minimum this setup is,
    # averaged from four independent signals so no single one can push the
    # score near 1.0 alone. Scales the target and the size below -- never the
    # entry decision itself, which is still a strict pass/fail on the filters
    # above. See the class docstring for why this is a size lever, not an
    # extra trade.
    quality_min_reward_risk: float = 1.50   # reward:risk at quality score 0
    quality_max_reward_risk: float = 3.00   # reward:risk at quality score 1
    use_measured_move_target: bool = True   # project the broken range's own height
    quality_max_risk_mult: float = 1.50     # position-size multiplier at quality score 1
    # -- management
    breakeven_at_r: float | None = 1.0
    breakeven_offset_atr: float = 0.05
    # A trade that reaches breakeven and later reverses all the way to the
    # (now-raised) stop is a scratch, not a loss -- but one that reaches 1.5R
    # or more and gives it ALL back is exactly the "won, then lost" complaint
    # this trail exists to stop: past trail_at_r, the stop follows price at a
    # fixed ATR distance instead of sitting still at breakeven. It only ever
    # tightens (never loosens), same rule as breakeven.
    trail_at_r: float | None = 1.5
    trail_atr_mult: float = 1.20
    # -- optional second entry path: immediate breakout, no retest wait
    # The retest requirement is the entire strategy above -- and also its
    # entire cost: some fraction of decisive breakouts simply never come back
    # to retest before running away, and those are pure missed trades, not
    # avoided losses. This path borrows XauBreak's philosophy (enter on the
    # break itself) as a SEPARATE, additive trigger rather than changing what
    # counts as a retest: it fires only on a breakout bar stronger than the
    # bare arming minimum (min_bar_range_atr), still gated by the same
    # regime/ADX/HTF-alignment filters and the same cooldown as the retest
    # path, so it cannot fire more often than those filters already allow and
    # cannot overlap a live retest-based position (one position at a time,
    # enforced by the engine). It has no retest bar to size precision/
    # rejection from, so its quality score is the breakout strength alone and
    # its stop/target are plain ATR multiples rather than the retest-anchored
    # ones above.
    # OFF by default: unlike require_htf_alignment (an established technique
    # applied here for the first time), this is a brand-new trigger with zero
    # real Strategy Tester evidence behind it yet. Turning it on trades some
    # of the "waited for confirmation, missed the move" cost for a fresh,
    # unproven source of losses -- exactly the kind of change this file's
    # whole history says needs its own real-data proof before it can default
    # to on.
    use_immediate_breakout: bool = False
    immediate_breakout_min_strength: float = 0.60   # 0-1 bar_strength; stricter than bare arming
    immediate_breakout_reward_risk: float = 2.00
    immediate_breakout_stop_atr_mult: float = 1.20


class BreakoutRetest(Strategy):
    name = "breakout_retest"

    _FEATURE_COLS = (
        "open", "high", "low", "close", "atr", "adx", "atr_med", "htf_ema",
        "close_pos", "bar_range", "retest_level", "retest_side",
        "retest_strength", "retest_width", "cooldown_ok",
        "breakout_now_side", "breakout_now_strength",
    )

    def __init__(self, params: BreakoutRetestParams | None = None):
        self.params = params or BreakoutRetestParams()
        self._a: dict[str, np.ndarray] = {}
        self._n: int = -1

    @staticmethod
    def _regime_gate_pass(
        side_v: float, atr_v: float, med: float, adx_v: float,
        close: float, htf_ema_v: float, p: "BreakoutRetestParams",
    ) -> bool:
        """ATR-regime, ADX and HTF-alignment gates shared by both entry paths.

        Extracted so the retest path (:meth:`_raw_conditions_pass`) and the
        opt-in immediate-breakout path cannot drift apart on what counts as
        tradeable regime/trend conditions -- they differ only in what they
        require of the price action itself (a proven retest vs. a single
        decisive bar), never in these gates.
        """
        if not (np.isfinite(atr_v) and np.isfinite(med)):
            return False
        if atr_v <= 0 or med <= 0:
            return False
        if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
            return False
        if p.adx_min > 0.0 and (not np.isfinite(adx_v) or adx_v < p.adx_min):
            return False
        if p.require_htf_alignment:
            if not np.isfinite(htf_ema_v):
                return False
            if side_v > 0 and close <= htf_ema_v:
                return False
            if side_v < 0 and close >= htf_ema_v:
                return False
        return True

    @staticmethod
    def _raw_conditions_pass(
        side_v: float, level: float, atr_v: float, med: float, adx_v: float,
        close: float, high_v: float, low_v: float, close_pos: float,
        htf_ema_v: float, p: "BreakoutRetestParams",
    ) -> bool:
        """The retest-path entry filters, EXCLUDING cooldown, as a pure function.

        Used from two places: the sequential scan in :meth:`prepare` (to
        compute, causally, the bar this same check last passed on -- see
        ``cooldown_ok`` below) and :meth:`entry` itself. One function means
        the two can never drift apart, which matters here specifically
        because the cooldown feature would otherwise have to duplicate this
        logic to stay causal.
        """
        if side_v == 0.0 or not np.isfinite(level):
            return False
        if not BreakoutRetest._regime_gate_pass(side_v, atr_v, med, adx_v, close, htf_ema_v, p):
            return False
        tol = p.retest_tolerance_atr * atr_v
        if side_v > 0:
            return low_v <= level + tol and close > level and close_pos >= p.retest_close_position_min
        return high_v >= level - tol and close < level and (1.0 - close_pos) >= p.retest_close_position_min

    @property
    def warmup(self) -> int:
        p = self.params
        return (
            max(p.regime_lookback, p.range_lookback, p.htf_ema_period)
            + p.retest_max_bars + 50
        )

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
        out["htf_ema"] = ind.ema(c, p.htf_ema_period)

        # The range excludes the bar being judged, exactly like XauBreak: a bar
        # cannot break a level that is partly made of itself.
        range_high = ind.rolling_high(h, p.range_lookback).shift(1)
        range_low = ind.rolling_low(l, p.range_lookback).shift(1)

        rng = (h - l).replace(0.0, np.nan)
        out["close_pos"] = (c - l) / rng
        out["bar_range"] = h - l

        atr_a = out["atr"].to_numpy(float)
        adx_a = out["adx"].to_numpy(float)
        med_a = out["atr_med"].to_numpy(float)
        htf_ema_a = out["htf_ema"].to_numpy(float)
        close_a = c.to_numpy(float)
        high_a = h.to_numpy(float)
        low_a = l.to_numpy(float)
        rh_a = range_high.to_numpy(float)
        rl_a = range_low.to_numpy(float)
        bar_range_a = out["bar_range"].to_numpy(float)
        close_pos_a = out["close_pos"].to_numpy(float)

        n = len(out)
        level = np.full(n, np.nan)
        side = np.zeros(n, dtype=float)
        strength = np.full(n, np.nan)   # breakout decisiveness, frozen at breakout time
        width = np.full(n, np.nan)      # the broken range's own height, for the measured move
        cooldown_ok = np.zeros(n, dtype=float)
        breakout_now_side = np.zeros(n, dtype=float)      # opt-in immediate-entry trigger
        breakout_now_strength = np.full(n, np.nan)

        pending_level = float("nan")
        pending_side = 0
        pending_age = 0
        pending_strength = float("nan")
        pending_width = float("nan")
        last_fire_bar = -10**9

        for i in range(n):
            # State as carried INTO this bar -- what entry() at i is allowed to
            # see. Recorded before anything below reacts to bar i itself.
            level[i] = pending_level
            side[i] = float(pending_side)
            strength[i] = pending_strength
            width[i] = pending_width

            # Whether a fresh signal is allowed at bar i is itself a causal,
            # sequential fact -- "has it been at least cooldown_bars since the
            # last bar whose OWN raw conditions passed" -- computed here for
            # the same reason retest_level/side are: entry() cannot look
            # backward through arbitrary history on its own, and duplicating
            # this scan inside entry() would make it stateful and unsafe to
            # call twice at the same bar (which the causality audit does, on
            # purpose, comparing a full-history call against a truncated one).
            cooldown_ok[i] = 1.0 if (i - last_fire_bar) >= p.cooldown_bars else 0.0
            if cooldown_ok[i] > 0.0 and self._raw_conditions_pass(
                pending_side, pending_level, atr_a[i], med_a[i], adx_a[i],
                close_a[i], high_a[i], low_a[i], close_pos_a[i], htf_ema_a[i], p,
            ):
                last_fire_bar = i

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
                    pending_strength = float("nan")
                    pending_width = float("nan")

            a_v = atr_a[i]
            rh, rl = rh_a[i], rl_a[i]
            if np.isfinite(a_v) and a_v > 0 and np.isfinite(rh) and np.isfinite(rl):
                decisive = bar_range_a[i] >= p.min_bar_range_atr * a_v
                margin = p.break_margin_atr * a_v
                broke_up = decisive and close_a[i] > rh + margin and close_pos_a[i] >= 0.5
                broke_down = decisive and close_a[i] < rl - margin and (1.0 - close_pos_a[i]) >= 0.5
                # How far the breakout bar's own range exceeded the bare
                # minimum required to call it decisive, as a 0-1 fraction.
                bar_strength = min(
                    max((bar_range_a[i] / a_v - p.min_bar_range_atr) / p.min_bar_range_atr, 0.0),
                    1.0,
                )
                if broke_up and not broke_down:
                    pending_side, pending_level, pending_age = 1, rh, 0
                    pending_strength, pending_width = bar_strength, rh - rl
                elif broke_down and not broke_up:
                    pending_side, pending_level, pending_age = -1, rl, 0
                    pending_strength, pending_width = bar_strength, rh - rl

                # Opt-in second trigger: bar i is ITSELF a fresh, unusually
                # decisive breakout -- independent of any pending retest state
                # (which reflects bars before i, never bar i's own). Gated by
                # the same regime/ADX/HTF filters and the same cooldown_ok[i]
                # already frozen above (computed from state carried into this
                # bar, so using it here cannot leak this bar's own result back
                # into itself). Disabled entirely when the flag is off, so
                # default behaviour is untouched byte-for-byte.
                if p.use_immediate_breakout:
                    imm_side = 0
                    if broke_up and not broke_down and bar_strength >= p.immediate_breakout_min_strength:
                        imm_side = 1
                    elif broke_down and not broke_up and bar_strength >= p.immediate_breakout_min_strength:
                        imm_side = -1
                    if imm_side != 0 and cooldown_ok[i] > 0.5 and self._regime_gate_pass(
                        float(imm_side), atr_a[i], med_a[i], adx_a[i], close_a[i], htf_ema_a[i], p,
                    ):
                        breakout_now_side[i] = float(imm_side)
                        breakout_now_strength[i] = bar_strength
                        last_fire_bar = i

        out["retest_level"] = level
        out["retest_side"] = side
        out["retest_strength"] = strength
        out["retest_width"] = width
        out["cooldown_ok"] = cooldown_ok
        out["breakout_now_side"] = breakout_now_side
        out["breakout_now_strength"] = breakout_now_strength
        self._a = {col: out[col].to_numpy(dtype=float) for col in self._FEATURE_COLS}
        self._n = n
        return out

    @staticmethod
    def _clamp01(x: float) -> float:
        return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)

    def _quality_score(
        self, breakout_strength: float, adx_v: float, precision: float, rejection: float
    ) -> float:
        """Average four independent 0-1 signals so no single one dominates.

        Each component answers a different question -- how decisive was the
        original break, how much trend is behind the continuation, how close
        (not just "close enough") was the retest, and how cleanly did the
        retest bar reject the level. A setup can only score near 1.0 by being
        genuinely strong on all four, not by maxing out one.
        """
        p = self.params
        adx_component = (
            self._clamp01((adx_v - p.adx_min) / p.adx_min)
            if p.adx_min > 0.0 and np.isfinite(adx_v)
            else 0.5  # filter is off: no gradient to read, so stay neutral
        )
        parts = [
            self._clamp01(breakout_strength) if np.isfinite(breakout_strength) else 0.5,
            adx_component,
            self._clamp01(precision),
            self._clamp01(rejection),
        ]
        return sum(parts) / len(parts)

    # ---------------------------------------------------------------- entry
    def entry(self, i: int, feat: pd.DataFrame) -> Signal | None:
        p = self.params
        self._bind(feat)
        a = self._a

        sig = self._entry_retest(i, a, p)
        if sig is not None:
            return sig
        if p.use_immediate_breakout:
            return self._entry_immediate(i, a, p)
        return None

    def _entry_retest(
        self, i: int, a: dict[str, np.ndarray], p: "BreakoutRetestParams"
    ) -> Signal | None:
        level, side_v = a["retest_level"][i], a["retest_side"][i]
        if side_v == 0.0 or not np.isfinite(level):
            return None
        if a["cooldown_ok"][i] < 0.5:
            return None

        atr_v, med, adx_v = a["atr"][i], a["atr_med"][i], a["adx"][i]
        close, high_v, low_v = a["close"][i], a["high"][i], a["low"][i]
        close_pos = a["close_pos"][i]
        htf_ema_v = a["htf_ema"][i]
        breakout_strength, range_width = a["retest_strength"][i], a["retest_width"][i]

        if not (np.isfinite(atr_v) and np.isfinite(med)):
            return None
        if atr_v <= 0 or med <= 0:
            return None
        if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
            return None
        if p.adx_min > 0.0 and (not np.isfinite(adx_v) or adx_v < p.adx_min):
            return None
        if p.require_htf_alignment:
            if not np.isfinite(htf_ema_v):
                return None
            if side_v > 0 and close <= htf_ema_v:
                return None
            if side_v < 0 and close >= htf_ema_v:
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
            precision = 1.0 - abs(low_v - level) / tol if tol > 0 else 0.5
            rejection = (
                (close_pos - p.retest_close_position_min) / (1.0 - p.retest_close_position_min)
                if p.retest_close_position_min < 1.0 else 1.0
            )
            quality = self._quality_score(breakout_strength, adx_v, precision, rejection)
            rr = p.quality_min_reward_risk + quality * (p.quality_max_reward_risk - p.quality_min_reward_risk)
            risk_dist = close - sl
            tp_dist = rr * risk_dist
            if p.use_measured_move_target and np.isfinite(range_width) and range_width > 0:
                tp_dist = max(tp_dist, range_width)
            tp = close + tp_dist
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
            precision = 1.0 - abs(high_v - level) / tol if tol > 0 else 0.5
            rejection = (
                ((1.0 - close_pos) - p.retest_close_position_min) / (1.0 - p.retest_close_position_min)
                if p.retest_close_position_min < 1.0 else 1.0
            )
            quality = self._quality_score(breakout_strength, adx_v, precision, rejection)
            rr = p.quality_min_reward_risk + quality * (p.quality_max_reward_risk - p.quality_min_reward_risk)
            risk_dist = sl - close
            tp_dist = rr * risk_dist
            if p.use_measured_move_target and np.isfinite(range_width) and range_width > 0:
                tp_dist = max(tp_dist, range_width)
            tp = close - tp_dist
            side = Side.SHORT

        risk_mult = 1.0 + quality * (p.quality_max_risk_mult - 1.0)

        return Signal(
            side=side,
            stop_loss=float(sl),
            take_profit=float(tp),
            reason="breakout_retest",
            meta={
                "atr": float(atr_v),
                "level": float(level),
                "target_distance": float(abs(tp - close)),
                "quality": float(quality),
                "risk_multiplier": float(risk_mult),
            },
        )

    def _entry_immediate(
        self, i: int, a: dict[str, np.ndarray], p: "BreakoutRetestParams"
    ) -> Signal | None:
        """Opt-in second trigger: enter on the breakout bar itself.

        Everything that makes this safe to fire already happened causally in
        :meth:`prepare` -- decisiveness above the stricter
        ``immediate_breakout_min_strength`` bar, the same regime/ADX/HTF gate
        as the retest path, and the same cooldown. There is no retest bar to
        size a stop or a precision/rejection score from, so the stop is a
        plain ATR multiple and the quality score is the breakout strength
        alone.
        """
        side_v = a["breakout_now_side"][i]
        if side_v == 0.0:
            return None
        if a["cooldown_ok"][i] < 0.5:
            return None
        atr_v = a["atr"][i]
        close = a["close"][i]
        strength = a["breakout_now_strength"][i]
        if not np.isfinite(atr_v) or atr_v <= 0 or not np.isfinite(strength):
            return None

        if side_v > 0:
            sl = close - p.immediate_breakout_stop_atr_mult * atr_v
            if sl >= close:
                return None
            tp_dist = p.immediate_breakout_reward_risk * (close - sl)
            tp = close + tp_dist
            side = Side.LONG
        else:
            sl = close + p.immediate_breakout_stop_atr_mult * atr_v
            if sl <= close:
                return None
            tp_dist = p.immediate_breakout_reward_risk * (sl - close)
            tp = close - tp_dist
            side = Side.SHORT

        quality = self._clamp01(strength)
        risk_mult = 1.0 + quality * (p.quality_max_risk_mult - 1.0)

        return Signal(
            side=side,
            stop_loss=float(sl),
            take_profit=float(tp),
            reason="breakout_immediate",
            meta={
                "atr": float(atr_v),
                "target_distance": float(abs(tp - close)),
                "quality": float(quality),
                "risk_multiplier": float(risk_mult),
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
        if risk <= 0 or atr_v <= 0:
            return pos.stop_loss, pos.take_profit
        if p.breakeven_at_r is None and p.trail_at_r is None:
            return pos.stop_loss, pos.take_profit

        sl = pos.stop_loss
        if pos.side is Side.LONG:
            gained_r = (close - pos.entry_price) / risk
            if p.breakeven_at_r is not None and gained_r >= p.breakeven_at_r:
                sl = max(sl, pos.entry_price + p.breakeven_offset_atr * atr_v)
            if p.trail_at_r is not None and gained_r >= p.trail_at_r:
                sl = max(sl, close - p.trail_atr_mult * atr_v)
            sl = min(sl, close)
        else:
            gained_r = (pos.entry_price - close) / risk
            if p.breakeven_at_r is not None and gained_r >= p.breakeven_at_r:
                sl = min(sl, pos.entry_price - p.breakeven_offset_atr * atr_v)
            if p.trail_at_r is not None and gained_r >= p.trail_at_r:
                sl = min(sl, close + p.trail_atr_mult * atr_v)
            sl = max(sl, close)
        return sl, pos.take_profit
