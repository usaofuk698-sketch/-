from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from goldbot import indicators as ind


@pytest.fixture
def series():
    rng = np.random.default_rng(3)
    return pd.Series(2000 + np.cumsum(rng.normal(0, 1, 500)))


def test_ema_of_a_constant_is_that_constant():
    s = pd.Series([50.0] * 100)
    assert ind.ema(s, 20).dropna().eq(50.0).all()


def test_rma_matches_wilders_recursion():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    out = ind.rma(s, 3)
    assert out.iloc[2] == pytest.approx(2.0)                    # seed = SMA(1,2,3)
    assert out.iloc[3] == pytest.approx(2.0 + (4.0 - 2.0) / 3)  # then recursive
    assert pd.isna(out.iloc[1])


def test_rsi_is_bounded_and_extreme_on_monotone_input():
    up = pd.Series(np.arange(1, 80, dtype=float))
    assert ind.rsi(up, 14).dropna().eq(100.0).all()
    down = pd.Series(np.arange(80, 1, -1, dtype=float))
    assert ind.rsi(down, 14).dropna().max() < 1e-9


def test_rsi_stays_within_bounds(series):
    r = ind.rsi(series, 14).dropna()
    assert r.between(0, 100).all()
    assert len(r) > 400


def test_true_range_covers_gaps():
    h = pd.Series([10.0, 20.0]); l = pd.Series([9.0, 19.0]); c = pd.Series([9.5, 19.5])
    tr = ind.true_range(h, l, c)
    assert pd.isna(tr.iloc[0])
    assert tr.iloc[1] == pytest.approx(20.0 - 9.5)  # gap, not the 1.0 bar range


def test_atr_is_positive_and_warms_up(series):
    h, l, c = series + 1, series - 1, series
    a = ind.atr(h, l, c, 14)
    assert a.dropna().gt(0).all()
    assert a.iloc[:13].isna().all()


def test_adx_is_bounded_and_high_in_a_clean_trend():
    trend = pd.Series(np.arange(200, dtype=float))
    a, plus, minus = ind.adx(trend + 1, trend - 1, trend, 14)
    assert a.dropna().between(0, 100).all()
    assert a.dropna().iloc[-1] > 40, "a perfect ramp must read as a strong trend"
    assert plus.dropna().iloc[-1] > minus.dropna().iloc[-1]


def test_indicators_are_causal_under_truncation(series):
    """Extending the series must not change any already-computed value."""
    h, l = series + 1, series - 1
    k = 400
    for name, full, trunc in [
        ("ema", ind.ema(series, 21), ind.ema(series.iloc[: k + 1], 21)),
        ("rsi", ind.rsi(series, 14), ind.rsi(series.iloc[: k + 1], 14)),
        ("atr", ind.atr(h, l, series, 14), ind.atr(h.iloc[: k + 1], l.iloc[: k + 1], series.iloc[: k + 1], 14)),
    ]:
        assert full.iloc[k] == pytest.approx(trunc.iloc[-1], abs=1e-12), name


def test_rolling_extremes_only_look_backwards():
    s = pd.Series([1.0, 5.0, 2.0, 9.0, 3.0])
    assert ind.rolling_high(s, 3).iloc[2] == 5.0   # not 9.0
    assert ind.rolling_low(s, 3).iloc[2] == 1.0


def test_slope_sign_follows_direction():
    up = pd.Series(np.arange(50, dtype=float))
    assert ind.slope(up, 10).dropna().gt(0).all()
    assert ind.slope(up[::-1].reset_index(drop=True), 10).dropna().lt(0).all()
