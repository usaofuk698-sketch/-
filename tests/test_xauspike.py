"""XauSpike: two-core spike catcher.

No Python twin exists -- the research engine works on M5 bars and cannot see
a spike that lasts seconds -- so these check the structure that must hold:
the exit profile copied from the reference report, server-side protection,
stops that only tighten, and a per-tick cost that does not grow with the
window length.
"""
from __future__ import annotations

import re
from pathlib import Path

MQ5 = Path(__file__).resolve().parents[1] / "mt5" / "XauSpike2.mq5"
BAT = Path(__file__).resolve().parents[1] / "mt5" / "installer" / "Install-XauSpike2.bat"


def src() -> str:
    return MQ5.read_text()


def inputs() -> dict:
    pat = re.compile(r"^\s*input\s+(?:double|int|long|bool)\s+(\w+)\s*=\s*([-\w.]+)\s*;", re.M)
    return {n: (v == "true" if v in ("true", "false") else float(v)) for n, v in pat.findall(src())}


def test_magic_numbers_are_unique_across_all_eas():
    mine = {int(inputs()["InpA_Magic"]), int(inputs()["InpB_Magic"])}
    assert len(mine) == 2
    for f in MQ5.parent.glob("*.mq5"):
        if f == MQ5:
            continue
        for m in re.findall(r"Magic\w*\s*=\s*(\d+)", f.read_text()):
            assert int(m) not in mine, f"{f.name} uses {m}"


def test_core_a_matches_reference_exit_profile():
    """Reference core A: SL 5, far TP, trailing stop; smallest win ~0.5."""
    i = inputs()
    assert i["InpA_StopLoss"] == 5.0
    assert i["InpA_TakeProfit"] >= 10 * i["InpA_StopLoss"]
    assert i["InpA_TrailStart"] > i["InpA_TrailDistance"] > 0
    assert 0.3 <= i["InpA_TrailStart"] - i["InpA_TrailDistance"] <= 1.0


def test_core_b_matches_reference_exit_profile():
    """Reference core B: SL 15, exits near +15 or at a +1.5 lock."""
    i = inputs()
    assert i["InpB_StopLoss"] == 15.0
    assert i["InpB_TakeProfit"] == 15.0
    assert i["InpB_LockProfit"] == 1.5
    assert i["InpB_LockTrigger"] > i["InpB_LockProfit"]


def test_stop_and_target_are_sent_with_the_order():
    s = src()
    assert "trade.Buy(lots, _Symbol, 0.0, sl, tp, comment)" in s
    assert "trade.Sell(lots, _Symbol, 0.0, sl, tp, comment)" in s


def test_stops_only_tighten():
    s = src()
    assert "MathMax(newSl, trailSl)" in s and "MathMin(newSl, trailSl)" in s
    assert "MathMax(newSl, lockSl)" in s and "MathMin(newSl, lockSl)" in s
    assert "if(!better) return;" in s


def test_spike_window_is_incremental():
    """A 60 s window walked on every tick is O(window) per tick -- too slow
    over tens of millions of tester ticks."""
    body = re.search(r"int DetectSpike\(.*?\n  \}", src(), re.S).group(0)
    assert "for(" not in body
    assert "g_winUps" in body and "TrimWindow" in body


def test_struct_is_simple():
    """Structs holding strings are not simple in MQL5; the EA copies CoreCfg."""
    struct = re.search(r"struct CoreCfg\s*\{(.*?)\};", src(), re.S).group(1)
    assert "string" not in struct


def test_every_skip_reason_is_named_and_counted():
    s = src()
    enum = re.search(r"enum ENUM_BLOCK\s*\{([^}]*)\}", s).group(1)
    members = [m.strip().split("=")[0].strip() for m in enum.split(",") if m.strip()]
    assert members[-1] == "BLK_COUNT"
    n = len(members) - 1
    names = re.search(r"g_blockName\[(\d+)\]\s*=\s*\{([^}]*)\}", s)
    assert int(names.group(1)) == n
    assert len(re.findall(r'"[^"]*"', names.group(2))) == n
    assert f"g_blockCount[2][{n}]" in s
    for m in members[:-1]:
        assert f"Block(i, {m})" in s, f"{m} is never counted"


