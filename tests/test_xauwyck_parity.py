"""Parity between the Python volume-profile/Wyckoff strategy and XauWyck_M5.

The profile builder gets its own tests: it is the part where a subtle mistake
(including the current bar, or a bad value-area expansion) produces levels that
look plausible and are quietly wrong.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from goldbot.backtest.types import Side
from goldbot.strategy.volume_profile_wyckoff import (
    VolumeProfileWyckoff,
    VolumeProfileWyckoffParams,
    _profile_levels,
)

MQ5 = Path(__file__).resolve().parents[1] / "mt5" / "XauWyck_M5.mq5"

PARAM_MAP = {
    "InpProfileLookback": "profile_lookback",
    "InpProfileBins": "profile_bins",
    "InpValueAreaPct": "value_area_pct",
    "InpProfileUpdateBars": "profile_update_bars",
    "InpProbeDepthAtr": "probe_depth_atr",
    "InpCloseBackMarginAtr": "close_back_margin_atr",
    "InpProbeVolumeMax": "probe_volume_max",
    "InpClosePositionMin": "close_position_min",
    "InpAtrPeriod": "atr_period",
    "InpRegimeLookback": "regime_lookback",
    "InpAtrMinMult": "atr_min_mult",
    "InpAtrMaxMult": "atr_max_mult",
    "InpAdxPeriod": "adx_period",
    "InpAdxMax": "adx_max",
    "InpStopAtrBuffer": "stop_atr_buffer",
    "InpMinStopAtrMult": "min_stop_atr_mult",
    "InpRewardRisk": "reward_risk",
    "InpMinTargetSpreadRatio": "min_target_spread_ratio",
}


def parse_inputs() -> dict:
    src = MQ5.read_text()
    pat = re.compile(r"^\s*input\s+(?:double|int|long|bool)\s+(\w+)\s*=\s*([-\w.]+)\s*;", re.M)
    return {n: (v == "true" if v in ("true", "false") else float(v)) for n, v in pat.findall(src)}


@pytest.mark.parametrize("mq5_name,py_field", sorted(PARAM_MAP.items()))
def test_defaults_match(mq5_name, py_field):
    inputs = parse_inputs()
    assert mq5_name in inputs
    assert float(inputs[mq5_name]) == pytest.approx(
        float(getattr(VolumeProfileWyckoffParams(), py_field))
    )


# ------------------------------------------------------------ profile maths
def test_poc_lands_where_the_volume_is():
    highs = np.array([10.0, 10.2, 10.1, 10.3, 10.15])
    lows = np.array([9.8, 10.0, 9.9, 10.1, 9.95])
    vols = np.array([1.0, 1.0, 50.0, 1.0, 1.0])      # the heavy bar spans 9.9-10.1
    poc, vah, val = _profile_levels(highs, lows, vols, 20, 0.70)
    assert 9.9 <= poc <= 10.1
    assert val <= poc <= vah


def test_value_area_brackets_the_poc_and_narrows_as_the_share_falls():
    rng = np.random.default_rng(0)
    mid = 2000 + rng.normal(0, 2, 400)
    highs, lows = mid + 0.5, mid - 0.5
    vols = np.ones(400)
    _, vah70, val70 = _profile_levels(highs, lows, vols, 50, 0.70)
    _, vah40, val40 = _profile_levels(highs, lows, vols, 50, 0.40)
    assert (vah40 - val40) < (vah70 - val70)


def test_flat_profile_is_reported_as_unusable():
    flat = np.full(30, 2000.0)
    poc, vah, val = _profile_levels(flat, flat, np.ones(30), 20, 0.70)
    assert np.isnan(poc) and np.isnan(vah) and np.isnan(val)


# ------------------------------------------------------------- EA structure
def test_ea_profile_window_excludes_the_current_bar():
    """A level built partly from the bar being judged is not a level the
    market could have been trading against."""
    src = MQ5.read_text()
    assert "CopyRates(_Symbol, PERIOD_CURRENT, 1, lookback, r)" in src


def test_ea_expands_the_value_area_toward_the_heavier_side():
    src = MQ5.read_text()
    assert "if(up >= down) { hiIdx++;" in src


def test_ea_treats_adx_as_a_ceiling():
    src = MQ5.read_text()
    assert "adxV >= InpAdxMax" in src


def test_ea_uses_tick_volume():
    src = MQ5.read_text()
    assert "tick_volume" in src
    assert "TICK volume" in src, "the proxy must be documented in the file"


def test_ea_caches_the_profile_rather_than_rebuilding_every_bar():
    src = MQ5.read_text()
    assert "g_barsSinceProfile" in src
    assert "InpProfileUpdateBars" in src


# --------------------------------------------------------- decision parity
def mql5_entry(i, feat, p):
    a = {c: feat[c].to_numpy(float) for c in
         ("open", "high", "low", "close", "volume", "atr", "adx", "atr_med",
          "poc", "vah", "val", "vol_avg", "close_pos")}
    if i < max(p.regime_lookback, p.profile_lookback) + 10:
        return None
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
    vol_ok = True
    if np.isfinite(vol_avg) and vol_avg > 0:
        vol_ok = vol <= p.probe_volume_max * vol_avg

    spring = (low_v <= val - probe and close >= val + margin
              and close_pos >= p.close_position_min and vol_ok)
    upthrust = (high_v >= vah + probe and close <= vah - margin
                and (1.0 - close_pos) >= p.close_position_min and vol_ok)
    if spring == upthrust:
        return None

    if spring:
        sl = min(low_v - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
        return None if sl >= close else (Side.LONG, sl)
    sl = max(high_v + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
    return None if sl <= close else (Side.SHORT, sl)


def test_entry_decisions_are_identical(long_bars):
    strategy = VolumeProfileWyckoff()
    feat = strategy.prepare(long_bars)
    bad, signals = [], 0
    for i in range(strategy.warmup, len(feat)):
        py = strategy.entry(i, feat)
        mq = mql5_entry(i, feat, strategy.params)
        pk = None if py is None else (py.side, round(py.stop_loss, 6))
        mk = None if mq is None else (mq[0], round(mq[1], 6))
        if pk is not None:
            signals += 1
        if pk != mk:
            bad.append((i, feat.index[i], pk, mk))
    assert signals > 100, f"only {signals} signals"
    assert not bad, f"{len(bad)} bars disagree; first: {bad[:3]}"
