"""Parity between the Python 123 combo strategy and 123_M5.mq5.

123 is a dispatcher over five already-shipped strategies, not a sixth
invented one: :class:`Combo123` instantiates the real
``BreakoutRetest``/``BreakoutMomentum``/``TrendPullback``/
``MeanReversionScalper``/``VolumeProfileWyckoff`` objects internally and
delegates to them (see the class docstring in ``combo_123.py``). That
delegation means each sub-strategy's Python-side correctness is already
covered by its own existing parity suite
(``test_xauretest_parity.py``, ``test_xaubreak_parity.py``,
``test_mql5_parity.py``, ``test_xaupulse_parity.py``,
``test_xauwyck_parity.py``) -- this file does not re-prove that arithmetic.

What IS new and needs its own check is ``mt5/123_M5.mq5``: a hand-written
MQL5 file that re-implements all five sub-strategies' entry conditions
under prefixed names (``InpRt*``, ``InpBk*``, ``InpTr*``, ``InpMr*``,
``InpWk*``) inside one EA. A transcription slip there -- a flipped
comparison, a renamed input that silently stopped being read -- would
otherwise be invisible. This file reuses each sub-strategy's own
``mql5_entry`` mirror (adapted to the prefixed input names) and compares it
against ``Combo123``'s delegated sub-strategy instances, exactly the same
technique the five standalone suites already use for their own EAs.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from goldbot.backtest.types import Side
from goldbot.strategy.breakout_momentum import BreakoutMomentumParams
from goldbot.strategy.breakout_retest import BreakoutRetestParams
from goldbot.strategy.combo_123 import Combo123, Combo123Params
from goldbot.strategy.mean_reversion_scalper import MeanReversionScalperParams
from goldbot.strategy.trend_pullback import TrendPullbackParams
from goldbot.strategy.volume_profile_wyckoff import VolumeProfileWyckoffParams

MQ5 = Path(__file__).resolve().parents[1] / "mt5" / "123_M5.mq5"

# mq5 Inp* name -> Combo123Params field. Auto-derived from the input list plus
# a handful of manual overrides for MQL5's own abbreviations (RR, Off, Align,
# Pos, Imm) -- the same abbreviations the five standalone EAs already use, so
# these overrides mirror what their own PARAM_MAPs already do.
PARAM_MAP = {
    "InpBkAdxMin": "bk_adx_min",
    "InpBkAdxPeriod": "bk_adx_period",
    "InpBkAtrMaxMult": "bk_atr_max_mult",
    "InpBkAtrMinMult": "bk_atr_min_mult",
    "InpBkAtrPeriod": "bk_atr_period",
    "InpBkBreakMarginAtr": "bk_break_margin_atr",
    # InpBkBreakevenAtR excluded: bk_breakeven_at_r defaults to None (breakeven
    # OFF by default for this sub-strategy, see "the break-even trap" in
    # breakout_momentum.py) but MQL5 still needs a concrete float for the
    # input -- same exclusion XauBreak's own PARAM_MAP already makes.
    "InpBkBreakevenOffAtr": "bk_breakeven_offset_atr",
    "InpBkClosePositionMin": "bk_close_position_min",
    "InpBkMinBarRangeAtr": "bk_min_bar_range_atr",
    "InpBkMinStopAtrMult": "bk_min_stop_atr_mult",
    "InpBkMinTargetSpreadRatio": "bk_min_target_spread_ratio",
    "InpBkRangeLookback": "bk_range_lookback",
    "InpBkRegimeLookback": "bk_regime_lookback",
    "InpBkRequireTrendAlign": "bk_require_trend_alignment",
    "InpBkRewardRisk": "bk_reward_risk",
    "InpBkSqueezeMaxAtr": "bk_squeeze_max_atr",
    "InpBkStopAtrBuffer": "bk_stop_atr_buffer",
    "InpBkTrendEmaPeriod": "bk_trend_ema_period",
    "InpMrAdxMax": "mr_adx_max",
    "InpMrAdxPeriod": "mr_adx_period",
    "InpMrAtrMaxMult": "mr_atr_max_mult",
    "InpMrAtrMinMult": "mr_atr_min_mult",
    "InpMrAtrPeriod": "mr_atr_period",
    "InpMrBreakevenAtR": "mr_breakeven_at_r",
    "InpMrBreakevenOffAtr": "mr_breakeven_offset_atr",
    "InpMrClosePositionMin": "mr_close_position_min",
    "InpMrDivergenceLookback": "mr_divergence_lookback",
    "InpMrEmaPeriod": "mr_ema_period",
    "InpMrHtfEmaPeriod": "mr_htf_ema_period",
    "InpMrHtfMaxSlopeAtr": "mr_htf_max_slope_atr",
    "InpMrHtfSlopeLookback": "mr_htf_slope_lookback",
    "InpMrMinStopAtrMult": "mr_min_stop_atr_mult",
    "InpMrMinTargetSpreadRatio": "mr_min_target_spread_ratio",
    "InpMrRegimeLookback": "mr_regime_lookback",
    "InpMrRequireDivergence": "mr_require_divergence",
    "InpMrRewardRisk": "mr_reward_risk",
    "InpMrRsiOverbought": "mr_rsi_overbought",
    "InpMrRsiOversold": "mr_rsi_oversold",
    "InpMrRsiPeriod": "mr_rsi_period",
    "InpMrStopAtrBuffer": "mr_stop_atr_buffer",
    "InpMrStretchAtr": "mr_stretch_atr",
    "InpMrSwingLookback": "mr_swing_lookback",
    "InpRtAdxMin": "rt_adx_min",
    "InpRtAdxPeriod": "rt_adx_period",
    "InpRtAtrMaxMult": "rt_atr_max_mult",
    "InpRtAtrMinMult": "rt_atr_min_mult",
    "InpRtAtrPeriod": "rt_atr_period",
    "InpRtBreakMarginAtr": "rt_break_margin_atr",
    "InpRtBreakevenAtR": "rt_breakeven_at_r",
    "InpRtBreakevenOffAtr": "rt_breakeven_offset_atr",
    "InpRtCooldownBars": "rt_cooldown_bars",
    "InpRtHtfEmaPeriod": "rt_htf_ema_period",
    "InpRtImmBreakoutMinStrength": "rt_immediate_breakout_min_strength",
    "InpRtImmBreakoutRR": "rt_immediate_breakout_reward_risk",
    "InpRtImmBreakoutStopAtrMult": "rt_immediate_breakout_stop_atr_mult",
    "InpRtMinBarRangeAtr": "rt_min_bar_range_atr",
    "InpRtMinStopAtrMult": "rt_min_stop_atr_mult",
    "InpRtMinTargetSpreadRatio": "rt_min_target_spread_ratio",
    "InpRtQualityMaxRR": "rt_quality_max_reward_risk",
    "InpRtQualityMaxRiskMult": "rt_quality_max_risk_mult",
    "InpRtQualityMinRR": "rt_quality_min_reward_risk",
    "InpRtRangeLookback": "rt_range_lookback",
    "InpRtRegimeLookback": "rt_regime_lookback",
    "InpRtRequireHtfAlign": "rt_require_htf_alignment",
    "InpRtRetestClosePosMin": "rt_retest_close_position_min",
    "InpRtRetestMaxBars": "rt_retest_max_bars",
    "InpRtRetestToleranceAtr": "rt_retest_tolerance_atr",
    "InpRtStopAtrBuffer": "rt_stop_atr_buffer",
    "InpRtTrailAtR": "rt_trail_at_r",
    "InpRtTrailAtrMult": "rt_trail_atr_mult",
    "InpRtUseImmediateBreakout": "rt_use_immediate_breakout",
    "InpRtUseMeasuredMove": "rt_use_measured_move_target",
    "InpTrAdxMin": "tr_adx_min",
    "InpTrAdxPeriod": "tr_adx_period",
    "InpTrAtrMaxMult": "tr_atr_max_mult",
    "InpTrAtrMinMult": "tr_atr_min_mult",
    "InpTrAtrPeriod": "tr_atr_period",
    "InpTrBreakevenAtR": "tr_breakeven_at_r",
    "InpTrBreakevenOffAtr": "tr_breakeven_offset_atr",
    "InpTrEmaFast": "tr_ema_fast",
    "InpTrEmaSlow": "tr_ema_slow",
    "InpTrEmaTrend": "tr_ema_trend",
    "InpTrMinStopAtrMult": "tr_min_stop_atr_mult",
    "InpTrPullbackLookback": "tr_pullback_lookback",
    "InpTrRegimeLookback": "tr_regime_lookback",
    "InpTrRewardRisk": "tr_reward_risk",
    "InpTrRsiPeriod": "tr_rsi_period",
    "InpTrRsiPullbackLong": "tr_rsi_pullback_long",
    "InpTrRsiPullbackShort": "tr_rsi_pullback_short",
    "InpTrStopAtrBuffer": "tr_stop_atr_buffer",
    "InpTrSwingLookback": "tr_swing_lookback",
    "InpTrTrailAtR": "tr_trail_at_r",
    "InpTrTrailAtrMult": "tr_trail_atr_mult",
    "InpWkAdxMax": "wk_adx_max",
    "InpWkAdxPeriod": "wk_adx_period",
    "InpWkAtrMaxMult": "wk_atr_max_mult",
    "InpWkAtrMinMult": "wk_atr_min_mult",
    "InpWkAtrPeriod": "wk_atr_period",
    # InpWkBreakevenAtR excluded: wk_breakeven_at_r defaults to None, same
    # reasoning and same exclusion as InpBkBreakevenAtR above.
    "InpWkBreakevenOffAtr": "wk_breakeven_offset_atr",
    "InpWkCloseBackMarginAtr": "wk_close_back_margin_atr",
    "InpWkClosePositionMin": "wk_close_position_min",
    "InpWkMinStopAtrMult": "wk_min_stop_atr_mult",
    "InpWkMinTargetSpreadRatio": "wk_min_target_spread_ratio",
    "InpWkProbeDepthAtr": "wk_probe_depth_atr",
    "InpWkProbeVolumeMax": "wk_probe_volume_max",
    "InpWkProfileBins": "wk_profile_bins",
    "InpWkProfileLookback": "wk_profile_lookback",
    "InpWkProfileUpdateBars": "wk_profile_update_bars",
    "InpWkRegimeLookback": "wk_regime_lookback",
    "InpWkRewardRisk": "wk_reward_risk",
    "InpWkStopAtrBuffer": "wk_stop_atr_buffer",
    "InpWkTargetPoc": "wk_target_poc",
    "InpWkValueAreaPct": "wk_value_area_pct",
}

ENABLE_MAP = {
    "InpEnableRetest": "enable_retest",
    "InpEnableBreakout": "enable_breakout_momentum",
    "InpEnableTrend": "enable_trend_pullback",
    "InpEnableMeanReversion": "enable_mean_reversion",
    "InpEnableWyckoff": "enable_wyckoff",
}


def parse_inputs() -> dict:
    src = MQ5.read_text()
    pat = re.compile(r"^\s*input\s+(?:double|int|long|bool)\s+(\w+)\s*=\s*([-\w.]+)\s*;", re.M)
    return {n: (v == "true" if v in ("true", "false") else float(v)) for n, v in pat.findall(src)}


def test_ea_exists_and_parses():
    assert MQ5.exists()
    inputs = parse_inputs()
    assert len(inputs) > 100, f"only parsed {len(inputs)} inputs; the regex is wrong"


@pytest.mark.parametrize("mq5_name,py_field", sorted(PARAM_MAP.items()))
def test_defaults_match(mq5_name, py_field):
    inputs = parse_inputs()
    assert mq5_name in inputs, f"{mq5_name} missing from 123_M5.mq5"
    expected = getattr(Combo123Params(), py_field)
    assert float(inputs[mq5_name]) == pytest.approx(float(expected))


@pytest.mark.parametrize("mq5_name,py_field", sorted(ENABLE_MAP.items()))
def test_enable_flags_default_on(mq5_name, py_field):
    inputs = parse_inputs()
    assert inputs[mq5_name] is True
    assert getattr(Combo123Params(), py_field) is True


def test_magic_number_is_unique_across_every_ea():
    inputs = parse_inputs()
    others = {
        "XauTrend": 770577, "Anas": 770588, "XauBreak": 770599,
        "XauMicro": 770611, "XauWyck": 770622, "XauRetest": 770633,
    }
    magic = inputs["InpMagicNumber"]
    assert magic not in others.values()
    assert magic == 770644


def test_session_open_by_default():
    """Unlike XauRetest, this bot has no proven hours of its own yet -- see
    the class docstring in combo_123.py and the file header in 123_M5.mq5."""
    inputs = parse_inputs()
    assert inputs["InpSession1Start"] == 0
    assert inputs["InpSession1End"] == 24
    assert inputs["InpSession2Start"] == inputs["InpSession2End"]  # off
    assert inputs["InpSession3Start"] == inputs["InpSession3End"]  # off


def test_dispatcher_tries_retest_first_and_wyckoff_last():
    """Priority order: most-confirmed philosophy first (see the file header
    in both combo_123.py and 123_M5.mq5)."""
    src = MQ5.read_text()
    dispatcher = src[src.index("void TryEntry()"):src.index("//| RT --")]
    order = ["TryEntryRt()", "TryEntryRtImmediate()", "TryEntryBk()", "TryEntryTr()",
             "TryEntryMr()", "TryEntryWk()"]
    positions = [dispatcher.index(call) for call in order]
    assert positions == sorted(positions), "dispatch order drifted from the documented priority"


def test_rt_pending_breakout_advances_unconditionally():
    """Same invariant as XauRetest_M5.mq5 itself: AdvanceRtPendingBreakout()
    must run before the open-position branch, on every closed bar."""
    src = MQ5.read_text()
    on_tick = src[src.index("void OnTick()"):src.index("bool IsNewBar()")]
    advance_idx = on_tick.index("AdvanceRtPendingBreakout();")
    has_pos_idx = on_tick.index("HasOpenPosition()")
    assert advance_idx < has_pos_idx


def test_open_trade_tags_the_position_comment():
    src = MQ5.read_text()
    assert '"123:" + tag' in src


def test_manage_dispatches_by_position_comment():
    src = MQ5.read_text()
    manage_body = src[src.index("void ManageOpenPosition("):src.index("//| PANEL")]
    for tag, fn in (("RT", "ManageRt"), ("BK", "ManageBk"), ("TR", "ManageTr"),
                    ("MR", "ManageMr"), ("WK", "ManageWk")):
        assert f'StringFind(comment, "{tag}")' in manage_body
        assert fn in manage_body


def test_daily_limits_are_shared_across_sub_strategies():
    """One InpMaxTradesPerDay / InpMaxConsecLosses bounds the COMBINATION,
    not each sub-strategy separately -- otherwise five bots' worth of risk
    could stack behind one set of numbers that looks like one bot's."""
    src = MQ5.read_text()
    assert src.count("input int    InpMaxTradesPerDay") == 1
    assert src.count("input int    InpMaxConsecLosses") == 1


