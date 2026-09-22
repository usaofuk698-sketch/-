"""XauFlash: the tick-level seconds scalper.

There is no Python twin of this EA -- the research engine works on M5 bars
and cannot see a 3-second burst -- so these checks are about the things that
must hold whatever the market does: protection lives on the server, the hold
timer runs without ticks, and the cost of a trade is covered by its target.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

MQ5 = Path(__file__).resolve().parents[1] / "mt5" / "XauFlash_S.mq5"
BAT = Path(__file__).resolve().parents[1] / "mt5" / "installer" / "Install-XauFlash.bat"


def src() -> str:
    return MQ5.read_text()


def parse_inputs() -> dict:
    pat = re.compile(r"^\s*input\s+(?:double|int|long|bool)\s+(\w+)\s*=\s*([-\w.]+)\s*;", re.M)
    return {n: (v == "true" if v in ("true", "false") else float(v)) for n, v in pat.findall(src())}


def test_magic_number_is_unique():
    others = set()
    for f in MQ5.parent.glob("*.mq5"):
        if f != MQ5:
            m = re.search(r"InpMagicNumber\s*=\s*(\d+)", f.read_text())
            others.add(int(m.group(1)))
    assert int(parse_inputs()["InpMagicNumber"]) not in others


def test_trades_really_last_seconds():
    inp = parse_inputs()
    assert 1 <= inp["InpMaxHoldSeconds"] <= 60
    assert inp["InpBurstWindowMs"] <= 10_000


def test_stop_and_target_are_sent_with_the_order():
    """A seconds scalper that relies on the terminal to close a loser is one
    frozen VPS away from an unbounded loss."""
    s = src()
    assert re.search(r"trade\.Buy\(lots, _Symbol, 0\.0, sl, tp", s)
    assert re.search(r"trade\.Sell\(lots, _Symbol, 0\.0, sl, tp", s)


def test_hold_timer_fires_without_ticks():
    s = src()
    assert "EventSetMillisecondTimer" in s
    assert re.search(r"void OnTimer\(\)\s*\{[^}]*ManageOpenPosition", s)
    assert "EventKillTimer" in s


def test_target_covers_a_typical_spread():
    """At a 0.25 spread the target must still be several spreads away."""
    inp = parse_inputs()
    assert inp["InpTakeProfit"] >= inp["InpMinTargetSpreadRatio"] * 0.25
    # and the spread filter itself (points, 2-digit feed) allows no worse than that
    assert inp["InpMaxSpreadPoints"] * 0.01 * inp["InpMinTargetSpreadRatio"] <= inp["InpTakeProfit"] * 1.5


def test_risk_is_modest_for_a_high_frequency_ea():
    inp = parse_inputs()
    assert inp["InpRiskPercent"] <= 1.0
    worst_day = inp["InpRiskPercent"] * inp["InpMaxConsecLosses"]
    assert worst_day <= 5.0
    assert 0 < inp["InpMaxDailyLossPct"] <= 5.0


def test_lots_are_floored_not_rounded():
    assert "MathFloor(lots / g_volStep) * g_volStep" in src()


def test_installer_expects_the_current_file_size():
    size = re.search(r'if not "!SRCSIZE!"=="(\d+)"', BAT.read_text()).group(1)
    assert int(size) == MQ5.stat().st_size
