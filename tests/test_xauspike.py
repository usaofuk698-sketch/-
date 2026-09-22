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

MQ5 = Path(__file__).resolve().parents[1] / "mt5" / "XauSpike.mq5"
BAT = Path(__file__).resolve().parents[1] / "mt5" / "installer" / "Install-XauSpike.bat"


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
