"""Causality audit: proof that nothing in the strategy reads the future.

This is the single most valuable test in the project, and almost no retail
system has one.

The failure it catches is *repainting*. An indicator repaints when the value it
shows for a past bar changes as new bars arrive -- centred moving averages,
ZigZag, most "non-lag" indicators, and anything using ``shift(-n)``. On a chart
the signals look uncanny, because you are seeing values that were only knowable
after the fact. In live trading the arrow appears, then moves, then disappears.

The test is simple and decisive: compute the features on the full history, then
recompute them on a *truncated prefix* ending at bar ``k``. If the strategy is
causal, every value at bar ``k`` must be bit-for-bit identical, because a
truncated prefix is exactly what the strategy will have available in real time.
Any difference is a value that depended on data from after bar ``k``.

The same comparison is applied to the emitted signal, which catches look-ahead
introduced by the decision logic rather than by an indicator.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..strategy.base import Strategy


@dataclass
class Violation:
    bar: int
    timestamp: pd.Timestamp
    column: str
    full_history_value: object
    truncated_value: object

    def __str__(self) -> str:
        return (
            f"bar {self.bar} ({self.timestamp}) column {self.column!r}: "
            f"full={self.full_history_value!r} vs truncated={self.truncated_value!r}"
        )


@dataclass
class AuditReport:
    checks: int = 0
    violations: list[Violation] = field(default_factory=list)
    columns_checked: tuple[str, ...] = ()
    signal_mismatches: int = 0

    @property
    def passed(self) -> bool:
        return not self.violations and self.signal_mismatches == 0

    def summary(self) -> str:
        head = "PASS" if self.passed else "FAIL"
        lines = [
            "=" * 64,
            f" LOOK-AHEAD / REPAINT AUDIT: {head}",
            "=" * 64,
            f"   bars probed        : {self.checks}",
            f"   columns per bar    : {len(self.columns_checked)}",
            f"   feature violations : {len(self.violations)}",
            f"   signal mismatches  : {self.signal_mismatches}",
        ]
        if self.passed:
            lines += [
                "",
                "   Every feature and signal at bar k is identical whether computed",
                "   on history up to k or on the full series. The strategy cannot",
                "   see the future, so backtest fills are reproducible live.",
            ]
        else:
            lines += ["", "   REPAINTING DETECTED -- backtest results are invalid.", ""]
            for v in self.violations[:12]:
                lines.append(f"     {v}")
            if len(self.violations) > 12:
                lines.append(f"     ... and {len(self.violations) - 12} more")
        lines.append("=" * 64)
        return "\n".join(lines)


def _numeric_columns(df: pd.DataFrame) -> tuple[str, ...]:
    return tuple(c for c in df.columns if pd.api.types.is_numeric_dtype(df[c]))


def audit(
    strategy: Strategy,
    df: pd.DataFrame,
    n_checks: int = 40,
    rtol: float = 0.0,
    atol: float = 1e-9,
    seed: int = 0,
    check_signals: bool = True,
) -> AuditReport:
    """Probe ``n_checks`` bars for look-ahead.

    Args:
        rtol/atol: Tolerance for the value comparison. The default is
            effectively exact: a causal pipeline performs *identical* float
            operations in an identical order on a prefix, so the results match
            bit-for-bit. Loosen this only if a feature legitimately involves
            non-deterministic computation, and be suspicious if you need to.
        check_signals: Also compare the emitted :class:`Signal` at each bar.
    """
    rng = np.random.default_rng(seed)
    n = len(df)
    lo = max(strategy.warmup + 5, 10)
    if n <= lo + 5:
        raise ValueError(f"need more than {lo + 5} bars to audit; got {n}")

    bars = np.unique(rng.integers(lo, n - 1, size=n_checks))
    full = strategy.prepare(df)
    cols = _numeric_columns(full)
    report = AuditReport(columns_checked=cols)

    for k in bars:
        k = int(k)
        # Exactly the data the strategy would hold at bar k in real time.
        truncated = strategy.prepare(df.iloc[: k + 1])
        report.checks += 1

        for col in cols:
            a = full[col].iloc[k]
            b = truncated[col].iloc[-1]
            if pd.isna(a) and pd.isna(b):
                continue
            if pd.isna(a) != pd.isna(b) or not np.isclose(
                float(a), float(b), rtol=rtol, atol=atol, equal_nan=True
            ):
                report.violations.append(Violation(k, df.index[k], col, a, b))

        if check_signals:
            sig_full = strategy.entry(k, full)
            sig_trunc = strategy.entry(k, truncated)
            if _signal_key(sig_full) != _signal_key(sig_trunc):
                report.signal_mismatches += 1
                report.violations.append(
                    Violation(k, df.index[k], "<signal>", _signal_key(sig_full), _signal_key(sig_trunc))
                )

    # Restore the strategy's internal cache to the full frame so the caller can
    # keep using the object.
    strategy.prepare(df)
    return report


def _signal_key(sig) -> tuple | None:
    if sig is None:
        return None
    return (
        sig.side.value,
        round(float(sig.stop_loss), 9),
        round(float(sig.take_profit), 9) if sig.take_profit is not None else None,
    )
