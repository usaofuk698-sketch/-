"""Loading and sanity-checking OHLCV data.

Bad data is the quietest source of fake backtest profit. A single bar where the
high is below the close, or a duplicated timestamp, or a hidden weekend gap, can
hand a strategy trades it could never have taken. Everything loaded here is
validated before the engine is allowed near it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

_ALIASES = {
    "open": {"open", "o", "<open>", "openprice"},
    "high": {"high", "h", "<high>", "highprice"},
    "low": {"low", "l", "<low>", "lowprice"},
    "close": {"close", "c", "<close>", "closeprice", "last"},
    "volume": {"volume", "vol", "v", "<vol>", "tickvol", "<tickvol>", "tick_volume"},
}
_TIME_KEYS = {"time", "date", "datetime", "timestamp", "<date>", "<time>", "gmt time"}


@dataclass
class DataReport:
    rows: int
    start: pd.Timestamp | None
    end: pd.Timestamp | None
    duplicates_dropped: int = 0
    bars_repaired: int = 0
    rows_dropped: int = 0
    gaps: list[tuple[pd.Timestamp, pd.Timestamp, float]] = field(default_factory=list)

    def summary(self) -> str:
        lines = [
            f"rows           : {self.rows:,}",
            f"range          : {self.start}  ->  {self.end}",
            f"dup timestamps : {self.duplicates_dropped}",
            f"bars repaired  : {self.bars_repaired}",
            f"rows dropped   : {self.rows_dropped}",
            f"gaps > 1h      : {len(self.gaps)}",
        ]
        for s, e, hrs in self.gaps[:5]:
            lines.append(f"    {s} -> {e}  ({hrs:.1f}h)")
        if len(self.gaps) > 5:
            lines.append(f"    ... and {len(self.gaps) - 5} more")
        return "\n".join(lines)


def _normalise_columns(df: pd.DataFrame) -> pd.DataFrame:
    lower = {str(c).strip().lower(): c for c in df.columns}
    rename: dict[str, str] = {}
    for canonical, names in _ALIASES.items():
        for low, original in lower.items():
            if low in names:
                rename[original] = canonical
                break
    return df.rename(columns=rename)


def _build_index(df: pd.DataFrame, tz: str) -> pd.DataFrame:
    lower = {str(c).strip().lower(): c for c in df.columns}
    date_col = next((lower[k] for k in lower if k in {"<date>", "date"}), None)
    time_col = next((lower[k] for k in lower if k in {"<time>", "time"}), None)

    if date_col is not None and time_col is not None and date_col != time_col:
        stamp = df[date_col].astype(str) + " " + df[time_col].astype(str)
        df = df.drop(columns=[date_col, time_col])
    else:
        key = next((lower[k] for k in lower if k in _TIME_KEYS), None)
        if key is None:
            raise ValueError(
                f"no timestamp column found; looked for {sorted(_TIME_KEYS)}, "
                f"got {list(df.columns)}"
            )
        stamp = df[key]
        df = df.drop(columns=[key])

    idx = pd.to_datetime(stamp, errors="coerce", format="mixed")
    df.index = pd.DatetimeIndex(idx, name="time")
    df = df[df.index.notna()]
    if df.index.tz is None:
        df.index = df.index.tz_localize(tz)
    else:
        df.index = df.index.tz_convert(tz)
    # The engine reasons in naive UTC hours; normalise once, here.
    df.index = df.index.tz_convert("UTC").tz_localize(None)
    return df


def clean(df: pd.DataFrame, gap_hours: float = 1.0) -> tuple[pd.DataFrame, DataReport]:
    """Validate and repair an OHLCV frame. Returns the clean frame and a report."""
    n_in = len(df)
    for col in ("open", "high", "low", "close"):
        if col not in df.columns:
            raise ValueError(f"missing required column {col!r}")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" not in df.columns:
        df["volume"] = np.nan

    df = df[["open", "high", "low", "close", "volume"]]
    df = df[df[["open", "high", "low", "close"]].notna().all(axis=1)]
    df = df[(df[["open", "high", "low", "close"]] > 0).all(axis=1)]

    df = df.sort_index()
    dups = int(df.index.duplicated().sum())
    if dups:
        df = df[~df.index.duplicated(keep="first")]

    # Repair bars whose high/low do not envelope open/close. These come from
    # feed glitches; clamping is safer than dropping the bar and inventing a gap.
    true_high = df[["open", "high", "low", "close"]].max(axis=1)
    true_low = df[["open", "high", "low", "close"]].min(axis=1)
    repaired = int(((df["high"] < true_high) | (df["low"] > true_low)).sum())
    df["high"] = true_high
    df["low"] = true_low

    gaps: list[tuple[pd.Timestamp, pd.Timestamp, float]] = []
    if len(df) > 1:
        deltas = df.index.to_series().diff()
        big = deltas[deltas > pd.Timedelta(hours=gap_hours)]
        for ts, delta in big.items():
            gaps.append((ts - delta, ts, delta.total_seconds() / 3600.0))

    report = DataReport(
        rows=len(df),
        start=df.index[0] if len(df) else None,
        end=df.index[-1] if len(df) else None,
        duplicates_dropped=dups,
        bars_repaired=repaired,
        rows_dropped=n_in - len(df) - dups,
        gaps=gaps,
    )
    return df, report


def load_csv(path: str | Path, tz: str = "UTC", gap_hours: float = 1.0):
    """Read a broker/platform CSV export into a validated OHLCV frame.

    Handles MetaTrader ``<DATE>/<TIME>`` exports, Dukascopy, and generic
    ``time,open,high,low,close,volume`` files. ``tz`` is the timezone the file's
    stamps are in -- MT5 exports are usually broker time, not UTC, and getting
    this wrong silently shifts every session filter.
    """
    raw = pd.read_csv(path, sep=None, engine="python")
    raw = _normalise_columns(raw)
    raw = _build_index(raw, tz)
    return clean(raw, gap_hours=gap_hours)


def resample(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    """Aggregate to a coarser timeframe (e.g. ``'15min'``). Never upsamples."""
    out = df.resample(rule, label="left", closed="left").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}
    )
    return out.dropna(subset=["open", "high", "low", "close"])


def train_test_split(df: pd.DataFrame, test_fraction: float = 0.3):
    """Chronological hold-out. Never shuffle time series -- it leaks the future."""
    if not 0.0 < test_fraction < 1.0:
        raise ValueError("test_fraction must be in (0, 1)")
    cut = int(len(df) * (1.0 - test_fraction))
    return df.iloc[:cut].copy(), df.iloc[cut:].copy()
