"""Synthetic XAUUSD M5 bars.

**This exists to exercise the plumbing, not to evaluate a strategy.** The
generator has no memory of real order flow, no news, no liquidity structure and
no regime shifts beyond a toy drift process. A profit produced on synthetic bars
is evidence that the code runs -- nothing more. Treat any equity curve from this
module as an integration test result, never as a finding.

What it does reproduce, because the *engine* needs to be tested against it:
weekday-only sessions with weekend gaps, an intraday volatility profile that
peaks over the London/New York overlap, realistic intrabar high/low structure,
and alternating trending/ranging stretches.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

# Relative volatility by UTC hour: Asia quiet, London ramp, NY overlap peak.
_HOUR_VOL = {
    0: 0.55, 1: 0.50, 2: 0.55, 3: 0.55, 4: 0.55, 5: 0.60,
    6: 0.75, 7: 0.95, 8: 1.20, 9: 1.30, 10: 1.20, 11: 1.05,
    12: 1.35, 13: 1.65, 14: 1.70, 15: 1.45, 16: 1.20, 17: 1.00,
    18: 0.85, 19: 0.75, 20: 0.70, 21: 0.60, 22: 0.55, 23: 0.55,
}


def _trading_index(start: str, end: str, freq: str = "5min") -> pd.DatetimeIndex:
    """M5 stamps for a 24x5 market: Sunday 22:00 UTC open, Friday 21:00 close."""
    idx = pd.date_range(start=start, end=end, freq=freq, inclusive="left")
    dow, hour = idx.dayofweek, idx.hour
    open_mask = (
        ((dow == 6) & (hour >= 22))          # Sunday evening open
        | (dow < 4)                           # Mon-Thu, all day
        | ((dow == 4) & (hour < 21))          # Friday until the close
    )
    return idx[open_mask]


def generate(
    start: str = "2022-01-03",
    end: str = "2025-01-01",
    start_price: float = 1800.0,
    annual_vol: float = 0.15,
    seed: int = 7,
    drift_persistence: float = 0.995,
    drift_scale: float = 0.6,
    subticks: int = 12,
) -> pd.DataFrame:
    """Generate an OHLCV frame of M5 gold bars.

    Args:
        annual_vol: Target annualised volatility (gold sits near 0.13-0.18).
        drift_persistence: AR(1) coefficient on a latent drift term. Higher
            values produce longer trends; this is the only structure in the
            series and it is entirely artificial.
        drift_scale: Strength of the drift relative to diffusion, measured
            over the drift's own correlation time. 0 gives a pure random walk;
            1.0 makes trend and noise contribute equally; above ~2 the series
            stops resembling a traded market.
        subticks: Sub-steps simulated per bar to build a plausible high/low.
    """
    rng = np.random.default_rng(seed)
    idx = _trading_index(start, end)
    n = len(idx)
    if n == 0:
        raise ValueError("empty date range")

    bars_per_year = 252 * 288
    target_sigma = annual_vol / np.sqrt(bars_per_year)

    # Latent AR(1) drift: creates alternating trend and chop.
    #
    # Scaling this correctly is subtle and getting it wrong is what makes most
    # naive synthetic series unusable. A *persistent* drift accumulates linearly
    # over its correlation time T, while diffusion only accumulates as sqrt(T).
    # So a drift that looks negligible per bar (a few percent of bar volatility)
    # still dominates the path over T bars and detaches the series from its
    # starting level. The stationary drift std is therefore pinned to
    # sigma / sqrt(T), which makes the drift contribute about as much as
    # diffusion over its own correlation time, and `drift_scale` a dimensionless
    # knob around that balance.
    phi = float(np.clip(drift_persistence, 0.0, 0.9999))
    corr_time = 1.0 / max(1.0 - phi, 1e-9)

    # Split the volatility budget so total realised vol still hits `annual_vol`
    # once the drift's own variance is added in.
    budget = 1.0 / np.sqrt(1.0 + drift_scale**2)
    base_sigma = target_sigma * budget

    stationary_std = base_sigma * drift_scale / np.sqrt(corr_time)
    shock_std = stationary_std * np.sqrt(max(1.0 - phi * phi, 1e-12))
    drift = np.empty(n)
    drift[0] = rng.normal(0.0, stationary_std)
    shock = rng.normal(0.0, shock_std, n)
    for i in range(1, n):
        drift[i] = phi * drift[i - 1] + shock[i]

    hour_scale = np.array([_HOUR_VOL.get(h, 1.0) for h in idx.hour])
    sigma = base_sigma * hour_scale / subticks**0.5

    steps = rng.normal(0.0, 1.0, (n, subticks)) * sigma[:, None] + drift[:, None] / subticks
    log_path = np.cumsum(steps.reshape(-1))
    prices = start_price * np.exp(log_path)
    grid = prices.reshape(n, subticks)

    open_ = np.empty(n)
    open_[0] = start_price
    open_[1:] = grid[:-1, -1]
    close = grid[:, -1]
    high = np.maximum(grid.max(axis=1), np.maximum(open_, close))
    low = np.minimum(grid.min(axis=1), np.minimum(open_, close))

    # Weekend gaps: jump the price across each break in the index.
    gap_at = np.flatnonzero(np.diff(idx.values).astype("timedelta64[m]").astype(int) > 5) + 1
    if len(gap_at):
        jumps = rng.normal(0.0, base_sigma * 25.0, len(gap_at))
        shift = np.zeros(n)
        for pos, j in zip(gap_at, jumps):
            shift[pos:] += j
        factor = np.exp(shift)
        open_, close, high, low = open_ * factor, close * factor, high * factor, low * factor

    volume = rng.gamma(shape=3.0, scale=120.0, size=n) * hour_scale

    df = pd.DataFrame(
        {
            "open": np.round(open_, 2),
            "high": np.round(high, 2),
            "low": np.round(low, 2),
            "close": np.round(close, 2),
            "volume": np.round(volume, 0),
        },
        index=idx,
    )
    df.index.name = "time"
    # Rounding can break the envelope by a cent; restore the invariant.
    df["high"] = df[["open", "high", "low", "close"]].max(axis=1)
    df["low"] = df[["open", "high", "low", "close"]].min(axis=1)
    return df
