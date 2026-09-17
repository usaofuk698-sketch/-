"""Causal technical indicators.

Every function here is *causal*: the value at index ``i`` depends only on input
rows ``0..i``. Nothing shifts data backwards, so a value never changes once its
bar has closed. This is the property that ``goldbot.validation.lookahead``
verifies mechanically -- indicators that violate it are the reason so many
strategies look flawless on a chart and fail in real time.

Warm-up rows are ``NaN`` rather than a back-filled guess; the engine refuses to
trade on ``NaN`` features instead of silently treating them as zero.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _as_array(s: pd.Series) -> np.ndarray:
    return np.asarray(s, dtype=float)


def rma(series: pd.Series, period: int) -> pd.Series:
    """Wilder's smoothing (a.k.a. RMA / SMMA).

    Seeded with a simple average of the first ``period`` observations, matching
    Wilder's original definition and the behaviour of MetaTrader and TradingView.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    x = _as_array(series)
    out = np.full(x.shape, np.nan)
    n = len(x)
    if n < period:
        return pd.Series(out, index=series.index, name=f"rma{period}")

    # Locate the first window of `period` consecutive non-NaN observations.
    start = -1
    run = 0
    for i in range(n):
        run = run + 1 if np.isfinite(x[i]) else 0
        if run == period:
            start = i
            break
    if start < 0:
        return pd.Series(out, index=series.index, name=f"rma{period}")

    acc = float(np.mean(x[start - period + 1 : start + 1]))
    out[start] = acc
    alpha = 1.0 / period
    for i in range(start + 1, n):
        xi = x[i]
        if not np.isfinite(xi):
            out[i] = acc  # hold the last value across a gap rather than poison it
            continue
        acc = acc + alpha * (xi - acc)
        out[i] = acc
    return pd.Series(out, index=series.index, name=f"rma{period}")


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average, seeded with an SMA of the first `period` bars."""
    if period < 1:
        raise ValueError("period must be >= 1")
    x = _as_array(series)
    out = np.full(x.shape, np.nan)
    n = len(x)
    if n < period:
        return pd.Series(out, index=series.index, name=f"ema{period}")
    acc = float(np.mean(x[:period]))
    out[period - 1] = acc
    alpha = 2.0 / (period + 1.0)
    for i in range(period, n):
        acc = acc + alpha * (x[i] - acc)
        out[i] = acc
    return pd.Series(out, index=series.index, name=f"ema{period}")


def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean().rename(f"sma{period}")


def true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """Wilder's True Range. The first bar has no prior close, so it is NaN."""
    prev_close = close.shift(1)
    a = high - low
    b = (high - prev_close).abs()
    c = (low - prev_close).abs()
    tr = pd.concat([a, b, c], axis=1).max(axis=1)
    tr.iloc[0] = np.nan
    return tr.rename("tr")


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    return rma(true_range(high, low, close), period).rename(f"atr{period}")


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI on close-to-close changes."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    gain.iloc[0] = np.nan
    loss.iloc[0] = np.nan
    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # avg_loss == 0 means an unbroken run of up-closes: RSI is 100 by definition.
    out = out.where(avg_loss != 0.0, 100.0)
    out = out.where(avg_gain.notna() & avg_loss.notna(), np.nan)
    return out.rename(f"rsi{period}")


def adx(
    high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14
) -> tuple[pd.Series, pd.Series, pd.Series]:
    """Wilder's ADX with the directional indicators.

    Returns:
        ``(adx, plus_di, minus_di)``. ADX measures trend *strength* without
        direction; it is used here purely as a chop filter.
    """
    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move.fillna(0.0), 0.0),
        index=high.index,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move.fillna(0.0), 0.0),
        index=high.index,
    )
    plus_dm.iloc[0] = np.nan
    minus_dm.iloc[0] = np.nan

    atr_n = rma(true_range(high, low, close), period)
    safe_atr = atr_n.replace(0.0, np.nan)
    plus_di = 100.0 * rma(plus_dm, period) / safe_atr
    minus_di = 100.0 * rma(minus_dm, period) / safe_atr

    denom = (plus_di + minus_di).replace(0.0, np.nan)
    dx = 100.0 * (plus_di - minus_di).abs() / denom
    adx_n = rma(dx, period)
    return (
        adx_n.rename(f"adx{period}"),
        plus_di.rename("plus_di"),
        minus_di.rename("minus_di"),
    )


def rolling_high(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).max().rename(f"hh{period}")


def rolling_low(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).min().rename(f"ll{period}")


def slope(series: pd.Series, period: int) -> pd.Series:
    """Per-bar linear-regression slope over a trailing window."""
    if period < 2:
        raise ValueError("period must be >= 2")
    x = np.arange(period, dtype=float)
    x_centered = x - x.mean()
    denom = float((x_centered**2).sum())

    def _fit(window: np.ndarray) -> float:
        if not np.all(np.isfinite(window)):
            return np.nan
        return float((x_centered * (window - window.mean())).sum() / denom)

    return (
        series.rolling(period, min_periods=period)
        .apply(_fit, raw=True)
        .rename(f"slope{period}")
    )
