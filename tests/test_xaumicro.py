"""XauMicro: XauBreak's strategy with a 100 USD account's risk settings.

The strategy itself is XauBreak's and is covered by test_xaubreak_parity. What
is specific here is whether a small account can actually afford the setups, so
that is what these check.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from goldbot import config, metrics
from goldbot.backtest.engine import BacktestEngine
from goldbot.risk import RiskManager

MQ5 = Path(__file__).resolve().parents[1] / "mt5" / "XauMicro_M5.mq5"

# XAUUSDm as the EA reports it: 0.01 lot loses exactly the stop distance in USD
MIN_LOT, CONTRACT = 0.01, 100.0


def parse_inputs() -> dict:
    src = MQ5.read_text()
    pat = re.compile(r"^\s*input\s+(?:double|int|long|bool)\s+(\w+)\s*=\s*([-\w.]+)\s*;", re.M)
    return {n: (v == "true" if v in ("true", "false") else float(v)) for n, v in pat.findall(src)}


def test_risk_settings_suit_a_small_account():
    """At 1% the budget is 1.00 against a 0.29 cost -- 29% of every trade's
    risk. 2% is the smallest risk at which 100 USD can trade this at all."""
    inp = parse_inputs()
    assert inp["InpRiskPercent"] == pytest.approx(2.0)
    budget = 100.0 * inp["InpRiskPercent"] / 100.0
    assert 0.29 / budget < 0.20, "cost share must stay under 20% of risk"


def test_drawdown_from_a_losing_streak_is_survivable():
    inp = parse_inputs()
    left = 100.0 * (1 - inp["InpRiskPercent"] / 100.0) ** inp["InpMaxConsecLosses"]
    assert left > 85.0, f"streak limit allows too deep a hole: {100 - left:.0f}%"


def test_daily_loss_limit_is_about_three_trades():
    inp = parse_inputs()
    assert inp["InpMaxDailyLossPct"] / inp["InpRiskPercent"] == pytest.approx(3.0, abs=0.5)


def test_max_stop_rejects_what_the_account_cannot_size():
    """Without this the EA would find setups it can only take at below the
    minimum lot, and refuse them one at a time with no explanation."""
    inp = parse_inputs()
    budget = 100.0 * inp["InpRiskPercent"] / 100.0
    assert inp["InpMaxStopDistance"] <= budget * 1.3


def test_risk_steps_down_as_the_account_grows():
    """2% is a floor forced by account size, not a level to keep on the way up."""
    inp = parse_inputs()
    assert inp["InpReduceRiskAboveEquity"] > 0
    assert inp["InpReducedRiskPercent"] < inp["InpRiskPercent"]
    src = MQ5.read_text()
    assert "EffectiveRiskPercent" in src
    assert "riskCash = equity * EffectiveRiskPercent(equity)" in src


def test_magic_number_is_unique():
    others = {770577, 770588, 770599}
    assert int(parse_inputs()["InpMagicNumber"]) not in others


def test_most_signals_are_affordable_at_this_risk(long_bars):
    """The point of choosing the breakout strategy for a small account: its
    stops are tight enough that a 2.00 budget covers most of its setups."""
    cfg = config.load("config/breakout.yaml")
    cfg.initial_capital = 2000.0
    eng = BacktestEngine(
        cfg.build_strategy(), RiskManager(cfg.risk, cfg.contract, 2000.0),
        cfg.build_broker(), cfg.contract, 2000.0, max_bars_in_trade=36,
    )
    tr = eng.run(long_bars).trades
    assert not tr.empty

    lots = tr["lots"].to_numpy(float)
    r = tr["r_multiple"].to_numpy(float)
    pnl = tr["net_pnl"].to_numpy(float)
    ok = np.abs(r) > 1e-9
    stops = np.abs(pnl[ok] / (r[ok] * lots[ok] * CONTRACT))

    budget = 100.0 * parse_inputs()["InpRiskPercent"] / 100.0
    affordable = (stops <= budget).mean()
    assert affordable > 0.80, f"only {affordable:.0%} of setups fit a 100 USD account"