# ---------------------------------------------------------------------------
# Decision parity, one section per sub-strategy. Each mql5_entry_* below is
# adapted from that sub-strategy's own standalone parity test (same formulas,
# reading feat[...] instead of MQL5 buffers) -- see the module docstring for
# why re-deriving them here would not test anything new.
# ---------------------------------------------------------------------------

def mql5_entry_rt(i, feat, p: BreakoutRetestParams):
    a = {c: feat[c].to_numpy(float) for c in
         ("high", "low", "close", "atr", "adx", "atr_med", "htf_ema", "close_pos",
          "retest_level", "retest_side", "retest_strength", "retest_width", "cooldown_ok")}
    level, side_v = a["retest_level"][i], a["retest_side"][i]
    if side_v == 0.0 or not np.isfinite(level):
        return None
    if a["cooldown_ok"][i] < 0.5:
        return None
    atr_v, med, adx_v = a["atr"][i], a["atr_med"][i], a["adx"][i]
    close, high_v, low_v = a["close"][i], a["high"][i], a["low"][i]
    close_pos = a["close_pos"][i]
    htf_ema_v = a["htf_ema"][i]
    breakout_strength, width = a["retest_strength"][i], a["retest_width"][i]
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

    def _quality(bs, adx_v, precision, rejection):
        adx_component = (
            max(0.0, min(1.0, (adx_v - p.adx_min) / p.adx_min))
            if p.adx_min > 0.0 and np.isfinite(adx_v) else 0.5
        )
        bsc = max(0.0, min(1.0, bs)) if np.isfinite(bs) else 0.5
        return (bsc + adx_component + max(0.0, min(1.0, precision)) + max(0.0, min(1.0, rejection))) / 4.0

    tol = p.retest_tolerance_atr * atr_v
    if side_v > 0:
        retest_ok = low_v <= level + tol and close > level and close_pos >= p.retest_close_position_min
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
        quality = _quality(breakout_strength, adx_v, precision, rejection)
        rr = p.quality_min_reward_risk + quality * (p.quality_max_reward_risk - p.quality_min_reward_risk)
        tp_dist = rr * (close - sl)
        if p.use_measured_move_target and np.isfinite(width) and width > 0:
            tp_dist = max(tp_dist, width)
        risk_mult = 1.0 + quality * (p.quality_max_risk_mult - 1.0)
        return (Side.LONG, sl, close + tp_dist, risk_mult)

    retest_ok = high_v >= level - tol and close < level and (1.0 - close_pos) >= p.retest_close_position_min
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
    quality = _quality(breakout_strength, adx_v, precision, rejection)
    rr = p.quality_min_reward_risk + quality * (p.quality_max_reward_risk - p.quality_min_reward_risk)
    tp_dist = rr * (sl - close)
    if p.use_measured_move_target and np.isfinite(width) and width > 0:
        tp_dist = max(tp_dist, width)
    risk_mult = 1.0 + quality * (p.quality_max_risk_mult - 1.0)
    return (Side.SHORT, sl, close - tp_dist, risk_mult)


