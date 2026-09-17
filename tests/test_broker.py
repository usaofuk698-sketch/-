"""Fill-model tests. These pin the pessimistic assumptions in place so a later
'optimisation' cannot quietly restore the flattering behaviour."""
from __future__ import annotations

import pandas as pd
import pytest

from goldbot.backtest.broker import SimBroker, SpreadModel
from goldbot.backtest.types import ExitReason, Position, Side
from goldbot.contract import XAUUSD


def _broker(slip=0.0, spread=0.20):
    return SimBroker(
        contract=XAUUSD,
        spread_model=SpreadModel(base=spread, hour_multipliers={}, rollover_hour=None),
        slippage=slip,
    )


def _pos(side, entry, sl, tp, lots=0.10):
    return Position(
        side=side, lots=lots, entry_price=entry, entry_time=pd.Timestamp("2024-01-01 10:00"),
        stop_loss=sl, take_profit=tp, entry_bar=0, initial_risk=abs(entry - sl),
    )


def test_long_buys_the_ask_and_pays_slippage():
    b = _broker(slip=0.05, spread=0.20)
    assert b.entry_fill(Side.LONG, 2000.00, 0.20) == pytest.approx(2000.25)


def test_short_sells_the_bid_and_pays_slippage():
    b = _broker(slip=0.05, spread=0.20)
    assert b.entry_fill(Side.SHORT, 2000.00, 0.20) == pytest.approx(1999.95)


def test_ambiguous_bar_resolves_to_the_stop_not_the_target():
    """The bar's range covers both levels. OHLC cannot say which came first, so
    the loss is taken. Assuming the target here is the classic way a losing
    system backtests as a winner."""
    b = _broker()
    pos = _pos(Side.LONG, 2000.0, 1995.0, 2010.0)
    price, reason = b.check_exit(pos, o=2000.0, h=2012.0, l=1994.0, c=2005.0, spread=0.20)
    assert reason is ExitReason.STOP_LOSS
    assert price == pytest.approx(1995.0)


def test_gap_through_stop_fills_at_the_open_not_the_stop():
    b = _broker()
    pos = _pos(Side.LONG, 2000.0, 1995.0, 2010.0)
    price, reason = b.check_exit(pos, o=1990.0, h=1992.0, l=1985.0, c=1988.0, spread=0.20)
    assert reason is ExitReason.STOP_LOSS
    assert price == pytest.approx(1990.0), "a gap must fill at the gap, not the level"


def test_stop_takes_slippage_but_target_does_not():
    """A stop becomes a market order when triggered; a target is a limit."""
    b = _broker(slip=0.10)
    pos = _pos(Side.LONG, 2000.0, 1995.0, 2010.0)
    sl_price, _ = b.check_exit(pos, o=2000.0, h=2001.0, l=1994.0, c=1996.0, spread=0.20)
    assert sl_price == pytest.approx(1994.90)
    tp_price, reason = b.check_exit(pos, o=2000.0, h=2011.0, l=1999.0, c=2010.5, spread=0.20)
    assert reason is ExitReason.TAKE_PROFIT
    assert tp_price == pytest.approx(2010.0)


def test_short_exits_are_measured_on_the_ask():
    """A short closes by buying, so its stop is hit `spread` earlier than the
    raw (bid) high suggests. Ignoring this makes every short look better."""
    b = _broker(slip=0.0, spread=0.50)
    pos = _pos(Side.SHORT, 2000.0, 2005.0, 1990.0)
    # bid high 2004.8 -> ask high 2005.3, which is through the 2005.0 stop.
    out = b.check_exit(pos, o=2000.0, h=2004.8, l=1999.0, c=2004.0, spread=0.50)
    assert out is not None and out[1] is ExitReason.STOP_LOSS

    b0 = _broker(slip=0.0, spread=0.0)
    assert b0.check_exit(pos, o=2000.0, h=2004.8, l=1999.0, c=2004.0, spread=0.0) is None


def test_pnl_uses_contract_size():
    b = _broker()
    pos = _pos(Side.LONG, 2000.0, 1995.0, 2010.0, lots=0.10)
    assert b.gross_pnl(pos, 2010.0) == pytest.approx(0.10 * 100 * 10.0)  # $100


def test_short_pnl_is_inverted():
    b = _broker()
    pos = _pos(Side.SHORT, 2000.0, 2005.0, 1990.0, lots=0.10)
    assert b.gross_pnl(pos, 1990.0) == pytest.approx(100.0)


def test_spread_widens_at_rollover():
    sm = SpreadModel.default_xauusd()
    assert sm.spread_at(0) > sm.spread_at(13) * 5
    assert sm.spread_at(13) < sm.spread_at(2), "NY overlap must be tighter than Asia"
