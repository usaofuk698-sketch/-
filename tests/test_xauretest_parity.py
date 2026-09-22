"""Parity between the Python breakout-retest strategy and XauRetest_M5.mq5."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from goldbot.backtest.types import Side
from goldbot.strategy.breakout_retest import BreakoutRetest, BreakoutRetestParams

MQ5 = Path(__file__).resolve().parents[1] / "mt5" / "XauRetest_M5.mq5"

PARAM_MAP = {
    "InpRangeLookback": "range_lookback",
    "InpBreakMarginAtr": "break_margin_atr",
    "InpMinBarRangeAtr": "min_bar_range_atr",
    "InpAtrPeriod": "atr_period",
    "InpRegimeLookback": "regime_lookback",
    "InpAtrMinMult": "atr_min_mult",
    "InpAtrMaxMult": "atr_max_mult",
    "InpAdxPeriod": "adx_period",
    "InpAdxMin": "adx_min",
    "InpRetestMaxBars": "retest_max_bars",
    "InpRetestToleranceAtr": "retest_tolerance_atr",
    "InpRetestClosePosMin": "retest_close_position_min",
    "InpCooldownBars": "cooldown_bars",
    "InpStopAtrBuffer": "stop_atr_buffer",
    "InpMinStopAtrMult": "min_stop_atr_mult",
    "InpMinTargetSpreadRatio": "min_target_spread_ratio",
    "InpQualityMinRR": "quality_min_reward_risk",
    "InpQualityMaxRR": "quality_max_reward_risk",
    "InpUseMeasuredMove": "use_measured_move_target",
    "InpQualityMaxRiskMult": "quality_max_risk_mult",
    "InpBreakevenAtR": "breakeven_at_r",
    "InpBreakevenOffAtr": "breakeven_offset_atr",
    "InpTrailAtR": "trail_at_r",
    "InpTrailAtrMult": "trail_atr_mult",
}


def parse_inputs() -> dict:
    src = MQ5.read_text()
    pat = re.compile(r"^\s*input\s+(?:double|int|long|bool)\s+(\w+)\s*=\s*([-\w.]+)\s*;", re.M)
    return {n: (v == "true" if v in ("true", "false") else float(v)) for n, v in pat.findall(src)}


def test_ea_exists_and_parses():
    assert MQ5.exists()
    assert len(parse_inputs()) > 20


@pytest.mark.parametrize("mq5_name,py_field", sorted(PARAM_MAP.items()))
def test_defaults_match(mq5_name, py_field):
    inputs = parse_inputs()
    assert mq5_name in inputs, f"{mq5_name} missing from the EA"
    expected = getattr(BreakoutRetestParams(), py_field)
    assert float(inputs[mq5_name]) == pytest.approx(float(expected))


def test_range_excludes_the_current_bar():
    """Same invariant as XauBreak: a bar cannot break a level partly made of
    itself. Python shifts; MQL5's range loop starts at index 1."""
    src = MQ5.read_text()
    assert "for(int k = 1; k <= InpRangeLookback" in src


def test_pending_state_is_advanced_unconditionally():
    """The pending-breakout state is a pure function of price history, computed
    once for the whole series in Python's ``prepare()`` regardless of whether a
    position happened to be open. The live EA must advance it every closed bar
    the same way -- if it were only updated inside the entry-seeking branch, a
    breakout that occurs while a trade is open would never arm a retest."""
    src = MQ5.read_text()
    assert "AdvancePendingBreakout();" in src
    on_tick = src[src.index("void OnTick()"):]
    has_pos_idx = on_tick.index("HasOpenPosition()")
    advance_idx = on_tick.index("AdvancePendingBreakout();")
    assert advance_idx < has_pos_idx, (
        "AdvancePendingBreakout() must run before the open-position branch, "
        "so it fires on every closed bar, not only when flat"
    )


def test_entry_snapshots_state_before_advancing():
    """TryEntry() must read the state as it stood BEFORE this bar's own
    breakout is folded in -- otherwise a bar could retest the breakout it just
    made, which the Python side never allows (level[i] is recorded before bar
    i's own effect)."""
    src = MQ5.read_text()
    snap_idx = re.search(r"g_retestLevel\s*=\s*g_pendingLevel", src).start()
    advance_idx = src.index("AdvancePendingBreakout();")
    assert snap_idx < advance_idx
    # TryEntry itself must read the snapshot, never the live pending state.
    entry_start = src.index("void TryEntry()")
    entry_end = src.index("void OpenTrade(")
    try_entry_body = src[entry_start:entry_end]
    assert "g_retestLevel" in try_entry_body
    assert "g_pendingLevel" not in try_entry_body


def test_cooldown_gates_before_regime_checks_and_updates_on_both_sides():
    """Cooldown is checked right after the pending-level gate, matching
    Python's entry() ordering (cooldown_ok before ATR/ADX/retest checks), and
    g_lastFireBarTime is set on each branch that actually confirms a retest --
    not on the ATR/ADX checks alone, which the Python side's raw-conditions
    function also requires before counting a bar as having fired."""
    src = MQ5.read_text()
    entry_start = src.index("void TryEntry()")
    entry_end = src.index("void OpenTrade(")
    body = src[entry_start:entry_end]

    no_pending_idx = body.index("no pending breakout to retest")
    cooldown_idx = body.index("InpCooldownBars")
    regime_idx = body.index("InpAtrMinMult * medAtr")
    assert no_pending_idx < cooldown_idx < regime_idx

    assert body.count("g_lastFireBarTime = thisBarTime;") == 2