def test_lots_are_floored():
    assert "MathFloor(lots / g_volStep) * g_volStep" in src()


def test_installer_expects_the_current_file_size():
    size = re.search(r'if not "!SRCSIZE!"=="(\d+)"', BAT.read_text()).group(1)
    assert int(size) == MQ5.stat().st_size


def test_entry_quality_filters_are_on_by_default():
    i = inputs()
    for core in "AB":
        assert i[f"Inp{core}_SpikeRatio"] > 1.0
        assert 0 < i[f"Inp{core}_FreshShare"] < 1.0
        assert i[f"Inp{core}_NoFollowSeconds"] > 0


def test_no_follow_exit_cuts_before_the_stop():
    """The quick exit only helps if it fires before the stop would."""
    i = inputs()
    for core in "AB":
        assert i[f"Inp{core}_NoFollowProfit"] < i[f"Inp{core}_StopLoss"]
    # core A must give a spike less time than the reference's median hold
    # of a winner is allowed to run (16 s) plus a margin, not minutes
    assert i["InpA_NoFollowSeconds"] <= 60


def test_peak_is_reset_for_each_new_position():
    s = src()
    assert "if(g_peakTicket[i] != ticket)" in s


def test_core_b_is_off_by_default():
    """Core B lost in each of July, August and September 2026."""
    assert inputs()["InpB_Enable"] is False
    assert inputs()["InpA_Enable"] is True


def test_skip_hours_default_is_empty_and_checked():
    s = src()
    assert re.search(r'input string InpSkipHours\s*=\s*"";', s)
    assert "if(g_skipHour[h])" in s


def test_performance_brake_scales_risk_not_fixed_lots():
    s = src()
    i = inputs()
    assert i["InpPerfTrades"] >= 5
    assert 0 < i["InpPerfRiskMult"] < 1
    assert "c.riskPct * g_perfMult[i]" in s
    # measured per ounce, so compounding lot sizes do not skew it
    assert "money / (vol * perOzPerLot)" in s
    # needs a full window before judging
    assert "g_perfN[c] >= InpPerfTrades" in s


TEST_COPY = MQ5.parent / "XauSpike_Test.mq5"


def test_test_copy_differs_only_in_lots_magic_and_labels():
    """The test copy must trade the same logic as XauSpike, or its report
    says nothing about the EA that will actually run live."""
    live = src().splitlines()
    test = TEST_COPY.read_text().splitlines()
    allowed = ("FixedLots", "Magic ", "XauSpike", "TEST COPY", "LIVE chart", "if(!tester)",
               "Identical logic", "cores, so every", "magic numbers. For", "account.", "//|")
    import difflib
    for line in difflib.unified_diff(live, test, lineterm="", n=0):
        if line.startswith(("---", "+++", "@@")):
            continue
        body = line[1:].strip()
        if not body:
            continue
        assert any(a in body for a in allowed), f"unexpected difference: {line}"
    t = TEST_COPY.read_text()
    assert "InpA_FixedLots        = 0.01;" in t and "InpB_FixedLots        = 0.01;" in t


def test_cooldown_starts_when_a_position_disappears():
    """In the tester the stop-out and the next tick's entry could share a
    second, before OnTradeTransaction had started the cooldown."""
    s = src()
    assert "if(!has && g_hadPos[i])" in s
    assert "g_hadPos[i] = SelectCorePosition(g_core[i]);" in s


def test_test_copy_is_up_to_date():
    import subprocess, sys
    before = TEST_COPY.read_text()
    subprocess.run([sys.executable, str(MQ5.parents[1] / "tools" / "make_spike_test.py")],
                   check=True, capture_output=True)
    assert TEST_COPY.read_text() == before, "run tools/make_spike_test.py"
