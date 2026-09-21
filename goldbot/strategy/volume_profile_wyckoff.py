"""Volume-profile + Wyckoff spring/upthrust scalper for XAUUSD M5.

The method
----------
A volume profile is the distribution of activity across *price* rather than
across time. Two levels come out of it:

* **POC** (point of control) -- the price where the most activity occurred. It
  behaves as a magnet: price that leaves it tends to come back.
* **Value area** (VAH / VAL) -- the band holding the bulk of activity, by
  convention 70%. Its edges are where acceptance ends.

Wyckoff names what happens at those edges. A **spring** is a probe below
support that fails: price dips under the value area, finds no supply, and
closes back inside. An **upthrust** is the mirror above. Both are traps -- the
break attracts breakout sellers/buyers, the recovery strands them, and their
covering supplies the fuel for the move back toward the POC.

That is the trade here: fade a failed probe of the value area, target the POC.

An honest limitation you must know about
----------------------------------------
MetaTrader gives **tick volume** for gold, not traded volume. Gold CFDs have no
central exchange and therefore no real volume figure; tick volume counts price
*changes*, not contracts. It correlates with activity well enough to shape a
profile, and that is why this is usable at all -- but it is a proxy. Anyone
selling you "institutional volume" on a retail gold feed is selling you a
count of price updates.

The profile is also rebuilt every ``profile_update_bars`` rather than on every
bar. That is both far cheaper and closer to how the levels are actually used:
a profile that changes shape every five minutes is not a level anyone is
trading against.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .. import indicators as ind
from ..backtest.types import Position, Side, Signal
from .base import Strategy, StrategyParams


@dataclass
class VolumeProfileWyckoffParams(StrategyParams):
    # -- the profile
    profile_lookback: int = 96        # bars in the profile (8h on M5)
    profile_bins: int = 40
    value_area_pct: float = 0.70
    profile_update_bars: int = 12     # rebuild every hour, not every bar
    # -- the probe
    probe_depth_atr: float = 0.10     # how far beyond the edge counts as a probe
    close_back_margin_atr: float = 0.05   # and how far back inside it must close
    probe_volume_max: float = 1.40    # probe volume vs recent average: no supply
    close_position_min: float = 0.55  # the recovery bar must close strongly
    # -- regime
    atr_period: int = 14
    regime_lookback: int = 288
    atr_min_mult: float = 0.55
    atr_max_mult: float = 3.00
    adx_period: int = 14
    adx_max: float = 45.0             # a ceiling: do not fade a real trend
    # -- stop and target
    stop_atr_buffer: float = 0.30     # beyond the probe extreme
    min_stop_atr_mult: float = 1.00
    reward_risk: float = 2.20
    target_poc: bool = True           # cap the target at the POC when it is nearer
    min_target_spread_ratio: float = 6.0
    # -- management
    breakeven_at_r: float | None = None
    breakeven_offset_atr: float = 0.05
    trail_at_r: float | None = None


def _profile_levels(
    highs: np.ndarray, lows: np.ndarray, vols: np.ndarray, bins: int, va_pct: float
) -> tuple[float, float, float]:
    """Return ``(poc, vah, val)`` for one window.

    Each bar spreads its volume evenly over the price range it covered, which
    is the standard approximation when only OHLC is available: the true
    within-bar distribution is unknowable, and assuming it all sat at the close
    would put weight where the market may have spent no time at all.
    """
    lo, hi = float(lows.min()), float(highs.max())
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return float("nan"), float("nan"), float("nan")

    edges = np.linspace(lo, hi, bins + 1)
    centres = 0.5 * (edges[:-1] + edges[1:])
    hist = np.zeros(bins)

    for h, l, v in zip(highs, lows, vols):
        if not np.isfinite(v) or v <= 0:
            v = 1.0
        first = np.searchsorted(edges, l, side="right") - 1
        last = np.searchsorted(edges, h, side="left")
        first = max(first, 0)
        last = min(max(last, first + 1), bins)
        hist[first:last] += v / (last - first)

    total = hist.sum()
    if total <= 0:
        return float("nan"), float("nan"), float("nan")

    poc_i = int(hist.argmax())
    # Grow outward from the POC, always taking the heavier neighbour, until the
    # value-area share is covered. This is the conventional construction.
    lo_i = hi_i = poc_i
    covered = hist[poc_i]
    target = total * va_pct
    while covered < target and (lo_i > 0 or hi_i < bins - 1):
        down = hist[lo_i - 1] if lo_i > 0 else -1.0
        up = hist[hi_i + 1] if hi_i < bins - 1 else -1.0
        if up >= down:
            hi_i += 1
            covered += hist[hi_i]
        else:
            lo_i -= 1
            covered += hist[lo_i]
    return float(centres[poc_i]), float(edges[hi_i + 1]), float(edges[lo_i])


class VolumeProfileWyckoff(Strategy):
    name = "volume_profile_wyckoff"

    _FEATURE_COLS = (
        "open", "high", "low", "close", "volume", "atr", "adx", "atr_med",
        "poc", "vah", "val", "vol_avg", "close_pos",
    )

    def __init__(self, params: VolumeProfileWyckoffParams | None = None):
        self.params = params or VolumeProfileWyckoffParams()
        self._a: dict[str, np.ndarray] = {}
        self._n: int = -1

    @property
    def warmup(self) -> int:
        p = self.params
        return max(p.regime_lookback, p.profile_lookback) + 50

    def _bind(self, feat: pd.DataFrame) -> None:
        if self._n == len(feat) and self._a:
            return
        self._a = {c: feat[c].to_numpy(dtype=float) for c in self._FEATURE_COLS}
        self._n = len(feat)

    # ------------------------------------------------------------- features
    def prepare(self, df: pd.DataFrame) -> pd.DataFrame:
        p = self.params
        out = df.copy()
        h, l, c = out["high"], out["low"], out["close"]
        if "volume" not in out.columns:
            out["volume"] = 1.0
        out["volume"] = out["volume"].fillna(1.0)

        out["atr"] = ind.atr(h, l, c, p.atr_period)
        out["adx"], _, _ = ind.adx(h, l, c, p.adx_period)
        out["atr_med"] = (
            out["atr"].rolling(p.regime_lookback, min_periods=p.regime_lookback).median()
        )
        out["vol_avg"] = out["volume"].rolling(p.profile_lookback, min_periods=20).mean().shift(1)
        rng = (h - l).replace(0.0, np.nan)
        out["close_pos"] = (c - l) / rng

        highs = out["high"].to_numpy(float)
        lows = out["low"].to_numpy(float)
        vols = out["volume"].to_numpy(float)
        n = len(out)
        poc = np.full(n, np.nan)
        vah = np.full(n, np.nan)
        val = np.full(n, np.nan)

        # The window ends at the PREVIOUS bar: a level built partly from the bar
        # being judged is not a level the market could have been trading against.
        cur = (np.nan, np.nan, np.nan)
        for i in range(p.profile_lookback + 1, n):
            if (i - p.profile_lookback - 1) % p.profile_update_bars == 0:
                s = i - p.profile_lookback
                cur = _profile_levels(
                    highs[s:i], lows[s:i], vols[s:i], p.profile_bins, p.value_area_pct
                )
            poc[i], vah[i], val[i] = cur

        out["poc"], out["vah"], out["val"] = poc, vah, val
        self._a = {col: out[col].to_numpy(dtype=float) for col in self._FEATURE_COLS}
        self._n = len(out)
        return out

    # ---------------------------------------------------------------- entry
    def entry(self, i: int, feat: pd.DataFrame) -> Signal | None:
        p = self.params
        self._bind(feat)
        a = self._a

        atr_v, med, adx_v = a["atr"][i], a["atr_med"][i], a["adx"][i]
        poc, vah, val = a["poc"][i], a["vah"][i], a["val"][i]
        close, high_v, low_v = a["close"][i], a["high"][i], a["low"][i]
        vol, vol_avg, close_pos = a["volume"][i], a["vol_avg"][i], a["close_pos"][i]

        if any(not np.isfinite(v) for v in (atr_v, med, poc, vah, val, close_pos)):
            return None
        if atr_v <= 0 or med <= 0 or vah <= val:
            return None
        if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
            return None
        if np.isfinite(adx_v) and adx_v >= p.adx_max:
            return None

        probe = p.probe_depth_atr * atr_v
        margin = p.close_back_margin_atr * atr_v

        # "No supply on the probe": a shallow-volume dip through the edge is a
        # test; a heavy one is a genuine breakout and must not be faded.
        vol_ok = True
        if np.isfinite(vol_avg) and vol_avg > 0:
            vol_ok = vol <= p.probe_volume_max * vol_avg

        spring = (
            low_v <= val - probe          # probed below the value area
            and close >= val + margin     # and was rejected back inside
            and close_pos >= p.close_position_min
            and vol_ok
        )
        upthrust = (
            high_v >= vah + probe
            and close <= vah - margin
            and (1.0 - close_pos) >= p.close_position_min
            and vol_ok
        )
        if spring == upthrust:
            return None

        if spring:
            sl = min(low_v - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
            if sl >= close:
                return None
            tp = close + p.reward_risk * (close - sl)
            if p.target_poc and poc > close:
                tp = min(tp, poc)          # the magnet, when it is the nearer one
            if tp <= close:
                return None
            side = Side.LONG
        else:
            sl = max(high_v + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
            if sl <= close:
                return None
            tp = close - p.reward_risk * (sl - close)
            if p.target_poc and poc < close:
                tp = max(tp, poc)
            if tp >= close:
                return None
            side = Side.SHORT

        return Signal(
            side=side,
            stop_loss=float(sl),
            take_profit=float(tp),
            reason="spring" if spring else "upthrust",
            meta={"atr": float(atr_v), "poc": float(poc), "vah": float(vah), "val": float(val)},
        )

    # --------------------------------------------------------------- manage
    def manage(self, i: int, feat: pd.DataFrame, pos: Position) -> tuple[float, float | None]:
        p = self.params
        if p.breakeven_at_r is None:
            return pos.stop_loss, pos.take_profit
        self._bind(feat)
        atr_v = self._a["atr"][i]
        atr_v = float(atr_v) if np.isfinite(atr_v) else 0.0
        close = float(self._a["close"][i])
        risk = pos.initial_risk
        if risk <= 0 or atr_v <= 0:
            return pos.stop_loss, pos.take_profit
        sl = pos.stop_loss
        if pos.side is Side.LONG:
            if (close - pos.entry_price) / risk >= p.breakeven_at_r:
                sl = max(sl, pos.entry_price + p.breakeven_offset_atr * atr_v)
            sl = min(sl, close)
        else:
            if (pos.entry_price - close) / risk >= p.breakeven_at_r:
                sl = min(sl, pos.entry_price - p.breakeven_offset_atr * atr_v)
            sl = max(sl, close)
        return sl, pos.take_profit
