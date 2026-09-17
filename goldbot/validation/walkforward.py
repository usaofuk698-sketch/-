"""Walk-forward analysis.

A single backtest over all your data, with parameters chosen by looking at all
your data, measures one thing: how well you fitted the past. It cannot measure
anything else, no matter how good the numbers are.

Walk-forward asks the only question that matters. Optimise on a training window,
then trade the parameters *forward* on data the optimiser never saw. Roll the
window and repeat. The stitched out-of-sample curve is the closest honest
estimate of live behaviour this project can produce.

Two outputs matter more than the equity curve:

``efficiency``
    Out-of-sample expectancy divided by in-sample expectancy. Near 1.0 means the
    edge survived contact with unseen data. Below ~0.4 means the optimiser was
    mostly fitting noise, whatever the in-sample numbers looked like.

``parameter stability``
    Whether each fold chooses similar parameters. A system whose optimal
    settings jump around every window does not have an edge -- it has a
    different curve fit each time, and the next window will want different
    numbers again.
"""
from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from .. import metrics
from ..backtest.engine import BacktestEngine
from ..contract import XAUUSD, Contract
from ..risk import RiskConfig, RiskManager
from ..strategy.base import Strategy

StrategyFactory = Callable[[dict], Strategy]


def default_objective(stats: metrics.Stats, min_trades: int = 25) -> float:
    """Rank candidates by t-statistic, not by profit.

    Total profit prefers whichever parameter set happened to catch the biggest
    move in the window. The t-statistic rewards an edge that is both positive
    and *consistent*, and it penalises small samples automatically, which is
    what we actually want out of sample.
    """
    if stats.trades < min_trades:
        return -math.inf
    if not np.isfinite(stats.t_stat):
        return -math.inf
    return float(stats.t_stat)


@dataclass
class Fold:
    index: int
    train_start: pd.Timestamp
    train_end: pd.Timestamp
    test_start: pd.Timestamp
    test_end: pd.Timestamp
    best_params: dict
    train_stats: metrics.Stats
    test_stats: metrics.Stats
    candidates_evaluated: int
    start_equity: float
    end_equity: float


