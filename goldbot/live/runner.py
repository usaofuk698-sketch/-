"""Live / paper execution loop.

The single rule this module exists to enforce: **act only on closed bars**.

The backtest computes a signal from bar ``i`` once bar ``i`` is complete and
fills at bar ``i+1``'s open. If the live loop instead evaluates the bar that is
still forming, it is reading values that will change before the bar closes --
the live system and the tested system are then different systems, and the
backtest no longer describes anything. Every read here goes through
:meth:`MarketFeed.closed_bars`, which drops the forming bar unconditionally.

Safety posture:

* ``PaperExecutor`` is the default. Live order routing requires constructing
  ``Mt5Executor`` explicitly *and* passing ``confirm_live=True``.
* The account's real equity drives the risk limits, not a simulated balance.
* Positions are reconciled from the broker at startup, so a restart cannot
  double up on an existing trade.
* The bot only manages orders carrying its own magic number.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone

import pandas as pd

from ..backtest.types import Side, Signal
from ..config import AppConfig
from ..risk import RiskManager

log = logging.getLogger("goldbot.live")


# --------------------------------------------------------------------- feeds
class MarketFeed(ABC):
    @abstractmethod
    def fetch(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Return recent OHLCV bars, oldest first, indexed by UTC bar-open time."""

    def closed_bars(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        """Bars guaranteed complete.

        The most recent row from any broker feed is the bar currently forming.
        Its high, low and close are still moving, so a signal derived from it is
        not reproducible and does not match the backtest. It is dropped here,
        once, so no caller has to remember to.
        """
        df = self.fetch(symbol, timeframe, count + 1)
        return df.iloc[:-1] if len(df) else df


# ----------------------------------------------------------------- executors
@dataclass
class OrderRequest:
    symbol: str
    side: Side
    lots: float
    stop_loss: float
    take_profit: float | None
    comment: str = "goldbot"
    magic: int = 770577


@dataclass
class BrokerPosition:
    ticket: int
    symbol: str
    side: Side
    lots: float
    entry_price: float
    stop_loss: float
    take_profit: float | None
    magic: int


class Executor(ABC):
    @abstractmethod
    def equity(self) -> float: ...

    @abstractmethod
    def positions(self, symbol: str, magic: int) -> list[BrokerPosition]: ...

    @abstractmethod
    def open(self, req: OrderRequest) -> BrokerPosition | None: ...

    @abstractmethod
    def modify(self, ticket: int, stop_loss: float, take_profit: float | None) -> bool: ...

    @abstractmethod
    def close(self, ticket: int) -> bool: ...


@dataclass
class PaperExecutor(Executor):
    """Logs what it would have done. Never touches a broker.

    Run every strategy change through this for at least a few weeks and compare
    its fills against the backtest's. Divergence here -- and there usually is
    some -- is the honest measure of how much of the backtest survives contact
    with a real feed.
    """

    starting_equity: float = 10_000.0
    _equity: float = field(init=False)
    _positions: dict[int, BrokerPosition] = field(default_factory=dict, init=False)
    _next_ticket: int = field(default=1, init=False)

    def __post_init__(self) -> None:
        self._equity = self.starting_equity

    def equity(self) -> float:
        return self._equity

    def positions(self, symbol: str, magic: int) -> list[BrokerPosition]:
        return [p for p in self._positions.values() if p.symbol == symbol and p.magic == magic]

    def open(self, req: OrderRequest) -> BrokerPosition:
        ticket = self._next_ticket
        self._next_ticket += 1
        pos = BrokerPosition(
            ticket=ticket, symbol=req.symbol, side=req.side, lots=req.lots,
            entry_price=float("nan"), stop_loss=req.stop_loss,
            take_profit=req.take_profit, magic=req.magic,
        )
        self._positions[ticket] = pos
        log.info(
            "PAPER open  #%d %s %.2f lots  sl=%.2f tp=%s",
            ticket, req.side.value, req.lots, req.stop_loss,
            f"{req.take_profit:.2f}" if req.take_profit else "none",
        )
        return pos

    def modify(self, ticket: int, stop_loss: float, take_profit: float | None) -> bool:
        pos = self._positions.get(ticket)
        if pos is None:
            return False
        pos.stop_loss, pos.take_profit = stop_loss, take_profit
        log.info("PAPER modify #%d sl=%.2f", ticket, stop_loss)
        return True

    def close(self, ticket: int) -> bool:
        if self._positions.pop(ticket, None) is None:
            return False
        log.info("PAPER close #%d", ticket)
        return True


# ------------------------------------------------------------------- runner
@dataclass
class LiveRunner:
    cfg: AppConfig
    feed: MarketFeed
    executor: Executor
    magic: int = 770577
    history_bars: int = 2_000
    confirm_live: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.executor, PaperExecutor) and not self.confirm_live:
            raise RuntimeError(
                "refusing to route live orders without confirm_live=True. "
                "Run PaperExecutor first, for weeks, and compare its fills to "
                "the backtest before enabling this."
            )
        self.strategy = self.cfg.build_strategy()
        self.risk = RiskManager(self.cfg.risk, self.cfg.contract, self.executor.equity())
        self._last_bar: pd.Timestamp | None = None
        need = self.strategy.warmup + 200
        if self.history_bars < need:
            log.warning("history_bars=%d is below warmup+200=%d; raising it", self.history_bars, need)
            self.history_bars = need

    # ------------------------------------------------------------- one cycle
    def step(self) -> bool:
        """Process at most one newly closed bar. Returns True if it acted."""
        bars = self.feed.closed_bars(self.cfg.symbol, self.cfg.timeframe, self.history_bars)
        if len(bars) < self.strategy.warmup + 2:
            log.debug("only %d bars, need %d", len(bars), self.strategy.warmup + 2)
            return False

        last_ts = bars.index[-1]
        if self._last_bar is not None and last_ts <= self._last_bar:
            return False  # nothing new has closed
        self._last_bar = last_ts

        equity = self.executor.equity()
        self.risk.on_bar(last_ts, equity)
        feat = self.strategy.prepare(bars)
        i = len(feat) - 1
        open_positions = self.executor.positions(self.cfg.symbol, self.magic)

        # 1. manage what is already open
        for pos in open_positions:
            if self.risk.must_flatten(last_ts):
                log.info("flattening #%d: session/risk rule", pos.ticket)
                self.executor.close(pos.ticket)
                continue
            new_sl = self._revised_stop(i, feat, pos)
            if new_sl is not None and abs(new_sl - pos.stop_loss) > self.cfg.contract.tick_size:
                self.executor.modify(pos.ticket, new_sl, pos.take_profit)

        # 2. consider a new entry
        if self.executor.positions(self.cfg.symbol, self.magic):
            return True
        ok, why = self.risk.may_open(last_ts, open_positions=0)
        if not ok:
            log.debug("no entry at %s: %s", last_ts, why)
            return True

        signal = self.strategy.entry(i, feat)
        if signal is None:
            return True

        ref_price = float(feat["close"].iloc[i])
        lots, reject = self.risk.size(equity, ref_price, signal.stop_loss)
        if lots <= 0:
            log.info("signal rejected by sizing: %s", reject)
            return True

        log.info(
            "signal %s at %s ref=%.2f sl=%.2f tp=%s -> %.2f lots",
            signal.side.value, last_ts, ref_price, signal.stop_loss,
            f"{signal.take_profit:.2f}" if signal.take_profit else "none", lots,
        )
        self.executor.open(
            OrderRequest(
                symbol=self.cfg.symbol, side=signal.side, lots=lots,
                stop_loss=self.cfg.contract.round_price(signal.stop_loss),
                take_profit=(
                    self.cfg.contract.round_price(signal.take_profit)
                    if signal.take_profit is not None else None
                ),
                magic=self.magic,
            )
        )
        return True

    def _revised_stop(self, i: int, feat: pd.DataFrame, pos: BrokerPosition) -> float | None:
        """Ask the strategy for a tighter stop, applying the same one-way guard
        the backtest engine applies."""
        from ..backtest.types import Position

        shim = Position(
            side=pos.side, lots=pos.lots, entry_price=pos.entry_price,
            entry_time=feat.index[i], stop_loss=pos.stop_loss,
            take_profit=pos.take_profit, entry_bar=0,
            initial_risk=abs(pos.entry_price - pos.stop_loss),
        )
        if not (shim.initial_risk > 0):
            return None
        new_sl, _ = self.strategy.manage(i, feat, shim)
        if pos.side is Side.LONG:
            return max(pos.stop_loss, new_sl)
        return min(pos.stop_loss, new_sl)

    # ------------------------------------------------------------------ loop
    def run(self, poll_seconds: int = 20, max_cycles: int | None = None) -> None:
        log.info(
            "starting %s runner on %s %s (magic=%d)",
            "PAPER" if isinstance(self.executor, PaperExecutor) else "LIVE",
            self.cfg.symbol, self.cfg.timeframe, self.magic,
        )
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            try:
                self.step()
            except Exception:
                # A feed hiccup must not kill the process and leave a position
                # unmanaged. Log it and retry on the next poll.
                log.exception("cycle failed at %s", datetime.now(timezone.utc))
            cycles += 1
            if max_cycles is None or cycles < max_cycles:
                time.sleep(poll_seconds)
