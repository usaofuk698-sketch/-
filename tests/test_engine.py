"""Engine sequencing tests.

The engine's whole claim to honesty is that a signal formed on a closed bar is
filled at the *next* bar's open, and that stops set after a bar closes cannot
affect that bar. These tests pin that down with hand-built data where the
correct answer is known exactly.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from goldbot.backtest.broker import SimBroker, SpreadModel
from goldbot.backtest.engine import BacktestEngine
from goldbot.backtest.types import ExitReason, Position, Side, Signal
from goldbot.contract import XAUUSD
from goldbot.risk import RiskConfig, RiskManager, SessionWindow
from goldbot.strategy.base import Strategy, StrategyParams


def _frame(rows, start="2024-01-03 10:00"):
    idx = pd.date_range(start=start, periods=len(rows), freq="5min")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=idx).assign(
        volume=100.0
    )


class _P(StrategyParams):
    pass


class SignalOnBar(Strategy):
    """Emits one long signal on a nominated bar and never again."""

    name = "signal_on_bar"

    def __init__(self, bar: int, sl: float, tp: float | None):
        self.params = _P()
        self.bar, self.sl, self.tp = bar, sl, tp

    @property
    def warmup(self) -> int:
        return 1

    def prepare(self, df):
        return df.copy()

    def entry(self, i, feat):
        if i == self.bar:
            return Signal(side=Side.LONG, stop_loss=self.sl, take_profit=self.tp)
        return None


def _engine(strategy, capital=100_000.0, spread=0.0, slip=0.0, **risk_kw):
    cfg = RiskConfig(
        sessions=(SessionWindow(0, 24),),
        flat_by_hour=None,
        no_new_trades_after_hour=None,
        min_stop_distance=0.5,
        max_stop_distance=100.0,
        **risk_kw,
    )
    broker = SimBroker(
        contract=XAUUSD,
        spread_model=SpreadModel(base=spread, hour_multipliers={}, rollover_hour=None),
        slippage=slip,
    )
    return BacktestEngine(
        strategy, RiskManager(cfg, XAUUSD, capital), broker, XAUUSD, capital,
        max_bars_in_trade=None,
    )


def test_entry_fills_on_the_next_bar_open_not_the_signal_bar():
    df = _frame([
        [2000, 2001, 1999, 2000],   # 0
        [2000, 2001, 1999, 2000],   # 1  <- signal forms here
        [2005, 2006, 2004, 2005],   # 2  <- must fill at THIS open (2005)
        [2010, 2012, 2009, 2011],   # 3
        [2011, 2013, 2010, 2012],   # 4
    ])
    res = _engine(SignalOnBar(bar=1, sl=1990.0, tp=2050.0)).run(df)
    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert t["entry_time"] == df.index[2], "filled on the signal bar = look-ahead"
    assert t["entry_price"] == pytest.approx(2005.0)


def test_entry_price_includes_spread_and_slippage():
    df = _frame([[2000, 2001, 1999, 2000]] * 2 + [[2005, 2006, 2004, 2005]] * 3)
    res = _engine(SignalOnBar(1, 1990.0, 2050.0), spread=0.30, slip=0.05).run(df)
    assert res.trades.iloc[0]["entry_price"] == pytest.approx(2005.35)


def test_trade_is_skipped_when_the_open_gaps_through_its_stop():
    df = _frame([
        [2000, 2001, 1999, 2000],
        [2000, 2001, 1999, 2000],   # signal, stop at 1995
        [1990, 1991, 1988, 1989],   # opens below the stop -> already dead
        [1990, 1991, 1988, 1989],
    ])
    res = _engine(SignalOnBar(1, 1995.0, 2050.0)).run(df)
    assert res.trades.empty
    assert res.blocked["gapped_through_stop"] == 1


def test_stop_on_the_entry_bar_is_honoured():
    """A position opened at this bar's open is still exposed to this bar's range."""
    df = _frame([
        [2000, 2001, 1999, 2000],
        [2000, 2001, 1999, 2000],
        [2000, 2001, 1990, 1992],   # entry bar, trades down through 1995
        [1992, 1993, 1991, 1992],
    ])
    res = _engine(SignalOnBar(1, 1995.0, 2050.0)).run(df)
    assert len(res.trades) == 1
    assert res.trades.iloc[0]["exit_reason"] == ExitReason.STOP_LOSS.value


def test_pnl_and_r_multiple_are_consistent():
    df = _frame([
        [2000, 2001, 1999, 2000],
        [2000, 2001, 1999, 2000],
        [2000, 2001, 1999, 2000],   # entry at 2000, stop 1990 -> risk $10/oz
        [2000, 2021, 1999, 2020],   # target 2020 hit
        [2020, 2021, 2019, 2020],
    ])
    res = _engine(SignalOnBar(1, 1990.0, 2020.0)).run(df)
    t = res.trades.iloc[0]
    assert t["exit_reason"] == ExitReason.TAKE_PROFIT.value
    expected = (2020.0 - 2000.0) * t["lots"] * XAUUSD.contract_size
    assert t["gross_pnl"] == pytest.approx(expected)
    assert t["r_multiple"] == pytest.approx(2.0, abs=0.01)  # 20 gain / 10 risk


class TighteningStrategy(SignalOnBar):
    """Tries to move the stop the wrong way; the engine must refuse."""

    def manage(self, i, feat, pos: Position):
        return pos.stop_loss - 50.0, pos.take_profit  # widening a long's stop


def test_engine_refuses_to_widen_a_stop():
    df = _frame([[2000, 2001, 1999, 2000]] * 3 + [[2000, 2001, 1960, 1965]] * 2)
    res = _engine(TighteningStrategy(1, 1995.0, 2100.0)).run(df)
    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert t["exit_reason"] == ExitReason.STOP_LOSS.value
    assert t["exit_price"] == pytest.approx(1995.0), "stop must not have moved down"


def test_equity_curve_matches_realised_trades():
    df = _frame([[2000, 2001, 1999, 2000]] * 3 + [[2000, 2031, 1999, 2030]] * 2)
    res = _engine(SignalOnBar(1, 1990.0, 2020.0), capital=100_000.0).run(df)
    net = float(res.trades["net_pnl"].sum())
    assert res.final_equity == pytest.approx(100_000.0 + net, abs=1e-6)


def test_rejects_malformed_input():
    eng = _engine(SignalOnBar(1, 1990.0, 2020.0))
    bad = _frame([[2000, 1990, 1999, 2000]] * 5)  # high < low
    with pytest.raises(ValueError, match="high < low"):
        eng.run(bad)
    unsorted = _frame([[2000, 2001, 1999, 2000]] * 5).iloc[::-1]
    with pytest.raises(ValueError, match="sorted"):
        eng.run(unsorted)
    with pytest.raises(ValueError, match="missing OHLC"):
        eng.run(pd.DataFrame({"close": [1, 2, 3]}, index=pd.date_range("2024-01-01", periods=3)))


def test_session_close_flattens_open_risk():
    idx = pd.date_range("2024-01-03 15:00", periods=8, freq="30min")
    df = pd.DataFrame(
        [[2000, 2001, 1999, 2000]] * 8, columns=["open", "high", "low", "close"], index=idx
    ).assign(volume=100.0)
    cfg = RiskConfig(
        sessions=(SessionWindow(0, 24),), flat_by_hour=17, no_new_trades_after_hour=None,
        min_stop_distance=0.5, max_stop_distance=100.0,
    )
    broker = SimBroker(XAUUSD, SpreadModel(0.0, {}, None), 0.0)
    # Bar 1, not bar 0: the engine's loop starts at `warmup`, so bar 0 is
    # never offered to the strategy at all.
    eng = BacktestEngine(
        SignalOnBar(1, 1990.0, 2100.0), RiskManager(cfg, XAUUSD, 100_000.0),
        broker, XAUUSD, 100_000.0, max_bars_in_trade=None,
    )
    res = eng.run(df)
    assert len(res.trades) == 1
    t = res.trades.iloc[0]
    assert t["exit_reason"] == ExitReason.SESSION_CLOSE.value
    assert pd.Timestamp(t["exit_time"]).hour == 17


def test_wide_spread_blocks_the_entry():
    """Mirrors the EA's spread gate: a quote too wide to trade is skipped."""
    df = _frame([[2000, 2001, 1999, 2000]] * 5)
    cfg = RiskConfig(
        sessions=(SessionWindow(0, 24),), flat_by_hour=None,
        no_new_trades_after_hour=None, min_stop_distance=0.5,
        max_stop_distance=100.0, max_spread=0.50,
    )
    broker = SimBroker(XAUUSD, SpreadModel(base=2.00, hour_multipliers={}, rollover_hour=None), 0.0)
    eng = BacktestEngine(
        SignalOnBar(1, 1990.0, 2050.0), RiskManager(cfg, XAUUSD, 100_000.0),
        broker, XAUUSD, 100_000.0, max_bars_in_trade=None,
    )
    res = eng.run(df)
    assert res.trades.empty
    assert res.blocked["spread_too_wide"] == 1


def test_normal_spread_still_trades():
    df = _frame([[2000, 2001, 1999, 2000]] * 5)
    cfg = RiskConfig(
        sessions=(SessionWindow(0, 24),), flat_by_hour=None,
        no_new_trades_after_hour=None, min_stop_distance=0.5,
        max_stop_distance=100.0, max_spread=0.50,
    )
    broker = SimBroker(XAUUSD, SpreadModel(base=0.20, hour_multipliers={}, rollover_hour=None), 0.0)
    eng = BacktestEngine(
        SignalOnBar(1, 1990.0, 2050.0), RiskManager(cfg, XAUUSD, 100_000.0),
        broker, XAUUSD, 100_000.0, max_bars_in_trade=None,
    )
    assert len(eng.run(df).trades) == 1