@dataclass
class WalkForwardResult:
    folds: list[Fold] = field(default_factory=list)
    oos_trades: pd.DataFrame = field(default_factory=pd.DataFrame)
    oos_equity: pd.Series = field(default_factory=lambda: pd.Series(dtype=float))
    initial_capital: float = 0.0
    param_grid: dict = field(default_factory=dict)

    @property
    def oos_stats(self) -> metrics.Stats:
        return metrics.compute(self.oos_trades, self.oos_equity, self.initial_capital)

    @property
    def efficiency(self) -> float:
        """Pooled OOS expectancy / pooled IS expectancy.

        Pooled, not averaged per fold. A mean of per-fold ratios is dominated by
        folds whose in-sample expectancy happens to sit near zero -- dividing by
        0.0001R produces a huge ratio from a trivial difference, and the average
        can come out strongly positive while out-of-sample performance is in
        fact negative. Pooling weights every fold by the trades it actually
        took, so the sign of this number always agrees with the OOS result.

        Returns ``nan`` when there was no in-sample edge to retain, since the
        ratio is meaningless then.
        """
        is_r = sum(f.train_stats.expectancy_r * f.train_stats.trades for f in self.folds)
        is_n = sum(f.train_stats.trades for f in self.folds)
        oos_r = sum(f.test_stats.expectancy_r * f.test_stats.trades for f in self.folds)
        oos_n = sum(f.test_stats.trades for f in self.folds)
        if is_n == 0 or oos_n == 0:
            return float("nan")
        is_exp = is_r / is_n
        if is_exp <= 0:
            return float("nan")
        return float((oos_r / oos_n) / is_exp)

    def parameter_stability(self) -> pd.DataFrame:
        """Per-parameter spread of the choices made across folds."""
        if not self.folds:
            return pd.DataFrame()
        chosen = pd.DataFrame([f.best_params for f in self.folds])
        rows = []
        for col in chosen.columns:
            vals = pd.to_numeric(chosen[col], errors="coerce")
            if vals.notna().all() and vals.nunique() > 1:
                mean = float(vals.mean())
                cv = float(vals.std(ddof=0) / abs(mean)) if mean else float("nan")
            else:
                cv = 0.0
            rows.append(
                {
                    "parameter": col,
                    "distinct_choices": int(chosen[col].nunique()),
                    "most_common": chosen[col].mode().iloc[0],
                    "coeff_of_variation": cv,
                }
            )
        return pd.DataFrame(rows).sort_values("coeff_of_variation", ascending=False)

    def fold_table(self) -> pd.DataFrame:
        return pd.DataFrame(
            [
                {
                    "fold": f.index,
                    "test_start": f.test_start,
                    "test_end": f.test_end,
                    "is_expectancy_r": round(f.train_stats.expectancy_r, 4),
                    "oos_expectancy_r": round(f.test_stats.expectancy_r, 4),
                    "oos_trades": f.test_stats.trades,
                    "oos_net": round(f.test_stats.net_profit, 2),
                    "equity_after": round(f.end_equity, 2),
                }
                for f in self.folds
            ]
        )

    def summary(self) -> str:
        s = self.oos_stats
        eff = self.efficiency
        lines = [
            "=" * 72,
            " WALK-FORWARD ANALYSIS (out-of-sample only)",
            "=" * 72,
            f"   folds                 {len(self.folds):>10d}",
            f"   OOS trades            {s.trades:>10,}",
            f"   OOS expectancy        {s.expectancy_r:>10.4f} R",
            f"   OOS t-statistic       {s.t_stat:>10.2f}",
            f"   OOS net profit        {s.net_profit:>10,.2f} USD",
            f"   OOS max drawdown      {s.max_drawdown_pct:>10.2f} %",
            f"   OOS Sharpe            {s.sharpe:>10.2f}",
            f"   walk-forward eff.     {eff:>10.2f}   (OOS / IS expectancy)",
            "",
            " VERDICT",
        ]
        if s.trades < 50:
            lines.append("   TOO FEW OOS TRADES - inconclusive.")
        elif s.expectancy_r <= 0:
            lines.append("   FAILED - no edge survives out of sample. Do not trade this.")
        elif s.t_stat < 2.0:
            lines.append(f"   FAILED - OOS edge is not significant (t={s.t_stat:.2f}).")
        elif not np.isfinite(eff):
            lines.append(
                "   OOS edge is positive and significant, but there was no positive "
                "in-sample\n   baseline to compare against -- inspect the folds directly."
            )
        elif eff < 0.4:
            lines.append(
                f"   OVERFIT - OOS keeps only {eff:.0%} of in-sample edge. "
                "The optimiser is fitting noise."
            )
        else:
            lines.append(
                "   SURVIVED - positive, significant and stable out of sample. "
                "This is the weakest claim worth acting on."
            )
        lines.append("=" * 72)
        return "\n".join(lines)


def expand_grid(grid: dict[str, Sequence]) -> list[dict]:
    """Cartesian product of a parameter grid."""
    if not grid:
        return [{}]
    keys = list(grid)
    return [dict(zip(keys, combo)) for combo in itertools.product(*(grid[k] for k in keys))]


def _run(
    strategy: Strategy,
    data: pd.DataFrame,
    risk_cfg: RiskConfig,
    contract: Contract,
    capital: float,
    max_bars_in_trade: int | None,
):
    risk = RiskManager(risk_cfg, contract, capital)
    engine = BacktestEngine(
        strategy, risk, None, contract, capital, max_bars_in_trade=max_bars_in_trade
    )
    return engine.run(data)


