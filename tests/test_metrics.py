from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from goldbot import metrics


def _trades(pnls, risk=100.0):
    n = len(pnls)
    return pd.DataFrame(
        {
            "net_pnl": pnls,
            "gross_pnl": pnls,
            "commission": [0.0] * n,
            "swap": [0.0] * n,
            "r_multiple": [p / risk for p in pnls],
            "bars_held": [10] * n,
            "exit_reason": ["take_profit" if p > 0 else "stop_loss" for p in pnls],
        }
    )


def _equity(pnls, start=10_000.0):
    idx = pd.date_range("2024-01-01", periods=len(pnls) + 1, freq="1D")
    return pd.Series(np.concatenate([[start], start + np.cumsum(pnls)]), index=idx)


def test_a_90_percent_win_rate_can_still_lose_money():
    """The headline point: win rate alone says nothing about profitability.

    90 wins of +$10 against 10 losses of -$100 is a 90% win rate and a net loss.
    The report must call this negative regardless of how good the win rate looks.
    """
    pnls = [10.0] * 90 + [-100.0] * 10
    st = metrics.compute(_trades(pnls), _equity(pnls), 10_000.0)
    assert st.win_rate_pct == pytest.approx(90.0)
    assert st.expectancy_usd == pytest.approx(-1.0)
    assert st.net_profit < 0
    assert "NEGATIVE EDGE" in metrics._verdict(st)


def test_a_35_percent_win_rate_can_be_strongly_profitable():
    pnls = [300.0] * 35 + [-100.0] * 65
    st = metrics.compute(_trades(pnls), _equity(pnls), 10_000.0)
    assert st.win_rate_pct == pytest.approx(35.0)
    assert st.expectancy_usd == pytest.approx(40.0)
    assert st.profit_factor > 1.5


def test_small_samples_are_called_inconclusive():
    pnls = [100.0, 120.0, -50.0, 200.0, 90.0]
    st = metrics.compute(_trades(pnls), _equity(pnls), 10_000.0)
    assert "NOT ENOUGH DATA" in metrics._verdict(st)


def test_positive_but_noisy_edge_is_flagged_not_significant():
    rng = np.random.default_rng(0)
    pnls = list(rng.normal(2.0, 200.0, 200))  # tiny edge, huge variance
    st = metrics.compute(_trades(pnls), _equity(pnls), 10_000.0)
    assert st.t_stat < 2.0
    assert "NOT SIGNIFICANT" in metrics._verdict(st)


def test_t_stat_grows_with_sample_size_for_a_fixed_edge():
    rng = np.random.default_rng(1)
    small = list(rng.normal(20.0, 100.0, 50))
    large = list(rng.normal(20.0, 100.0, 2000))
    t_small = metrics.compute(_trades(small), _equity(small), 10_000.0).t_stat
    t_large = metrics.compute(_trades(large), _equity(large), 10_000.0).t_stat
    assert t_large > t_small


def test_max_drawdown_measures_peak_to_trough():
    eq = pd.Series(
        [100.0, 120.0, 90.0, 110.0, 60.0, 80.0],
        index=pd.date_range("2024-01-01", periods=6, freq="1D"),
    )
    usd, pct, _ = metrics.max_drawdown(eq)
    assert usd == pytest.approx(-60.0)          # 120 -> 60
    assert pct == pytest.approx(-50.0)


def test_empty_results_do_not_crash():
    st = metrics.compute(pd.DataFrame(), pd.Series(dtype=float), 10_000.0)
    assert st.trades == 0
    assert isinstance(metrics.report(st), str)
