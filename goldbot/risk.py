"""Position sizing and the hard limits that keep a losing run survivable.

Sizing is derived from the stop distance, never from a fixed lot count: risk per
trade stays constant in dollars while gold's volatility swings the stop distance
around. A fixed 0.10 lots risks $30 on a quiet day and $300 through a CPI print.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from .contract import Contract
from .types_compat import Side


@dataclass(frozen=True)
class SessionWindow:
    """An inclusive-exclusive window of UTC hours, ``[start, end)``.

    Windows may wrap midnight (``start > end``), e.g. ``SessionWindow(22, 2)``.
    """

    start_hour: int
    end_hour: int

    def contains(self, ts: pd.Timestamp) -> bool:
        h = ts.hour
        if self.start_hour <= self.end_hour:
            return self.start_hour <= h < self.end_hour
        return h >= self.start_hour or h < self.end_hour


@dataclass
class RiskConfig:
    risk_per_trade_pct: float = 0.5       # % of equity risked per trade
    max_daily_loss_pct: float = 2.0       # stop trading for the day past this
    max_daily_profit_pct: float | None = None  # optional: bank the day and stop
    max_trades_per_day: int = 5
    max_consecutive_losses: int = 4       # cool off for the rest of the day
    max_concurrent_positions: int = 1
    compounding: bool = True              # size off live equity vs initial capital
    max_spread: float | None = None       # USD/oz; refuse entries above this
    min_stop_distance: float = 0.50       # USD/oz; refuse absurdly tight stops
    max_stop_distance: float = 30.0       # USD/oz; refuse absurdly wide stops
    trading_days: tuple[int, ...] = (0, 1, 2, 3, 4)  # Mon..Fri
    sessions: tuple[SessionWindow, ...] = (SessionWindow(7, 16),)
    flat_by_hour: int | None = 20         # force flat at this UTC hour
    no_new_trades_after_hour: int | None = 16
    news_blackout: tuple[tuple[pd.Timestamp, pd.Timestamp], ...] = ()


@dataclass
class DailyState:
    day: object = None
    realised_pnl: float = 0.0
    trades: int = 0
    consecutive_losses: int = 0
    halted: bool = False
    halt_reason: str = ""
    start_equity: float = 0.0


class RiskManager:
    """Owns sizing decisions and the per-day circuit breakers."""

    def __init__(self, cfg: RiskConfig, contract: Contract, initial_capital: float):
        self.cfg = cfg
        self.contract = contract
        self.initial_capital = initial_capital
        self.state = DailyState()

    # ------------------------------------------------------------ day rollover
    def on_bar(self, ts: pd.Timestamp, equity: float) -> None:
        day = ts.date()
        if self.state.day != day:
            self.state = DailyState(day=day, start_equity=equity)

    # ---------------------------------------------------------------- filters
    def in_session(self, ts: pd.Timestamp) -> bool:
        if ts.weekday() not in self.cfg.trading_days:
            return False
        return any(w.contains(ts) for w in self.cfg.sessions)

    def in_news_blackout(self, ts: pd.Timestamp) -> bool:
        return any(start <= ts < end for start, end in self.cfg.news_blackout)

    def spread_ok(self, spread: float) -> bool:
        """Mirror of the EA's spread filter.

        Without this the backtest would take entries the live EA refuses, and
        the two would quietly describe different systems -- exactly the gap
        that makes a backtest stop predicting anything.
        """
        return self.cfg.max_spread is None or spread <= self.cfg.max_spread

    def may_open(self, ts: pd.Timestamp, open_positions: int) -> tuple[bool, str]:
        """Gate on every condition that can forbid a *new* position."""
        if self.state.halted:
            return False, self.state.halt_reason
        if open_positions >= self.cfg.max_concurrent_positions:
            return False, "max_concurrent_positions"
        if self.state.trades >= self.cfg.max_trades_per_day:
            return False, "max_trades_per_day"
        if self.state.consecutive_losses >= self.cfg.max_consecutive_losses:
            self._halt("max_consecutive_losses")
            return False, "max_consecutive_losses"
        if not self.in_session(ts):
            return False, "out_of_session"
        if self.in_news_blackout(ts):
            return False, "news_blackout"
        if (
            self.cfg.no_new_trades_after_hour is not None
            and ts.hour >= self.cfg.no_new_trades_after_hour
        ):
            return False, "late_in_session"
        return True, ""

    def must_flatten(self, ts: pd.Timestamp) -> bool:
        """True when open risk should be closed regardless of the signal."""
        if self.state.halted:
            return True
        if self.cfg.flat_by_hour is not None and ts.hour >= self.cfg.flat_by_hour:
            return True
        if ts.weekday() not in self.cfg.trading_days:
            return True
        return False

    # ----------------------------------------------------------------- sizing
    def size(self, equity: float, entry: float, stop: float) -> tuple[float, str]:
        """Lots such that a stop-out costs ``risk_per_trade_pct`` of equity.

        Returns ``(lots, rejection_reason)``; ``lots == 0.0`` means no trade.
        """
        distance = abs(entry - stop)
        if distance < self.cfg.min_stop_distance:
            return 0.0, "stop_too_tight"
        if distance > self.cfg.max_stop_distance:
            return 0.0, "stop_too_wide"

        base = equity if self.cfg.compounding else self.initial_capital
        if base <= 0:
            return 0.0, "no_equity"
        risk_usd = base * (self.cfg.risk_per_trade_pct / 100.0)

        # Commission is a round-turn cost on the same position, so it competes
        # with the stop for the same risk budget.
        per_lot_loss = distance * self.contract.contract_size
        per_lot_loss += 2.0 * self.contract.commission_per_lot_per_side
        if per_lot_loss <= 0:
            return 0.0, "degenerate_risk"

        lots = self.contract.round_lots(risk_usd / per_lot_loss)
        if lots <= 0:
            return 0.0, "below_min_lot"
        return lots, ""

    # ------------------------------------------------------------- accounting
    def on_trade_closed(self, net_pnl: float, equity: float) -> None:
        self.state.trades += 1
        self.state.realised_pnl += net_pnl
        if net_pnl < 0:
            self.state.consecutive_losses += 1
        else:
            self.state.consecutive_losses = 0

        base = self.state.start_equity or self.initial_capital
        loss_limit = -abs(base * self.cfg.max_daily_loss_pct / 100.0)
        if self.state.realised_pnl <= loss_limit:
            self._halt("daily_loss_limit")
        elif self.cfg.max_daily_profit_pct is not None:
            target = base * self.cfg.max_daily_profit_pct / 100.0
            if self.state.realised_pnl >= target:
                self._halt("daily_profit_target")

    def _halt(self, reason: str) -> None:
        self.state.halted = True
        self.state.halt_reason = reason
