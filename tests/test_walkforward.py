from __future__ import annotations

import numpy as np
import pandas as pd

from goldbot import metrics
from goldbot.risk import RiskConfig, SessionWindow
from goldbot.strategy.trend_pullback import TrendPullback, TrendPullbackParams
from goldbot.validation import montecarlo, walkforward


def _factory(params: dict):
    return TrendPullback(TrendPullbackParams.from_dict(params))


def test_expand_grid_is_the_cartesian_product():
    grid = {"a": [1, 2], "b": [10, 20, 30]}
    combos = walkforward.expand_grid(grid)
    assert len(combos) == 6
    assert {"a": 2, "b": 30} in combos
    assert walkforward.expand_grid({}) == [{}]


def test_objective_rejects_undersized_samples():
    st = metrics.Stats(trades=5, t_stat=9.0)
    assert walkforward.default_objective(st, min_trades=25) == -np.inf
    st2 = metrics.Stats(trades=100, t_stat=2.5)
    assert walkforward.default_objective(st2, min_trades=25) == 2.5


def test_walkforward_test_windows_do_not_overlap(long_bars):
    result = walkforward.run(
        long_bars, _factory, {"adx_min": [20.0, 26.0]},
        train_bars=12_000, test_bars=6_000,
        risk_cfg=RiskConfig(sessions=(SessionWindow(7, 16),)),
        initial_capital=10_000.0, verbose=False,
    )
    assert len(result.folds) >= 3
    for a, b in zip(result.folds, result.folds[1:]):
        assert a.test_end < b.test_start, "overlapping OOS windows double-count trades"


def test_oos_trades_start_after_their_training_window(long_bars):
    result = walkforward.run(
        long_bars, _factory, {"adx_min": [20.0]},
        train_bars=12_000, test_bars=6_000,
        risk_cfg=RiskConfig(sessions=(SessionWindow(7, 16),)),
        initial_capital=10_000.0, verbose=False,
    )
    for fold in result.folds:
        assert fold.train_end < fold.test_start


def test_efficiency_sign_agrees_with_oos_result(long_bars):
    """Guards the pooling fix: a mean of per-fold ratios could report a
    strongly positive efficiency while OOS expectancy was negative."""
    result = walkforward.run(
        long_bars, _factory, {"adx_min": [18.0, 24.0]},
        train_bars=12_000, test_bars=6_000,
        risk_cfg=RiskConfig(sessions=(SessionWindow(7, 16),)),
        initial_capital=10_000.0, verbose=False,
    )
    eff = result.efficiency
    oos = result.oos_stats
    if np.isfinite(eff) and oos.trades > 0:
        assert (eff > 0) == (oos.expectancy_r > 0)


def test_monte_carlo_flags_a_losing_system():
    trades = pd.DataFrame({
        "net_pnl": [-50.0] * 70 + [60.0] * 30,
        "r_multiple": [-1.0] * 70 + [1.2] * 30,
    })
    mc = montecarlo.run(trades, 10_000.0, simulations=500, seed=1)
    assert mc.prob_profitable < 0.1
    assert mc.observed_final_equity < 10_000.0


def test_monte_carlo_drawdowns_bracket_the_observed_one():
    rng = np.random.default_rng(0)
    r = rng.normal(0.05, 1.0, 400)
    trades = pd.DataFrame({"net_pnl": r * 50.0, "r_multiple": r})
    mc = montecarlo.run(trades, 10_000.0, simulations=800, seed=2)
    p5 = float(np.percentile(mc.max_drawdown_pct, 5))
    p95 = float(np.percentile(mc.max_drawdown_pct, 95))
    assert p5 < p95, "percentiles must be ordered, deepest first"
    assert p5 <= mc.observed_max_dd_pct * 0.5, "a bad reshuffle must be worse than the one we saw"


def test_monte_carlo_percentile_table_rows_are_consistent():
    rng = np.random.default_rng(3)
    r = rng.normal(0.1, 1.0, 300)
    trades = pd.DataFrame({"net_pnl": r * 50.0, "r_multiple": r})
    table = montecarlo.run(trades, 10_000.0, simulations=600, seed=4).percentile_table()
    equity = table["final_equity"].to_numpy()
    dd = table["max_drawdown_pct"].to_numpy()
    assert (np.diff(equity) >= 0).all(), "equity must rise with percentile"
    assert (np.diff(dd) >= 0).all(), "drawdown must get shallower as the outcome improves"
