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
    "InpStopAtrBuffer": "stop_atr_buffer",
    "InpMinStopAtrMult": "min_stop_atr_mult",
    "InpRewardRisk": "reward_risk",
    "InpMinTargetSpreadRatio": "min_target_spread_ratio",
    "InpBreakevenAtR": "breakeven_at_r",
    "InpBreakevenOffAtr": "breakeven_offset_atr",
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
    snap_idx = src.index("g_retestLevel = g_pendingLevel")
    advance_idx = src.index("AdvancePendingBreakout();")
    assert snap_idx < advance_idx
    # TryEntry itself must read the snapshot, never the live pending state.
    entry_start = src.index("void TryEntry()")
    entry_end = src.index("void OpenTrade(")
    try_entry_body = src[entry_start:entry_end]
    assert "g_retestLevel" in try_entry_body
    assert "g_pendingLevel" not in try_entry_body


def mql5_entry(i: int, feat, p: BreakoutRetestParams):
    """Mirror of TryEntry() in XauRetest_M5.mq5."""
    a = {c: feat[c].to_numpy(float) for c in
         ("high", "low", "close", "atr", "adx", "atr_med", "close_pos",
          "retest_level", "retest_side")}

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
        retest_ok = low_v <= level + tol and close > level and close_pos >= p.retest_close_position_min
        if not retest_ok:
            return None
        sl = min(low_v - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
        return None if sl >= close else (Side.LONG, sl)

    retest_ok = high_v >= level - tol and close < level and (1.0 - close_pos) >= p.retest_close_position_min
    if not retest_ok:
        return None
    sl = max(high_v + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
    return None if sl <= close else (Side.SHORT, sl)


def test_entry_decisions_are_identical(long_bars):
    strategy = BreakoutRetest()
    feat = strategy.prepare(long_bars)
    bad, signals = [], 0
    for i in range(strategy.warmup, len(feat)):
        py = strategy.entry(i, feat)
        mq = mql5_entry(i, feat, strategy.params)
        pk = None if py is None else (py.side, round(py.stop_loss, 6))
        mk = None if mq is None else (mq[0], round(mq[1], 6))
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
        pk = None if py is None else (py.side, round(py.stop_loss, 6))
        mk = None if mq is None else (mq[0], round(mq[1], 6))
        if pk != mk:
            mismatches += 1
    assert mismatches > 0, "a drifted retest tolerance went undetected"