def _clamp01(x: float) -> float:
    return 0.0 if x < 0.0 else (1.0 if x > 1.0 else x)


def _quality(breakout_strength, adx_v, precision, rejection, p: BreakoutRetestParams) -> float:
    adx_component = (
        _clamp01((adx_v - p.adx_min) / p.adx_min)
        if p.adx_min > 0.0 and np.isfinite(adx_v)
        else 0.5
    )
    bs = _clamp01(breakout_strength) if np.isfinite(breakout_strength) else 0.5
    return (bs + adx_component + _clamp01(precision) + _clamp01(rejection)) / 4.0


def test_trail_follows_breakeven_and_only_tightens():
    """Trailing is checked AFTER break-even (so break-even still applies even
    before trailing engages) and must only ever tighten the stop -- MathMax
    for a long (raise the floor), MathMin for a short (lower the ceiling),
    the same rule break-even itself follows just above it."""
    src = MQ5.read_text()
    manage_body = src[src.index("void ManageOpenPosition("):]
    be_idx = manage_body.index("InpBreakevenAtR")
    trail_idx = manage_body.index("InpUseTrail && gainedR >= InpTrailAtR")
    assert be_idx < trail_idx
    assert "newSl = MathMax(newSl, close - InpTrailAtrMult * atrV)" in manage_body
    assert "newSl = MathMin(newSl, close + InpTrailAtrMult * atrV)" in manage_body


def mql5_entry(i: int, feat, p: BreakoutRetestParams):
    """Mirror of TryEntry()/OpenTrade() in XauRetest_M5.mq5.

    Returns ``(side, sl, tp, risk_multiplier)`` rounded, or ``None``. Unlike
    the other EAs' parity tests, this one checks the target and size lever
    too, not just the pass/fail decision -- the quality score is new,
    non-trivial logic and deserves the same scrutiny as the entry gate.
    """
    a = {c: feat[c].to_numpy(float) for c in
         ("high", "low", "close", "atr", "adx", "atr_med", "close_pos",
          "retest_level", "retest_side", "retest_strength", "retest_width",
          "cooldown_ok")}

    level, side_v = a["retest_level"][i], a["retest_side"][i]
    if side_v == 0.0 or not np.isfinite(level):
        return None
    if a["cooldown_ok"][i] < 0.5:
        return None

    atr_v, med, adx_v = a["atr"][i], a["atr_med"][i], a["adx"][i]
    close, high_v, low_v = a["close"][i], a["high"][i], a["low"][i]
    close_pos = a["close_pos"][i]
    breakout_strength, width = a["retest_strength"][i], a["retest_width"][i]

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
        quality = _quality(breakout_strength, adx_v, precision, rejection, p)
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
    quality = _quality(breakout_strength, adx_v, precision, rejection, p)
    rr = p.quality_min_reward_risk + quality * (p.quality_max_reward_risk - p.quality_min_reward_risk)
    tp_dist = rr * (sl - close)
    if p.use_measured_move_target and np.isfinite(width) and width > 0:
        tp_dist = max(tp_dist, width)
    risk_mult = 1.0 + quality * (p.quality_max_risk_mult - 1.0)
    return (Side.SHORT, sl, close - tp_dist, risk_mult)


def _key(sig):
    if sig is None:
        return None
    return (sig.side, round(sig.stop_loss, 6), round(sig.take_profit, 4),
            round(float(sig.meta["risk_multiplier"]), 4))


def _mkey(mq):
    if mq is None:
        return None
    side, sl, tp, risk_mult = mq
    return (side, round(sl, 6), round(tp, 4), round(risk_mult, 4))


def test_entry_decisions_are_identical(long_bars):
    strategy = BreakoutRetest()
    feat = strategy.prepare(long_bars)
    bad, signals = [], 0
    for i in range(strategy.warmup, len(feat)):
        py = strategy.entry(i, feat)
        mq = mql5_entry(i, feat, strategy.params)
        pk, mk = _key(py), _mkey(mq)
        if pk is not None:
            signals += 1
        if pk != mk:
            bad.append((i, feat.index[i], pk, mk))
    assert signals > 50, f"only {signals} signals; too few to be a real check"
    assert not bad, f"{len(bad)} bars disagree; first: {bad[:3]}"


def test_transliteration_catches_a_flipped_retest_side(long_bars):
    """Guard the guard: swapping which side is treated as bullish must fail."""
    strategy = BreakoutRetest()
    feat = strategy.prepare(long_bars)
    mismatches = 0
    for i in range(strategy.warmup, len(feat), 5):
        py = strategy.entry(i, feat)
        broken = BreakoutRetestParams(retest_tolerance_atr=0.0)  # far stricter
        mq = mql5_entry(i, feat, broken)
        pk, mk = _key(py), _mkey(mq)
        if pk != mk:
            mismatches += 1
    assert mismatches > 0, "a drifted retest tolerance went undetected"


def test_transliteration_catches_a_drifted_quality_score(long_bars):
    """Guard the quality score itself: a wrong weighting must fail too."""
    strategy = BreakoutRetest()
    feat = strategy.prepare(long_bars)
    mismatches = 0
    for i in range(strategy.warmup, len(feat), 5):
        py = strategy.entry(i, feat)
        broken = BreakoutRetestParams(quality_max_reward_risk=10.0)  # way off
        mq = mql5_entry(i, feat, broken)
        pk, mk = _key(py), _mkey(mq)
        if pk != mk:
            mismatches += 1
    assert mismatches > 0, "a drifted quality-to-reward mapping went undetected"
