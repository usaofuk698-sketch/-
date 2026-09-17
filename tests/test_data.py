from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from goldbot.data.loader import clean, load_csv, resample, train_test_split
from goldbot.data.synthetic import generate


def test_ohlc_envelope_is_repaired():
    idx = pd.date_range("2024-01-01", periods=3, freq="5min")
    df = pd.DataFrame(
        {"open": [100, 100, 100], "high": [99, 101, 101],   # bar 0 high < open
         "low": [98, 102, 99], "close": [100, 100, 100]},   # bar 1 low > close
        index=idx,
    )
    out, rep = clean(df)
    assert rep.bars_repaired == 2
    assert (out["high"] >= out[["open", "close"]].max(axis=1)).all()
    assert (out["low"] <= out[["open", "close"]].min(axis=1)).all()


def test_duplicate_timestamps_are_dropped():
    idx = pd.DatetimeIndex(["2024-01-01 00:00", "2024-01-01 00:00", "2024-01-01 00:05"])
    df = pd.DataFrame({"open": [1.0, 1.0, 1.0], "high": [2.0] * 3, "low": [0.5] * 3,
                       "close": [1.5] * 3}, index=idx)
    out, rep = clean(df)
    assert rep.duplicates_dropped == 1
    assert len(out) == 2


def test_gaps_are_reported():
    df = generate(start="2023-01-02", end="2023-02-01", seed=1)
    _, rep = clean(df.copy())
    assert len(rep.gaps) >= 3, "weekend closures must show up as gaps"
    assert all(hours > 24 for *_, hours in rep.gaps)


def test_csv_roundtrip_with_metatrader_headers(tmp_path):
    df = generate(start="2023-01-02", end="2023-01-10", seed=2)
    path = tmp_path / "XAUUSD_M5.csv"
    out = df.reset_index().rename(
        columns={"time": "<DATE>", "open": "<OPEN>", "high": "<HIGH>",
                 "low": "<LOW>", "close": "<CLOSE>", "volume": "<TICKVOL>"}
    )
    out.to_csv(path, index=False)
    loaded, rep = load_csv(path)
    assert rep.rows == len(df)
    assert list(loaded.columns) == ["open", "high", "low", "close", "volume"]
    assert np.allclose(loaded["close"].to_numpy(), df["close"].to_numpy())


def test_resample_aggregates_correctly():
    df = generate(start="2023-01-02", end="2023-01-05", seed=3)
    m15 = resample(df, "15min")
    assert len(m15) < len(df)
    first = df.iloc[:3]
    assert m15["open"].iloc[0] == pytest.approx(first["open"].iloc[0])
    assert m15["high"].iloc[0] == pytest.approx(first["high"].max())
    assert m15["close"].iloc[0] == pytest.approx(first["close"].iloc[-1])


def test_split_is_chronological_not_shuffled():
    df = generate(start="2023-01-02", end="2023-03-01", seed=4)
    train, test = train_test_split(df, 0.3)
    assert train.index[-1] < test.index[0], "shuffling a time series leaks the future"
    assert len(train) + len(test) == len(df)


def test_synthetic_data_is_internally_consistent():
    df = generate(start="2023-01-02", end="2023-04-01", seed=9)
    assert (df["high"] >= df["low"]).all()
    assert (df["high"] >= df[["open", "close"]].max(axis=1)).all()
    assert (df["low"] <= df[["open", "close"]].min(axis=1)).all()
    assert df.index.is_monotonic_increasing
    assert not df.index.has_duplicates
    assert not (df.index.dayofweek == 5).any(), "no Saturday bars"


def test_synthetic_volatility_tracks_the_requested_level():
    df = generate(start="2022-01-03", end="2024-01-01", annual_vol=0.15, seed=11)
    realised = float(np.log(df["close"]).diff().dropna().std() * np.sqrt(252 * 288))
    assert 0.11 < realised < 0.20, realised


def test_synthetic_prices_stay_in_a_plausible_band():
    """Guards the drift-scaling fix: a mis-scaled AR(1) drift detaches the
    series from its starting level entirely."""
    for seed in (1, 2, 3, 4, 5):
        df = generate(start="2022-01-03", end="2025-01-01", start_price=1800.0, seed=seed)
        assert 0.3 < df["close"].min() / 1800.0
        assert df["close"].max() / 1800.0 < 3.0, seed