def run(
    df: pd.DataFrame,
    strategy_factory: StrategyFactory,
    param_grid: dict[str, Sequence],
    train_bars: int = 30_000,
    test_bars: int = 10_000,
    step_bars: int | None = None,
    risk_cfg: RiskConfig | None = None,
    contract: Contract = XAUUSD,
    initial_capital: float = 10_000.0,
    objective: Callable[[metrics.Stats], float] = default_objective,
    max_bars_in_trade: int | None = 96,
    anchored: bool = False,
    verbose: bool = True,
) -> WalkForwardResult:
    """Roll train/test windows across ``df`` and stitch the OOS results.

    Args:
        train_bars: Optimisation window length, in bars.
        test_bars: Forward window traded with the chosen parameters.
        step_bars: Window advance; defaults to ``test_bars`` (no OOS overlap,
            so every out-of-sample trade is counted exactly once).
        anchored: If True the training window always starts at bar 0 and grows;
            otherwise it rolls with a fixed length.
    """
    risk_cfg = risk_cfg or RiskConfig()
    step = step_bars or test_bars
    combos = expand_grid(param_grid)
    probe = strategy_factory(combos[0])
    warmup = probe.warmup

    n = len(df)
    result = WalkForwardResult(initial_capital=initial_capital, param_grid=dict(param_grid))
    equity_pieces: list[pd.Series] = []
    all_trades: list[pd.DataFrame] = []
    capital = initial_capital

    fold_id = 0
    train_start = 0
    while True:
        train_end = train_start + train_bars
        test_end = min(train_end + test_bars, n)
        if train_end + warmup >= n or test_end - train_end < warmup + 50:
            break

        is_slice = df.iloc[(0 if anchored else train_start):train_end]
        # The test run needs `warmup` bars of history before its first tradable
        # bar, otherwise indicators would be cold exactly where we start trading.
        oos_slice = df.iloc[train_end - warmup:test_end]

        best_score, best_params, best_stats = -math.inf, None, None
        for combo in combos:
            res = _run(
                strategy_factory(combo), is_slice, risk_cfg, contract, capital, max_bars_in_trade
            )
            st = metrics.compute(res.trades, res.equity, capital)
            score = objective(st)
            if score > best_score:
                best_score, best_params, best_stats = score, combo, st

        if best_params is None or not np.isfinite(best_score):
            if verbose:
                print(f"  fold {fold_id}: no candidate met the objective, skipping")
            train_start += step
            fold_id += 1
            continue

        oos = _run(
            strategy_factory(best_params), oos_slice, risk_cfg, contract, capital, max_bars_in_trade
        )
        oos_stats = metrics.compute(oos.trades, oos.equity, capital)

        # Count only the genuinely out-of-sample portion of the stitched curve.
        oos_equity = oos.equity.loc[oos.equity.index >= df.index[train_end]]
        start_equity = capital
        capital = float(oos_equity.iloc[-1]) if len(oos_equity) else capital

        equity_pieces.append(oos_equity)
        if not oos.trades.empty:
            trades = oos.trades[oos.trades["entry_time"] >= df.index[train_end]]
            if not trades.empty:
                all_trades.append(trades)

        result.folds.append(
            Fold(
                index=fold_id,
                train_start=is_slice.index[0],
                train_end=is_slice.index[-1],
                test_start=df.index[train_end],
                test_end=df.index[test_end - 1],
                best_params=dict(best_params),
                train_stats=best_stats,
                test_stats=oos_stats,
                candidates_evaluated=len(combos),
                start_equity=start_equity,
                end_equity=capital,
            )
        )
        if verbose:
            print(
                f"  fold {fold_id}: IS t={best_stats.t_stat:5.2f} "
                f"-> OOS exp={oos_stats.expectancy_r:+.4f}R "
                f"n={oos_stats.trades:3d} equity={capital:,.0f}  {best_params}"
            )

        train_start += step
        fold_id += 1
        if test_end >= n:
            break

    if equity_pieces:
        result.oos_equity = pd.concat(equity_pieces).sort_index()
        result.oos_equity = result.oos_equity[~result.oos_equity.index.duplicated(keep="first")]
    if all_trades:
        result.oos_trades = pd.concat(all_trades, ignore_index=True)
    return result
