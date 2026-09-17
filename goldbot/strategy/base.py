"""Strategy interface.

A strategy answers one question -- *given bars up to and including this closed
one, do I want a position, and where does it die?* -- and nothing else. It never
sees the account balance, so it cannot accidentally couple signal quality to
position size, and the same object drives both the backtest and the live runner.

The contract is deliberately narrow:

``prepare(df)``
    Vectorised feature computation, run once over the whole frame. Must be
    causal (see :mod:`goldbot.indicators`).
``entry(i, feat)``
    Called on closed bar ``i``. Returns a :class:`Signal` or ``None``. May read
    ``feat.iloc[i]`` and earlier rows -- never later ones.
``manage(i, feat, pos)``
    Optional in-trade stop/target revision (break-even, trailing).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass

import pandas as pd

from ..backtest.types import Position, Signal


@dataclass
class StrategyParams:
    """Base for parameter sets; gives the optimiser a uniform dict view."""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StrategyParams":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in d.items() if k in known})


class Strategy(ABC):
    name: str = "strategy"
    params: StrategyParams

    @abstractmethod
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        """Return ``df`` plus feature columns. Must not mutate the input."""

    @abstractmethod
    def entry(self, i: int, feat: pd.DataFrame) -> Signal | None:
        """Entry decision for closed bar ``i``."""

    def manage(self, i: int, feat: pd.DataFrame, pos: Position) -> tuple[float, float | None]:
        """Return possibly-revised ``(stop_loss, take_profit)``.

        The default never moves a level. Overrides must only ever move a stop in
        the position's favour -- the engine asserts this.
        """
        return pos.stop_loss, pos.take_profit

    @property
    def warmup(self) -> int:
        """Bars required before features are trustworthy."""
        return 0
