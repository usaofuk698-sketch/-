"""Core value objects shared by the engine, broker and reporting layers."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

import pandas as pd


class Side(str, Enum):
    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        return 1 if self is Side.LONG else -1


class ExitReason(str, Enum):
    STOP_LOSS = "stop_loss"
    TAKE_PROFIT = "take_profit"
    TRAILING_STOP = "trailing_stop"
    SESSION_CLOSE = "session_close"
    MAX_BARS = "max_bars"
    SIGNAL_FLIP = "signal_flip"
    DAILY_LOSS_LIMIT = "daily_loss_limit"
    END_OF_DATA = "end_of_data"


@dataclass
class Signal:
    """A strategy's intent, expressed on a *closed* bar.

    Prices are levels, not fills: the broker decides the actual fill on the
    following bar. ``stop_loss`` is mandatory -- the risk model sizes the
    position from it, and a position with no stop cannot be sized.
    """

    side: Side
    stop_loss: float
    take_profit: float | None = None
    reason: str = ""
    meta: dict = field(default_factory=dict)


@dataclass
class Position:
    side: Side
    lots: float
    entry_price: float
    entry_time: pd.Timestamp
    stop_loss: float
    take_profit: float | None
    entry_bar: int
    initial_risk: float  # price distance entry -> original stop, for R-multiples
    reason: str = ""
    mae: float = 0.0  # worst unrealised excursion, in price units
    mfe: float = 0.0  # best unrealised excursion, in price units
    breakeven_done: bool = False
    swap_paid: float = 0.0


@dataclass
class Trade:
    """A completed round turn, with costs separated from gross P&L."""

    symbol: str
    side: Side
    lots: float
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float | None
    gross_pnl: float
    commission: float
    swap: float
    net_pnl: float
    r_multiple: float
    exit_reason: ExitReason
    bars_held: int
    mae: float
    mfe: float
    equity_after: float
    reason: str = ""

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["side"] = self.side.value
        d["exit_reason"] = self.exit_reason.value
        return d