def mql5_entry_bk(i, feat, p: BreakoutMomentumParams):
    a = {c: feat[c].to_numpy(float) for c in
         ("open", "high", "low", "close", "atr", "adx", "atr_med",
          "range_high", "range_low", "range_width", "close_pos", "bar_range", "trend_ema")}
    if i < max(p.regime_lookback, p.trend_ema_period, p.range_lookback) + 10:
        return None
    atr_v, med, adx_v = a["atr"][i], a["atr_med"][i], a["adx"][i]
    close = a["close"][i]
    rh, rl, width = a["range_high"][i], a["range_low"][i], a["range_width"][i]
    close_pos, bar_range, trend = a["close_pos"][i], a["bar_range"][i], a["trend_ema"][i]
    if any(not np.isfinite(v) for v in (atr_v, med, rh, rl, close_pos, trend)):
        return None
    if atr_v <= 0 or med <= 0:
        return None
    if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
        return None
    if p.adx_min > 0 and (not np.isfinite(adx_v) or adx_v < p.adx_min):
        return None
    if width > p.squeeze_max_atr * atr_v:
        return None
    if bar_range < p.min_bar_range_atr * atr_v:
        return None
    margin = p.break_margin_atr * atr_v
    up = close > rh + margin and close_pos >= p.close_position_min
    down = close < rl - margin and (1.0 - close_pos) >= p.close_position_min
    if p.require_trend_alignment:
        up = up and close > trend
        down = down and close < trend
    if up == down:
        return None
    if up:
        sl = min(rh - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
        return None if sl >= close else (Side.LONG, sl)
    sl = max(rl + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
    return None if sl <= close else (Side.SHORT, sl)


def mql5_entry_tr(i, feat, p: TrendPullbackParams):
    c = feat["close"].to_numpy(float)
    o = feat["open"].to_numpy(float)
    hi = feat["high"].to_numpy(float)
    lo = feat["low"].to_numpy(float)
    atr = feat["atr"].to_numpy(float)
    adx = feat["adx"].to_numpy(float)
    rsi = feat["rsi"].to_numpy(float)
    ema_f = feat["ema_fast"].to_numpy(float)
    ema_s = feat["ema_slow"].to_numpy(float)
    ema_t = feat["ema_trend"].to_numpy(float)
    atr_med = feat["atr_med"].to_numpy(float)
    if i < max(p.regime_lookback, p.ema_trend) + 10:
        return None
    vals = (atr[i], adx[i], rsi[i], ema_f[i], ema_s[i], ema_t[i], atr_med[i])
    if any(not np.isfinite(v) for v in vals):
        return None
    atr_v = atr[i]
    if atr_v <= 0:
        return None
    med = atr_med[i]
    if med <= 0:
        return None
    if atr_v < p.atr_min_mult * med or atr_v > p.atr_max_mult * med:
        return None
    if adx[i] < p.adx_min:
        return None
    close, open_ = c[i], o[i]
    prev_hi, prev_lo = hi[i - 1], lo[i - 1]
    swing_low = min(lo[i - k] for k in range(p.swing_lookback))
    swing_high = max(hi[i - k] for k in range(p.swing_lookback))
    rsi_min = min(rsi[i - k] for k in range(p.pullback_lookback))
    rsi_max = max(rsi[i - k] for k in range(p.pullback_lookback))
    long_ok = (
        ema_f[i] > ema_s[i] and close > ema_t[i]
        and rsi_min <= p.rsi_pullback_long and rsi[i] > p.rsi_pullback_long
        and close > prev_hi and close > open_
    )
    short_ok = (
        ema_f[i] < ema_s[i] and close < ema_t[i]
        and rsi_max >= p.rsi_pullback_short and rsi[i] < p.rsi_pullback_short
        and close < prev_lo and close < open_
    )
    if long_ok == short_ok:
        return None
    if long_ok:
        sl = min(swing_low - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
        return None if sl >= close else (Side.LONG, sl)
    sl = max(swing_high + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
    return None if sl <= close else (Side.SHORT, sl)


def mql5_entry_mr(i, feat, p: MeanReversionScalperParams):
    a = {c: feat[c].to_numpy(float) for c in
         ("open", "high", "low", "close", "ema", "atr", "adx", "rsi", "atr_med",
          "swing_low", "swing_high", "htf_slope", "close_pos",
          "prior_low", "prior_high", "prior_rsi_low", "prior_rsi_high")}
    if i < max(p.regime_lookback, p.ema_period, p.htf_ema_period + p.htf_slope_lookback) + 10:
        return None
    atr_v, med, adx_v, rsi_v = a["atr"][i], a["atr_med"][i], a["adx"][i], a["rsi"][i]
    ema_v, close, open_ = a["ema"][i], a["close"][i], a["open"][i]
    swing_lo, swing_hi = a["swing_low"][i], a["swing_high"][i]
    if any(not np.isfinite(v) for v in (atr_v, med, adx_v, rsi_v, ema_v, swing_lo, swing_hi)):
        return None
    if atr_v <= 0 or med <= 0:
        return None
    if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
        return None
    if adx_v >= p.adx_max:
        return None
    stretch = (close - ema_v) / atr_v
    high_v, low_v = a["high"][i], a["low"][i]
    close_pos = a["close_pos"][i]
    if np.isfinite(close_pos):
        long_bar = close_pos >= p.close_position_min
        short_bar = (1.0 - close_pos) >= p.close_position_min
    else:
        long_bar = short_bar = False
    htf_slope = a["htf_slope"][i]
    slope_atr = htf_slope / atr_v if (np.isfinite(htf_slope) and atr_v > 0) else 0.0
    if p.htf_max_slope_atr > 0.0:
        long_htf = slope_atr >= -p.htf_max_slope_atr
        short_htf = slope_atr <= p.htf_max_slope_atr
    else:
        long_htf = short_htf = True
    if p.require_divergence:
        pl, prl = a["prior_low"][i], a["prior_rsi_low"][i]
        ph, prh = a["prior_high"][i], a["prior_rsi_high"][i]
        long_div = np.isfinite(pl) and np.isfinite(prl) and low_v <= pl and rsi_v > prl
        short_div = np.isfinite(ph) and np.isfinite(prh) and high_v >= ph and rsi_v < prh
    else:
        long_div = short_div = True
    long_ok = (stretch <= -p.stretch_atr and rsi_v <= p.rsi_oversold and close > open_
               and long_bar and long_htf and long_div)
    short_ok = (stretch >= p.stretch_atr and rsi_v >= p.rsi_overbought and close < open_
                and short_bar and short_htf and short_div)
    if long_ok == short_ok:
        return None
    if long_ok:
        sl = min(swing_lo - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
        return None if sl >= close else (Side.LONG, sl)
    sl = max(swing_hi + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
    return None if sl <= close else (Side.SHORT, sl)


def mql5_entry_wk(i, feat, p: VolumeProfileWyckoffParams):
    a = {c: feat[c].to_numpy(float) for c in
         ("high", "low", "close", "volume", "atr", "adx", "atr_med",
          "poc", "vah", "val", "vol_avg", "close_pos")}
    if i < max(p.regime_lookback, p.profile_lookback) + 10:
        return None
    atr_v, med, adx_v = a["atr"][i], a["atr_med"][i], a["adx"][i]
    poc, vah, val = a["poc"][i], a["vah"][i], a["val"][i]
    close, high_v, low_v = a["close"][i], a["high"][i], a["low"][i]
    vol, vol_avg, close_pos = a["volume"][i], a["vol_avg"][i], a["close_pos"][i]
    if any(not np.isfinite(v) for v in (atr_v, med, poc, vah, val, close_pos)):
        return None
    if atr_v <= 0 or med <= 0 or vah <= val:
        return None
    if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
        return None
    if np.isfinite(adx_v) and adx_v >= p.adx_max:
        return None
    probe = p.probe_depth_atr * atr_v
    margin = p.close_back_margin_atr * atr_v
    vol_ok = True
    if np.isfinite(vol_avg) and vol_avg > 0:
        vol_ok = vol <= p.probe_volume_max * vol_avg
    spring = (low_v <= val - probe and close >= val + margin
              and close_pos >= p.close_position_min and vol_ok)
    upthrust = (high_v >= vah + probe and close <= vah - margin
                and (1.0 - close_pos) >= p.close_position_min and vol_ok)
    if spring == upthrust:
        return None
    if spring:
        sl = min(low_v - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
        return None if sl >= close else (Side.LONG, sl)
    sl = max(high_v + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
    return None if sl <= close else (Side.SHORT, sl)


def _rr_key(sig):
    if sig is None:
        return None
    side, sl, tp, risk_mult = sig
    return (side, round(float(sl), 6), round(float(tp), 4), round(float(risk_mult), 4))


def _pair_key(sig):
    if sig is None:
        return None
    side, sl = sig
    return (side, round(float(sl), 6))


@pytest.mark.parametrize("prefix,mirror", [
    ("bk", mql5_entry_bk),
    ("tr", mql5_entry_tr),
    ("mr", mql5_entry_mr),
    ("wk", mql5_entry_wk),
])
def test_sub_strategy_decisions_are_identical(long_bars, prefix, mirror):
    combo = Combo123()
    feat = combo.prepare(long_bars)
    sub, _ = combo._subs[prefix]
    sub_feat = combo._sub_frame(feat, prefix)

    bad, signals = [], 0
    for i in range(combo.warmup, len(feat)):
        py = sub.entry(i, sub_feat)
        mq = mirror(i, sub_feat, sub.params)
        pk = None if py is None else (py.side, round(py.stop_loss, 6))
        mk = _pair_key(mq)
        if pk is not None:
            signals += 1
        if pk != mk:
            bad.append((i, feat.index[i], pk, mk))
    assert signals > 20, f"[{prefix}] only {signals} signals; too few to be a real check"
    assert not bad, f"[{prefix}] {len(bad)} bars disagree; first: {bad[:3]}"


def test_rt_decisions_are_identical(long_bars):
    combo = Combo123()
    feat = combo.prepare(long_bars)
    sub_feat = combo._sub_frame(feat, "rt")

    bad, signals = [], 0
    for i in range(combo.warmup, len(feat)):
        py = combo._rt.entry(i, sub_feat)
        mq = mql5_entry_rt(i, sub_feat, combo._rt.params)
        pk = None if py is None else (py.side, round(py.stop_loss, 6), round(py.take_profit, 4),
                                       round(float(py.meta["risk_multiplier"]), 4))
        mk = _rr_key(mq)
        if pk is not None:
            signals += 1
        if pk != mk:
            bad.append((i, feat.index[i], pk, mk))
    assert signals > 5, f"only {signals} RT signals; too few to be a real check"
    assert not bad, f"{len(bad)} bars disagree; first: {bad[:3]}"


def test_combo_entry_matches_first_firing_sub_strategy(long_bars):
    """The dispatcher itself: whichever sub-strategy fires first must be
    exactly what Combo123.entry() returns that bar."""
    combo = Combo123()
    feat = combo.prepare(long_bars)
    order = ["rt", "bk", "tr", "mr", "wk"]

    checked, agree = 0, 0
    for i in range(combo.warmup, len(feat), 3):
        combo_sig = combo.entry(i, feat)
        expected_reason = None
        for prefix in order:
            sub, _ = combo._subs[prefix]
            sig = sub.entry(i, combo._sub_frame(feat, prefix))
            if sig is not None:
                expected_reason = sig.reason
                break
        checked += 1
        got_reason = None if combo_sig is None else combo_sig.reason
        if got_reason == expected_reason:
            agree += 1
    assert checked == agree, f"{checked - agree}/{checked} bars picked the wrong sub-strategy"
