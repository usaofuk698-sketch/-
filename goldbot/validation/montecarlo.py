"""Monte Carlo resampling of the trade sequence.

A backtest hands you exactly one ordering of your trades. That ordering is an
accident. Had the same trades arrived in a different sequence, the profit would
be identical but the *drawdown* -- the thing that actually decides whether you
survive to collect the profit -- could be twice as deep.

Resampling the trade sequence thousands of times answers the questions a single
equity curve cannot:

* How deep does the drawdown get in a bad-but-plausible run?
* What is the chance of losing a third of the account before the edge shows up?
* Is the backtest's final equity a typical outcome or a lucky one?

**Assumption and its limits.** Bootstrapping treats trades as independent draws
from a fixed distribution. Real trade sequences are often serially correlated --
losses cluster in the regime that does not suit the system -- and real edges
decay. Both make the true risk *worse* than what this reports, so treat these
percentiles as a floor on the pain, not a forecast. :func:`autocorrelation`
reports how badly the independence assumption is violated.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd


@dataclass
class MonteCarloResult:
    simulations: int
    initial_capital: float
    final_equity: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    max_drawdown_pct: np.ndarray = field(repr=False, default_factory=lambda: np.array([]))
    ruin_probability: float = 0.0
    ruin_threshold_pct: float = 50.0
    prob_profitable: float = 0.0
    r_autocorrelation: float = 0.0
    observed_final_equity: float = 0.0
    observed_max_dd_pct: float = 0.0

    def percentile_table(self) -> pd.DataFrame:
        """Outcome percentiles, with both columns oriented the same way.

        Drawdowns are stored as negative numbers, so taking the *same*
        percentile of each column keeps a row internally consistent: the p5 row
        is a bad run on both measures (low final equity, deep drawdown), and p95
        is a good one. Inverting the drawdown percentile instead would pair the
        worst equity outcomes with the shallowest drawdowns and read backwards.
        """
        qs = [1, 5, 10, 25, 50, 75, 90, 95, 99]
        equity = np.percentile(self.final_equity, qs)
        return pd.DataFrame(
            {
                "percentile": qs,
                "final_equity": equity.round(2),
                "return_pct": ((equity / self.initial_capital - 1) * 100).round(2),
                "max_drawdown_pct": np.percentile(self.max_drawdown_pct, qs).round(2),
            }
        )

    def summary(self) -> str:
        p = self.percentile_table()
        median_dd = float(np.median(self.max_drawdown_pct))
        worst_5 = float(np.percentile(self.max_drawdown_pct, 5))
        lines = [
            "=" * 72,
            f" MONTE CARLO ({self.simulations:,} resampled trade sequences)",
            "=" * 72,
            f"   probability of profit          {self.prob_profitable:>8.1%}",
            f"   probability of {self.ruin_threshold_pct:.0f}% loss (ruin)  {self.ruin_probability:>8.1%}",
            f"   median max drawdown            {median_dd:>8.2f} %",
            f"   5th-percentile max drawdown    {worst_5:>8.2f} %   <- plan for this",
            f"   backtest's own drawdown        {self.observed_max_dd_pct:>8.2f} %",
            f"   R-multiple autocorrelation     {self.r_autocorrelation:>8.3f}   "
            "(far from 0 = bootstrap understates risk)",
            "",
            "   outcome distribution:",
        ]
        for _, row in p.iterrows():
            lines.append(
                f"     p{int(row['percentile']):<3d} equity {row['final_equity']:>12,.0f}  "
                f"return {row['return_pct']:>8.1f}%   worst DD {row['max_drawdown_pct']:>7.2f}%"
            )
        lines += [
            "",
            "   Read the 5th percentile, not the median. If that drawdown would",
            "   make you abandon the system, the position size is too large --",
            "   halve the risk per trade and re-run.",
            "=" * 72,
        ]
        return "\n".join(lines)


def autocorrelation(r: np.ndarray, lag: int = 1) -> float:
    """Lag-`lag` autocorrelation of the R-multiple series."""
    if len(r) <= lag + 1:
        return 0.0
    a, b = r[:-lag], r[lag:]
    if a.std() == 0 or b.std() == 0:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _curve_stats(equity: np.ndarray) -> tuple[float, float]:
    peak = np.maximum.accumulate(equity)
    dd = (equity - peak) / peak
    return float(equity[-1]), float(dd.min() * 100.0)


def run(
    trades: pd.DataFrame,
    initial_capital: float,
    simulations: int = 5_000,
    risk_per_trade_pct: float = 0.5,
    mode: str = "r",
    ruin_threshold_pct: float = 50.0,
    seed: int = 0,
) -> MonteCarloResult:
    """Bootstrap the trade sequence.

    Args:
        mode: ``"r"`` resamples R-multiples and compounds them at
            ``risk_per_trade_pct`` of running equity -- the right model when
            position size scales with the account. ``"pnl"`` resamples realised
            dollar P&L additively, which matches fixed-lot trading.
        ruin_threshold_pct: Equity drop that counts as ruin.
    """
    if trades.empty:
        raise ValueError("no trades to resample")
    if mode not in {"r", "pnl"}:
        raise ValueError("mode must be 'r' or 'pnl'")

    rng = np.random.default_rng(seed)
    n = len(trades)
    r = trades["r_multiple"].to_numpy(float)
    pnl = trades["net_pnl"].to_numpy(float)

    finals = np.empty(simulations)
    dds = np.empty(simulations)
    ruin_level = initial_capital * (1.0 - ruin_threshold_pct / 100.0)
    ruined = 0

    risk_frac = risk_per_trade_pct / 100.0
    for s in range(simulations):
        draw = rng.integers(0, n, n)
        if mode == "r":
            # Multiplicative: each trade risks a fixed fraction of live equity.
            growth = 1.0 + r[draw] * risk_frac
            growth = np.maximum(growth, 0.0)  # a trade cannot owe more than the risk
            equity = initial_capital * np.cumprod(growth)
        else:
            equity = initial_capital + np.cumsum(pnl[draw])
        equity = np.maximum(equity, 1e-9)
        final, dd = _curve_stats(equity)
        finals[s] = final
        dds[s] = dd
        if equity.min() <= ruin_level:
            ruined += 1

    observed_equity = initial_capital + np.cumsum(pnl)
    _, observed_dd = _curve_stats(np.maximum(observed_equity, 1e-9))

    return MonteCarloResult(
        simulations=simulations,
        initial_capital=initial_capital,
        final_equity=finals,
        max_drawdown_pct=dds,
        ruin_probability=ruined / simulations,
        ruin_threshold_pct=ruin_threshold_pct,
        prob_profitable=float((finals > initial_capital).mean()),
        r_autocorrelation=autocorrelation(r),
        observed_final_equity=float(observed_equity[-1]),
        observed_max_dd_pct=observed_dd,
    )
