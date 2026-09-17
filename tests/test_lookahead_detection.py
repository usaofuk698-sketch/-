"""The auditor is only worth having if it fails a strategy that cheats.

These tests build strategies that peek at future bars in the two ways it
happens in practice -- a backward shift, and a centred/global statistic -- and
assert the auditor catches both. Without this, a passing audit would only prove
the auditor never says anything.
"""
from __future__ import annotations

import pandas as pd

from goldbot.backtest.types import Side, Signal
from goldbot.strategy.base import Strategy, StrategyParams
from goldbot.strategy.trend_pullback import TrendPullback
from goldbot.validation.lookahead import audit


class _Params(StrategyParams):
    pass


class PeekingStrategy(Strategy):
    """Reads tomorrow's close via shift(-1). The classic look-ahead bug."""

    name = "peeking"

    def __init__(self):
        self.params = _Params()

    @property
    def warmup(self) -> int:
        return 20

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["future_close"] = df["close"].shift(-1)  # <-- the future
        return out

    def entry(self, i: int, feat: pd.DataFrame) -> Signal | None:
        fut = feat["future_close"].iloc[i]
        if pd.isna(fut):
            return None
        close = float(feat["close"].iloc[i])
        if fut > close:
            return Signal(side=Side.LONG, stop_loss=close - 5, take_profit=close + 5)
        return None


class RepaintingStrategy(Strategy):
    """Normalises against a statistic of the WHOLE series.

    Subtler and far more common than shift(-1): every historical value silently
    changes as new bars arrive, so the chart shows levels that were never
    knowable at the time.
    """

    name = "repainting"

    def __init__(self):
        self.params = _Params()

    @property
    def warmup(self) -> int:
        return 20

    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        out = df.copy()
        out["pct_of_range"] = (df["close"] - df["close"].min()) / (
            df["close"].max() - df["close"].min()
        )
        return out

    def entry(self, i: int, feat: pd.DataFrame) -> Signal | None:
        v = float(feat["pct_of_range"].iloc[i])
        close = float(feat["close"].iloc[i])
        if v < 0.2:
            return Signal(side=Side.LONG, stop_loss=close - 5, take_profit=close + 5)
        return None


def test_auditor_catches_shift_minus_one(bars):
    report = audit(PeekingStrategy(), bars, n_checks=12)
    assert not report.passed
    assert any(v.column == "future_close" for v in report.violations)


def test_auditor_catches_whole_series_normalisation(bars):
    report = audit(RepaintingStrategy(), bars, n_checks=12)
    assert not report.passed
    assert any(v.column == "pct_of_range" for v in report.violations)


def test_real_strategy_is_causal(bars):
    report = audit(TrendPullback(), bars, n_checks=15)
    assert report.passed, report.summary()
    assert report.checks == 15
