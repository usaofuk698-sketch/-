"""Live-loop tests, centred on the rule that makes live match the backtest:
never act on the bar that is still forming."""
from __future__ import annotations

import pandas as pd
import pytest

from goldbot import config as cfg_mod
from goldbot.backtest.types import Side, Signal
from goldbot.data.synthetic import generate
from goldbot.live.runner import Executor, LiveRunner, MarketFeed, PaperExecutor
from goldbot.strategy.base import Strategy, StrategyParams


class ListFeed(MarketFeed):
    """Serves a growing prefix of a fixed frame, like a real feed would."""

    def __init__(self, df: pd.DataFrame, upto: int):
        self.df, self.upto = df, upto

    def fetch(self, symbol, timeframe, count):
        return self.df.iloc[max(0, self.upto - count):self.upto]


@pytest.fixture(scope="module")
def bars_df():
    return generate(start="2023-01-02", end="2023-04-01", seed=8)


def test_closed_bars_drops_the_forming_bar(bars_df):
    feed = ListFeed(bars_df, upto=500)
    served = feed.fetch("XAUUSD", "5min", 100)
    closed = feed.closed_bars("XAUUSD", "5min", 100)
    assert closed.index[-1] < served.index[-1]
    assert len(closed) == 100


def test_closed_bars_never_returns_the_live_bar(bars_df):
    """Whatever the requested count, the newest row the feed holds is excluded."""
    for upto in (300, 600, 900):
        feed = ListFeed(bars_df, upto=upto)
        closed = feed.closed_bars("XAUUSD", "5min", 50)
        assert closed.index[-1] == bars_df.index[upto - 2]


class AlwaysLong(Strategy):
    name = "always_long"

    def __init__(self):
        self.params = StrategyParams()
        self.calls: list[pd.Timestamp] = []

    @property
    def warmup(self) -> int:
        return 5

    def prepare(self, df):
        return df.copy()

    def entry(self, i, feat):
        self.calls.append(feat.index[i])
        close = float(feat["close"].iloc[i])
        return Signal(side=Side.LONG, stop_loss=close - 3.0, take_profit=close + 6.0)


def _cfg(tmp_path) -> cfg_mod.AppConfig:
    cfg = cfg_mod.load("config/default.yaml")
    cfg.risk.sessions = tuple(cfg.risk.sessions)
    return cfg


def _runner(cfg, feed, strategy):
    r = LiveRunner(cfg=cfg, feed=feed, executor=PaperExecutor(starting_equity=10_000.0),
                   history_bars=400)
    r.strategy = strategy  # swap in the probe after construction
    return r


class NeverSignals(AlwaysLong):
    """Records every bar it is asked about, but never takes a position.

    Needed because the runner deliberately skips entry evaluation while a
    position is open -- a probe that opens on its first call can only ever be
    asked once, which says nothing about per-bar cadence.
    """

    name = "never_signals"

    def entry(self, i, feat):
        self.calls.append(feat.index[i])
        return None


def test_runner_evaluates_each_closed_bar_exactly_once(bars_df, tmp_path):
    cfg = _cfg(tmp_path)
    cfg.risk.sessions = (__import__("goldbot.risk", fromlist=["SessionWindow"]).SessionWindow(0, 24),)
    cfg.risk.no_new_trades_after_hour = None
    cfg.risk.flat_by_hour = None

    feed = ListFeed(bars_df, upto=400)
    strat = NeverSignals()
    runner = _runner(cfg, feed, strat)

    assert runner.step() is True
    first = len(strat.calls)
    assert runner.step() is False, "same bar must not be processed twice"
    assert len(strat.calls) == first

    feed.upto = 401  # a new bar closes
    assert runner.step() is True
    assert len(strat.calls) == first + 1
    assert strat.calls[-1] == bars_df.index[399], "must evaluate the newly CLOSED bar"


def test_runner_stops_looking_for_entries_while_in_a_position(bars_df, tmp_path):
    from goldbot.risk import SessionWindow

    cfg = _cfg(tmp_path)
    cfg.risk.sessions = (SessionWindow(0, 24),)
    cfg.risk.no_new_trades_after_hour = None
    cfg.risk.flat_by_hour = None

    feed = ListFeed(bars_df, upto=400)
    strat = AlwaysLong()
    runner = _runner(cfg, feed, strat)
    runner.step()
    assert len(strat.calls) == 1
    feed.upto = 401
    runner.step()
    assert len(strat.calls) == 1, "entry must not be re-evaluated while holding"


def test_runner_opens_at_most_one_position(bars_df, tmp_path):
    cfg = _cfg(tmp_path)
    from goldbot.risk import SessionWindow
    cfg.risk.sessions = (SessionWindow(0, 24),)
    cfg.risk.no_new_trades_after_hour = None
    cfg.risk.flat_by_hour = None

    feed = ListFeed(bars_df, upto=400)
    runner = _runner(cfg, feed, AlwaysLong())
    for k in range(400, 410):
        feed.upto = k
        runner.step()
    assert len(runner.executor.positions(cfg.symbol, runner.magic)) == 1


def test_live_executor_requires_explicit_confirmation(bars_df, tmp_path):
    class FakeLive(Executor):
        def equity(self): return 10_000.0
        def positions(self, symbol, magic): return []
        def open(self, req): return None
        def modify(self, ticket, stop_loss, take_profit): return True
        def close(self, ticket): return True

    cfg = _cfg(tmp_path)
    with pytest.raises(RuntimeError, match="confirm_live"):
        LiveRunner(cfg=cfg, feed=ListFeed(bars_df, 400), executor=FakeLive())

    LiveRunner(cfg=cfg, feed=ListFeed(bars_df, 400), executor=FakeLive(), confirm_live=True)


def test_history_is_raised_to_cover_warmup(bars_df, tmp_path):
    cfg = _cfg(tmp_path)
    runner = LiveRunner(
        cfg=cfg, feed=ListFeed(bars_df, 400),
        executor=PaperExecutor(), history_bars=10,
    )
    assert runner.history_bars >= runner.strategy.warmup + 200
