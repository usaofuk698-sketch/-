from __future__ import annotations

import pandas as pd
import pytest

from goldbot.contract import XAUUSD
from goldbot.risk import RiskConfig, RiskManager, SessionWindow


def _rm(**kw):
    return RiskManager(RiskConfig(**kw), XAUUSD, 10_000.0)


def test_size_never_exceeds_the_risk_budget():
    """Lots are floored, never rounded up: rounding up would silently risk more
    than authorised on every trade."""
    rm = _rm(risk_per_trade_pct=1.0)
    for stop_distance in (1.0, 2.5, 3.3, 7.77, 12.5):
        lots, why = rm.size(10_000.0, 2000.0, 2000.0 - stop_distance)
        assert why == ""
        realised_risk = lots * XAUUSD.contract_size * stop_distance
        assert realised_risk <= 100.0 + 1e-9, stop_distance


def test_absurd_stops_are_refused():
    rm = _rm()
    assert rm.size(10_000, 2000.0, 1999.9)[1] == "stop_too_tight"
    assert rm.size(10_000, 2000.0, 1900.0)[1] == "stop_too_wide"


def test_sizing_scales_with_equity_when_compounding():
    rm = _rm(risk_per_trade_pct=1.0, compounding=True)
    small, _ = rm.size(10_000, 2000.0, 1995.0)
    big, _ = rm.size(100_000, 2000.0, 1995.0)
    assert big == pytest.approx(small * 10, rel=0.02)


def test_fixed_sizing_ignores_equity():
    rm = _rm(risk_per_trade_pct=1.0, compounding=False)
    assert rm.size(10_000, 2000.0, 1995.0)[0] == rm.size(50_000, 2000.0, 1995.0)[0]


def test_daily_loss_limit_halts_trading():
    rm = _rm(max_daily_loss_pct=2.0, max_trades_per_day=99)
    ts = pd.Timestamp("2024-01-03 10:00")
    rm.on_bar(ts, 10_000.0)
    assert rm.may_open(ts, 0)[0]
    rm.on_trade_closed(-150.0, 9_850.0)
    assert rm.may_open(ts, 0)[0], "one loss inside the limit must not halt"
    rm.on_trade_closed(-100.0, 9_750.0)  # cumulative -250 = -2.5% > 2% limit
    ok, why = rm.may_open(ts, 0)
    assert not ok and why == "daily_loss_limit"


def test_halt_clears_on_the_next_day():
    rm = _rm(max_daily_loss_pct=1.0)
    d1 = pd.Timestamp("2024-01-03 10:00")
    rm.on_bar(d1, 10_000.0)
    rm.on_trade_closed(-500.0, 9_500.0)
    assert not rm.may_open(d1, 0)[0]
    rm.on_bar(pd.Timestamp("2024-01-04 10:00"), 9_500.0)
    assert rm.may_open(pd.Timestamp("2024-01-04 10:00"), 0)[0]


def test_consecutive_loss_breaker():
    rm = _rm(max_consecutive_losses=3, max_daily_loss_pct=99.0, max_trades_per_day=99)
    ts = pd.Timestamp("2024-01-03 10:00")
    rm.on_bar(ts, 10_000.0)
    for _ in range(3):
        rm.on_trade_closed(-10.0, 9_990.0)
    assert not rm.may_open(ts, 0)[0]


def test_a_win_resets_the_loss_streak():
    rm = _rm(max_consecutive_losses=3, max_daily_loss_pct=99.0, max_trades_per_day=99)
    ts = pd.Timestamp("2024-01-03 10:00")
    rm.on_bar(ts, 10_000.0)
    rm.on_trade_closed(-10.0, 9_990.0)
    rm.on_trade_closed(-10.0, 9_980.0)
    rm.on_trade_closed(+30.0, 10_010.0)
    rm.on_trade_closed(-10.0, 10_000.0)
    assert rm.may_open(ts, 0)[0]


def test_weekend_and_session_filters():
    rm = _rm(sessions=(SessionWindow(7, 16),))
    assert not rm.may_open(pd.Timestamp("2024-01-06 10:00"), 0)[0]   # Saturday
    assert not rm.may_open(pd.Timestamp("2024-01-03 03:00"), 0)[0]   # before session
    rm.on_bar(pd.Timestamp("2024-01-03 10:00"), 10_000.0)
    assert rm.may_open(pd.Timestamp("2024-01-03 10:00"), 0)[0]


def test_concurrent_position_cap():
    rm = _rm()
    ts = pd.Timestamp("2024-01-03 10:00")
    rm.on_bar(ts, 10_000.0)
    assert not rm.may_open(ts, open_positions=1)[0]


def test_spread_filter_mirrors_the_ea():
    """The EA refuses entries above InpMaxSpreadPoints. If the backtest does
    not, it takes trades the live bot never would."""
    rm = _rm(max_spread=5.0)
    assert rm.spread_ok(0.25)
    assert rm.spread_ok(5.0), "the limit itself must be allowed"
    assert not rm.spread_ok(5.01)


def test_spread_filter_is_off_by_default():
    assert _rm().spread_ok(999.0), "no limit configured means no filtering"
