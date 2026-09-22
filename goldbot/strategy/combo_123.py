"""123: one bot, five philosophies -- built by composing the existing strategies.

This is not a sixth, independently-invented strategy. It is a dispatcher over
the five that already exist and are already shipped as their own EAs
(:mod:`breakout_retest` / XauRetest, :mod:`breakout_momentum` / XauBreak,
:mod:`trend_pullback` / XauTrend, :mod:`mean_reversion_scalper` / Anas,
:mod:`volume_profile_wyckoff` / XauWyck). Each keeps its own parameters, its
own feature computation, and its own in-trade management -- this class only
decides, bar by bar, which one (if any) gets to act, and later, which one
gets to manage the trade it opened.

Why composition, not reimplementation
--------------------------------------
Every one of the five sub-strategies is already built and (to varying
degrees) tested on its own. Re-deriving their entry logic here by hand would
risk exactly the kind of silent drift the parity tests in this project exist
to catch. Instead this class *instantiates* each sub-strategy internally,
calls its real ``prepare``/``entry``/``manage``, and only relabels columns
with a short prefix (``rt_``, ``bk_``, ``tr_``, ``mr_``, ``wk_``) so their
features can live side by side in one merged frame without colliding --
e.g. two sub-strategies both compute an ``atr``, possibly with different
periods, so each needs its own column. The actual arithmetic is untouched.

One position, five candidates, a fixed priority order
-------------------------------------------------------
The engine (and every EA in this project) enforces exactly one open position
at a time. ``entry()`` tries the sub-strategies in a fixed order and returns
the first signal it gets -- never more than one per bar, and never a second
position on top of an open one. The order tried is:

    1. breakout + retest (confirmed break, then a proven retest)
    2. breakout + retest's own opt-in immediate-entry trigger (if enabled)
    3. breakout momentum (confirmed break, no retest wait)
    4. trend pullback (trades with an established trend)
    5. mean-reversion scalp (trades against a stretched, exhausted move)
    6. volume-profile Wyckoff fade (trades against a failed probe)

This is ordered by how much confirmation each style demands before it acts,
most-confirmed first -- a defensible tie-break for the rare bar where more
than one philosophy's conditions line up at once, not a claim that any one
of them is "better". Only :mod:`breakout_retest` has been checked against
real Strategy Tester reports in this project; the other four are shipped,
tested-for-causality strategies with no real-money evidence gathered here.

Once a position is open, ``manage()`` reads ``Position.reason`` (each
sub-strategy already tags its own signals with a distinct string) and hands
management back to the exact sub-strategy that opened it, using that
sub-strategy's own break-even/trailing rules -- never a generic one-size-fits
rule, because e.g. breakout_momentum deliberately does *not* move its stop
early (see that module's docstring) while others do.

What is new here, and what is not
------------------------------------
Nothing about any single sub-strategy's edge is new. What is new, and
therefore UNVALIDATED, is running all five side by side and letting whichever
fires first take the trade -- their filters were tuned in isolation, not for
this combination, and interaction effects (e.g. one sub-strategy's cooldown
freeing up a bar right when another's setup completes) have not been
observed against real data. Treat this exactly like the immediate-breakout
trigger in :mod:`breakout_retest`: real Strategy Tester validation before any
real money, not a promotion in trust just because five familiar names are
inside it.

Session hours are deliberately left open (no default restriction) rather than
inheriting XauRetest's hard-won 8-10/12-16 GMT windows: those hours are
proven for *that one strategy's* filters, not for this five-way combination,
and re-imposing them here without evidence would be the same mistake as
widening them without evidence was in the other direction. Narrow the hours
in ``risk.sessions`` once a real backtest shows which of them this
combination should actually trade.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from ..backtest.types import Position, Signal
from .base import Strategy, StrategyParams
from .breakout_momentum import BreakoutMomentum, BreakoutMomentumParams
from .breakout_retest import BreakoutRetest, BreakoutRetestParams
from .mean_reversion_scalper import MeanReversionScalper, MeanReversionScalperParams
from .trend_pullback import TrendPullback, TrendPullbackParams
from .volume_profile_wyckoff import VolumeProfileWyckoff, VolumeProfileWyckoffParams

_BASE_COLS = ("open", "high", "low", "close", "volume")

# Position.reason -> which sub-strategy's manage() owns this trade. Each
# string is exactly what that sub-strategy's own entry() already sets.
_REASON_PREFIX = {
    "breakout_retest": "rt",
    "breakout_immediate": "rt",
    "range_break_impulse": "bk",
    "trend_up_pullback_resume": "tr",
    "trend_dn_pullback_resume": "tr",
    "stretch_fade": "mr",
    "spring": "wk",
    "upthrust": "wk",
}


@dataclass
class Combo123Params(StrategyParams):
    # -- which sub-strategies may fire. All on by default: this bot exists
    # specifically to combine all five, and gating individual ones defeats
    # the point -- but see the class docstring, the COMBINATION is what is
    # unvalidated, not any one of these switches.
    enable_retest: bool = True
    enable_breakout_momentum: bool = True
    enable_trend_pullback: bool = True
    enable_mean_reversion: bool = True
    enable_wyckoff: bool = True

    # ===================================================== breakout + retest
    # (XauRetest / breakout_retest.py -- field-for-field identical defaults)
    rt_range_lookback: int = 20
    rt_break_margin_atr: float = 0.10
    rt_min_bar_range_atr: float = 0.50
    rt_atr_period: int = 14
    rt_regime_lookback: int = 288
    rt_atr_min_mult: float = 0.55
    rt_atr_max_mult: float = 3.00
    rt_adx_period: int = 14
    rt_adx_min: float = 15.0
    rt_htf_ema_period: int = 100
    rt_require_htf_alignment: bool = True
    rt_retest_max_bars: int = 12
    rt_retest_tolerance_atr: float = 0.15
    rt_retest_close_position_min: float = 0.68
    rt_cooldown_bars: int = 12
    rt_stop_atr_buffer: float = 0.20
    rt_min_stop_atr_mult: float = 1.00
    rt_min_target_spread_ratio: float = 6.0
    rt_quality_min_reward_risk: float = 1.50
    rt_quality_max_reward_risk: float = 3.00
    rt_use_measured_move_target: bool = True
    rt_quality_max_risk_mult: float = 1.50
    rt_breakeven_at_r: float | None = 1.0
    rt_breakeven_offset_atr: float = 0.05
    rt_trail_at_r: float | None = 1.5
    rt_trail_atr_mult: float = 1.20
    rt_use_immediate_breakout: bool = False
    rt_immediate_breakout_min_strength: float = 0.60
    rt_immediate_breakout_reward_risk: float = 2.00
    rt_immediate_breakout_stop_atr_mult: float = 1.20

    # =================================================== breakout momentum
    # (XauBreak / breakout_momentum.py)
    bk_range_lookback: int = 12
    bk_squeeze_max_atr: float = 4.00
    bk_break_margin_atr: float = 0.05
    bk_atr_period: int = 14
    bk_min_bar_range_atr: float = 0.60
    bk_close_position_min: float = 0.60
    bk_regime_lookback: int = 288
    bk_atr_min_mult: float = 0.55
    bk_atr_max_mult: float = 3.00
    bk_adx_period: int = 14
    bk_adx_min: float = 0.0
    bk_trend_ema_period: int = 100
    bk_require_trend_alignment: bool = False
    bk_stop_atr_buffer: float = 0.20
    bk_min_stop_atr_mult: float = 1.00
    bk_reward_risk: float = 4.00
    bk_min_target_spread_ratio: float = 8.0
    bk_breakeven_at_r: float | None = None
    bk_breakeven_offset_atr: float = 0.05
    bk_trail_at_r: float | None = None

    # ====================================================== trend pullback
    # (XauTrend / trend_pullback.py)
    tr_ema_fast: int = 21
    tr_ema_slow: int = 55
    tr_ema_trend: int = 200
    tr_adx_period: int = 14
    tr_adx_min: float = 22.0
    tr_atr_period: int = 14
    tr_regime_lookback: int = 288
    tr_atr_min_mult: float = 0.80
    tr_atr_max_mult: float = 2.50
    tr_rsi_period: int = 14
    tr_rsi_pullback_long: float = 45.0
    tr_rsi_pullback_short: float = 55.0
    tr_pullback_lookback: int = 8
    tr_swing_lookback: int = 12
    tr_stop_atr_buffer: float = 0.35
    tr_min_stop_atr_mult: float = 1.00
    tr_reward_risk: float = 1.8
    tr_breakeven_at_r: float | None = 1.0
    tr_breakeven_offset_atr: float = 0.10
    tr_trail_at_r: float | None = 1.5
    tr_trail_atr_mult: float = 1.5

    # ================================================= mean-reversion scalp
    # (Anas / mean_reversion_scalper.py)
    mr_ema_period: int = 20
    mr_adx_period: int = 14
    mr_adx_max: float = 50.0
    mr_atr_period: int = 14
    mr_regime_lookback: int = 288
    mr_atr_min_mult: float = 0.55
    mr_atr_max_mult: float = 3.00
    mr_stretch_atr: float = 0.60
    mr_rsi_period: int = 7
    mr_rsi_oversold: float = 38.0
    mr_rsi_overbought: float = 62.0
    mr_swing_lookback: int = 6
    mr_stop_atr_buffer: float = 0.25
    mr_min_stop_atr_mult: float = 1.30
    mr_reward_risk: float = 1.20
    mr_min_target_spread_ratio: float = 5.0
    mr_htf_ema_period: int = 100
    mr_htf_slope_lookback: int = 20
    mr_htf_max_slope_atr: float = 0.06
    mr_close_position_min: float = 0.55
    mr_require_divergence: bool = False
    mr_divergence_lookback: int = 12
    mr_breakeven_at_r: float | None = 0.8
    mr_breakeven_offset_atr: float = 0.05
    mr_trail_at_r: float | None = None

    # ============================================== volume-profile Wyckoff
    # (XauWyck / volume_profile_wyckoff.py)
    wk_profile_lookback: int = 96
    wk_profile_bins: int = 40
    wk_value_area_pct: float = 0.70
    wk_profile_update_bars: int = 12
    wk_probe_depth_atr: float = 0.10
    wk_close_back_margin_atr: float = 0.05
    wk_probe_volume_max: float = 1.40
    wk_close_position_min: float = 0.55
    wk_atr_period: int = 14
    wk_regime_lookback: int = 288
    wk_atr_min_mult: float = 0.55
    wk_atr_max_mult: float = 3.00
    wk_adx_period: int = 14
    wk_adx_max: float = 45.0
    wk_stop_atr_buffer: float = 0.30
    wk_min_stop_atr_mult: float = 1.00
    wk_reward_risk: float = 2.20
    wk_target_poc: bool = True
    wk_min_target_spread_ratio: float = 6.0
    wk_breakeven_at_r: float | None = None
    wk_breakeven_offset_atr: float = 0.05
    wk_trail_at_r: float | None = None


def _rt_params(p: Combo123Params) -> BreakoutRetestParams:
    return BreakoutRetestParams(
        range_lookback=p.rt_range_lookback,
        break_margin_atr=p.rt_break_margin_atr,
        min_bar_range_atr=p.rt_min_bar_range_atr,
        atr_period=p.rt_atr_period,
        regime_lookback=p.rt_regime_lookback,
        atr_min_mult=p.rt_atr_min_mult,
        atr_max_mult=p.rt_atr_max_mult,
        adx_period=p.rt_adx_period,
        adx_min=p.rt_adx_min,
        htf_ema_period=p.rt_htf_ema_period,
        require_htf_alignment=p.rt_require_htf_alignment,
        retest_max_bars=p.rt_retest_max_bars,
        retest_tolerance_atr=p.rt_retest_tolerance_atr,
        retest_close_position_min=p.rt_retest_close_position_min,
        cooldown_bars=p.rt_cooldown_bars,
        stop_atr_buffer=p.rt_stop_atr_buffer,
        min_stop_atr_mult=p.rt_min_stop_atr_mult,
        min_target_spread_ratio=p.rt_min_target_spread_ratio,
        quality_min_reward_risk=p.rt_quality_min_reward_risk,
        quality_max_reward_risk=p.rt_quality_max_reward_risk,
        use_measured_move_target=p.rt_use_measured_move_target,
        quality_max_risk_mult=p.rt_quality_max_risk_mult,
        breakeven_at_r=p.rt_breakeven_at_r,
        breakeven_offset_atr=p.rt_breakeven_offset_atr,
        trail_at_r=p.rt_trail_at_r,
        trail_atr_mult=p.rt_trail_atr_mult,
        use_immediate_breakout=p.rt_use_immediate_breakout,
        immediate_breakout_min_strength=p.rt_immediate_breakout_min_strength,
        immediate_breakout_reward_risk=p.rt_immediate_breakout_reward_risk,
        immediate_breakout_stop_atr_mult=p.rt_immediate_breakout_stop_atr_mult,
    )


def _bk_params(p: Combo123Params) -> BreakoutMomentumParams:
    return BreakoutMomentumParams(
        range_lookback=p.bk_range_lookback,
        squeeze_max_atr=p.bk_squeeze_max_atr,
        break_margin_atr=p.bk_break_margin_atr,
        atr_period=p.bk_atr_period,
        min_bar_range_atr=p.bk_min_bar_range_atr,
        close_position_min=p.bk_close_position_min,
        regime_lookback=p.bk_regime_lookback,
        atr_min_mult=p.bk_atr_min_mult,
        atr_max_mult=p.bk_atr_max_mult,
        adx_period=p.bk_adx_period,
        adx_min=p.bk_adx_min,
        trend_ema_period=p.bk_trend_ema_period,
        require_trend_alignment=p.bk_require_trend_alignment,
        stop_atr_buffer=p.bk_stop_atr_buffer,
        min_stop_atr_mult=p.bk_min_stop_atr_mult,
        reward_risk=p.bk_reward_risk,
        min_target_spread_ratio=p.bk_min_target_spread_ratio,
        breakeven_at_r=p.bk_breakeven_at_r,
        breakeven_offset_atr=p.bk_breakeven_offset_atr,
        trail_at_r=p.bk_trail_at_r,
    )


def _tr_params(p: Combo123Params) -> TrendPullbackParams:
    return TrendPullbackParams(
        ema_fast=p.tr_ema_fast,
        ema_slow=p.tr_ema_slow,
        ema_trend=p.tr_ema_trend,
        adx_period=p.tr_adx_period,
        adx_min=p.tr_adx_min,
        atr_period=p.tr_atr_period,
        regime_lookback=p.tr_regime_lookback,
        atr_min_mult=p.tr_atr_min_mult,
        atr_max_mult=p.tr_atr_max_mult,
        rsi_period=p.tr_rsi_period,
        rsi_pullback_long=p.tr_rsi_pullback_long,
        rsi_pullback_short=p.tr_rsi_pullback_short,
        pullback_lookback=p.tr_pullback_lookback,
        swing_lookback=p.tr_swing_lookback,
        stop_atr_buffer=p.tr_stop_atr_buffer,
        min_stop_atr_mult=p.tr_min_stop_atr_mult,
        reward_risk=p.tr_reward_risk,
        breakeven_at_r=p.tr_breakeven_at_r,
        breakeven_offset_atr=p.tr_breakeven_offset_atr,
        trail_at_r=p.tr_trail_at_r,
        trail_atr_mult=p.tr_trail_atr_mult,
    )


def _mr_params(p: Combo123Params) -> MeanReversionScalperParams:
    return MeanReversionScalperParams(
        ema_period=p.mr_ema_period,
        adx_period=p.mr_adx_period,
        adx_max=p.mr_adx_max,
        atr_period=p.mr_atr_period,
        regime_lookback=p.mr_regime_lookback,
        atr_min_mult=p.mr_atr_min_mult,
        atr_max_mult=p.mr_atr_max_mult,
        stretch_atr=p.mr_stretch_atr,
        rsi_period=p.mr_rsi_period,
        rsi_oversold=p.mr_rsi_oversold,
        rsi_overbought=p.mr_rsi_overbought,
        swing_lookback=p.mr_swing_lookback,
        stop_atr_buffer=p.mr_stop_atr_buffer,
        min_stop_atr_mult=p.mr_min_stop_atr_mult,
        reward_risk=p.mr_reward_risk,
        min_target_spread_ratio=p.mr_min_target_spread_ratio,
        htf_ema_period=p.mr_htf_ema_period,
        htf_slope_lookback=p.mr_htf_slope_lookback,
        htf_max_slope_atr=p.mr_htf_max_slope_atr,
        close_position_min=p.mr_close_position_min,
        require_divergence=p.mr_require_divergence,
        divergence_lookback=p.mr_divergence_lookback,
        breakeven_at_r=p.mr_breakeven_at_r,
        breakeven_offset_atr=p.mr_breakeven_offset_atr,
        trail_at_r=p.mr_trail_at_r,
    )


def _wk_params(p: Combo123Params) -> VolumeProfileWyckoffParams:
    return VolumeProfileWyckoffParams(
        profile_lookback=p.wk_profile_lookback,
        profile_bins=p.wk_profile_bins,
        value_area_pct=p.wk_value_area_pct,
        profile_update_bars=p.wk_profile_update_bars,
        probe_depth_atr=p.wk_probe_depth_atr,
        close_back_margin_atr=p.wk_close_back_margin_atr,
        probe_volume_max=p.wk_probe_volume_max,
        close_position_min=p.wk_close_position_min,
        atr_period=p.wk_atr_period,
        regime_lookback=p.wk_regime_lookback,
        atr_min_mult=p.wk_atr_min_mult,
        atr_max_mult=p.wk_atr_max_mult,
        adx_period=p.wk_adx_period,
        adx_max=p.wk_adx_max,
        stop_atr_buffer=p.wk_stop_atr_buffer,
        min_stop_atr_mult=p.wk_min_stop_atr_mult,
        reward_risk=p.wk_reward_risk,
        target_poc=p.wk_target_poc,
        min_target_spread_ratio=p.wk_min_target_spread_ratio,
        breakeven_at_r=p.wk_breakeven_at_r,
        breakeven_offset_atr=p.wk_breakeven_offset_atr,
        trail_at_r=p.wk_trail_at_r,
    )


class Combo123(Strategy):
    name = "combo_123"

    def __init__(self, params: Combo123Params | None = None):
        self.params = params or Combo123Params()
        p = self.params
        self._rt = BreakoutRetest(_rt_params(p))
        self._bk = BreakoutMomentum(_bk_params(p))
        self._tr = TrendPullback(_tr_params(p))
        self._mr = MeanReversionScalper(_mr_params(p))
        self._wk = VolumeProfileWyckoff(_wk_params(p))
        # (sub_instance, its _FEATURE_COLS) keyed by the same short prefix
        # used for column names and for Position.reason dispatch.
        self._subs = {
            "rt": (self._rt, BreakoutRetest._FEATURE_COLS),
            "bk": (self._bk, BreakoutMomentum._FEATURE_COLS),
            "tr": (self._tr, TrendPullback._FEATURE_COLS),
            "mr": (self._mr, MeanReversionScalper._FEATURE_COLS),
            "wk": (self._wk, VolumeProfileWyckoff._FEATURE_COLS),
        }
        # Reconstructed per-sub-strategy views, cached by (prefix -> (len, df)).
        # Keyed by length only, exactly like every sub-strategy's own _bind()
        # -- correct because the causality audit's two prepare() calls always
        # differ in length (full history vs. a strictly shorter prefix), and
        # a normal backtest calls entry()/manage() with one constant `feat`
        # for its whole run.
        self._sub_cache: dict[str, tuple[int, pd.DataFrame]] = {}

    @property
    def warmup(self) -> int:
        return max(sub.warmup for sub, _ in self._subs.values())

    # ------------------------------------------------------------- features
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        feats = {prefix: sub.prepare(df) for prefix, (sub, _) in self._subs.items()}

        out = df.copy()
        # Wyckoff always has a clean, filled volume column (synthesised as
        # 1.0 when the feed has none) -- carry that version through so any
        # sub-strategy needing "volume" back out of the merged frame gets it,
        # regardless of what the raw input had.
        out["volume"] = feats["wk"]["volume"]

        for prefix, (_, cols) in self._subs.items():
            src = feats[prefix]
            for col in cols:
                if col in _BASE_COLS:
                    continue
                out[f"{prefix}_{col}"] = src[col]

        self._sub_cache = {}
        return out

    def _sub_frame(self, feat: pd.DataFrame, prefix: str) -> pd.DataFrame:
        """Rebuild one sub-strategy's own unprefixed view from the merged frame."""
        cached = self._sub_cache.get(prefix)
        if cached is not None and cached[0] == len(feat):
            return cached[1]
        _, cols = self._subs[prefix]
        sub = pd.DataFrame(index=feat.index)
        for col in cols:
            sub[col] = feat[col] if col in _BASE_COLS else feat[f"{prefix}_{col}"]
        self._sub_cache[prefix] = (len(feat), sub)
        return sub

    # ---------------------------------------------------------------- entry
    def entry(self, i: int, feat: pd.DataFrame) -> Signal | None:
        p = self.params

        if p.enable_retest:
            sig = self._rt.entry(i, self._sub_frame(feat, "rt"))
            if sig is not None:
                return sig
        if p.enable_breakout_momentum:
            sig = self._bk.entry(i, self._sub_frame(feat, "bk"))
            if sig is not None:
                return sig
        if p.enable_trend_pullback:
            sig = self._tr.entry(i, self._sub_frame(feat, "tr"))
            if sig is not None:
                return sig
        if p.enable_mean_reversion:
            sig = self._mr.entry(i, self._sub_frame(feat, "mr"))
            if sig is not None:
                return sig
        if p.enable_wyckoff:
            sig = self._wk.entry(i, self._sub_frame(feat, "wk"))
            if sig is not None:
                return sig
        return None

    # --------------------------------------------------------------- manage
    def manage(self, i: int, feat: pd.DataFrame, pos: Position) -> tuple[float, float | None]:
        prefix = _REASON_PREFIX.get(pos.reason)
        if prefix is None:
            return pos.stop_loss, pos.take_profit
        sub, _ = self._subs[prefix]
        return sub.manage(i, self._sub_frame(feat, prefix), pos)
