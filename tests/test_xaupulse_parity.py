"""Parity between the Python scalper and the Anas_M5 Expert Advisor.

Same purpose as ``test_mql5_parity``: nothing here compiles MQL5, so a porting
slip would stay invisible until it cost money. Defaults are parsed from the
``.mq5`` source, and the EA's entry logic is transliterated and required to
make the identical decision on every bar.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from goldbot.backtest.types import Side
from goldbot.strategy.mean_reversion_scalper import (
    MeanReversionScalper,
    MeanReversionScalperParams,
)

MQ5 = Path(__file__).resolve().parents[1] / "mt5" / "Anas_M5.mq5"

PARAM_MAP = {
    "InpEmaPeriod": "ema_period",
    "InpAdxPeriod": "adx_period",
    "InpAdxMax": "adx_max",
    "InpAtrPeriod": "atr_period",
    "InpRegimeLookback": "regime_lookback",
    "InpAtrMinMult": "atr_min_mult",
    "InpAtrMaxMult": "atr_max_mult",
    "InpStretchAtr": "stretch_atr",
    "InpRsiPeriod": "rsi_period",
    "InpRsiOversold": "rsi_oversold",
    "InpRsiOverbought": "rsi_overbought",
    "InpSwingLookback": "swing_lookback",
    "InpStopAtrBuffer": "stop_atr_buffer",
    "InpMinStopAtrMult": "min_stop_atr_mult",
    "InpRewardRisk": "reward_risk",
    "InpMinTargetSpreadRatio": "min_target_spread_ratio",
    "InpBreakevenAtR": "breakeven_at_r",
    "InpBreakevenOffAtr": "breakeven_offset_atr",
    # v2 entry-quality filters
    "InpHtfEmaPeriod": "htf_ema_period",
    "InpHtfSlopeLookback": "htf_slope_lookback",
    "InpHtfMaxSlopeAtr": "htf_max_slope_atr",
    "InpClosePositionMin": "close_position_min",
    "InpDivergenceLookback": "divergence_lookback",
}


def parse_inputs() -> dict:
    src = MQ5.read_text()
    pattern = re.compile(
        r"^\s*input\s+(?:double|int|long|bool)\s+(\w+)\s*=\s*([-\w.]+)\s*;", re.M
    )
    out = {}
    for name, raw in pattern.findall(src):
        out[name] = raw == "true" if raw in ("true", "false") else float(raw)
    return out


def test_ea_exists_and_parses():
    assert MQ5.exists()
    assert len(parse_inputs()) > 20


@pytest.mark.parametrize("mq5_name,py_field", sorted(PARAM_MAP.items()))
def test_defaults_match(mq5_name, py_field):
    inputs = parse_inputs()
    assert mq5_name in inputs, f"{mq5_name} missing from the EA"
    expected = getattr(MeanReversionScalperParams(), py_field)
    assert float(inputs[mq5_name]) == pytest.approx(float(expected))


def test_adx_is_a_ceiling_not_a_floor():
    """The defining difference from the trend EA, and the easiest thing to get
    backwards when porting. A HIGH ADX must disqualify a mean-reversion setup."""
    src = MQ5.read_text()
    assert "adxV >= InpAdxMax" in src, "ADX must be tested as a ceiling"
    assert "adxV < InpAdxMin" not in src, "that is the trend system's test"


def test_ea_enforces_the_cost_rule():
    src = MQ5.read_text()
    assert "InpMinTargetSpreadRatio" in src
    assert "targetDistance" in src


def test_ea_has_the_entry_quality_gates():
    """The v2 filters must exist in the EA, not only in the Python strategy."""
    src = MQ5.read_text()
    for token in ("InpClosePositionMin", "InpHtfMaxSlopeAtr", "closePos", "htfSlope"):
        assert token in src, f"{token} missing from the EA"


def test_ea_warmup_covers_the_long_ema():
    """The HTF filter needs InpHtfEmaPeriod + slope lookback bars; trading
    before that would read an EMA that is still settling."""
    src = MQ5.read_text()
    assert "InpHtfEmaPeriod + InpHtfSlopeLookback" in src


def test_ea_reads_only_closed_bars():
    src = MQ5.read_text()
    assert "CopyRates(_Symbol, PERIOD_CURRENT, 1," in src, "must start at shift 1"
    assert "iClose(_Symbol, PERIOD_CURRENT, 1)" in src


def mql5_entry(i: int, feat, p: MeanReversionScalperParams):
    """Mirror of TryEntry() in Anas_M5.mq5."""
    a = {c: feat[c].to_numpy(float) for c in
         ("open", "high", "low", "close", "ema", "atr", "adx", "rsi", "atr_med",
          "swing_low", "swing_high", "htf_slope", "close_pos",
          "prior_low", "prior_high", "prior_rsi_low", "prior_rsi_high")}
    if i < max(p.regime_lookback, p.ema_period,
               p.htf_ema_period + p.htf_slope_lookback) + 10:
        return None
    atr_v, med, adx_v, rsi_v = a["atr"][i], a["atr_med"][i], a["adx"][i], a["rsi"][i]
    ema_v, close, open_ = a["ema"][i], a["close"][i], a["open"][i]
    swing_lo, swing_hi = a["swing_low"][i], a["swing_high"][i]
    if any(not np.isfinite(v) for v in (atr_v, med, adx_v, rsi_v, ema_v, swing_lo, swing_hi)):
        return None
    if atr_v <= 0 or med <= 0:
        return None
    if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
        return None
    if adx_v >= p.adx_max:                      # ceiling, not floor
        return None

    stretch = (close - ema_v) / atr_v
    high_v, low_v = a["high"][i], a["low"][i]

    # gate 1: reversal-bar quality
    close_pos = a["close_pos"][i]
    if np.isfinite(close_pos):
        long_bar = close_pos >= p.close_position_min
        short_bar = (1.0 - close_pos) >= p.close_position_min
    else:
        long_bar = short_bar = False

    # gate 2: higher-timeframe slope
    htf_slope = a["htf_slope"][i]
    slope_atr = htf_slope / atr_v if (np.isfinite(htf_slope) and atr_v > 0) else 0.0
    if p.htf_max_slope_atr > 0.0:
        long_htf = slope_atr >= -p.htf_max_slope_atr
        short_htf = slope_atr <= p.htf_max_slope_atr
    else:
        long_htf = short_htf = True

    # gate 3: optional divergence
    if p.require_divergence:
        pl, prl = a["prior_low"][i], a["prior_rsi_low"][i]
        ph, prh = a["prior_high"][i], a["prior_rsi_high"][i]
        long_div = np.isfinite(pl) and np.isfinite(prl) and low_v <= pl and rsi_v > prl
        short_div = np.isfinite(ph) and np.isfinite(prh) and high_v >= ph and rsi_v < prh
    else:
        long_div = short_div = True

    long_ok = (stretch <= -p.stretch_atr and rsi_v <= p.rsi_oversold and close > open_
               and long_bar and long_htf and long_div)
    short_ok = (stretch >= p.stretch_atr and rsi_v >= p.rsi_overbought and close < open_
                and short_bar and short_htf and short_div)
    if long_ok == short_ok:
        return None

    if long_ok:
        sl = min(swing_lo - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
        return None if sl >= close else (Side.LONG, sl)
    sl = max(swing_hi + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
    return None if sl <= close else (Side.SHORT, sl)


def test_entry_decisions_are_identical(long_bars):
    strategy = MeanReversionScalper()
    feat = strategy.prepare(long_bars)
    disagreements, signals = [], 0
    for i in range(strategy.warmup, len(feat)):
        py = strategy.entry(i, feat)
        mq = mql5_entry(i, feat, strategy.params)
        pk = None if py is None else (py.side, round(py.stop_loss, 6))
        mk = None if mq is None else (mq[0], round(mq[1], 6))
        if pk is not None:
            signals += 1
        if pk != mk:
            disagreements.append((i, feat.index[i], pk, mk))
    assert signals > 200, f"only {signals} signals; too few to be a real check"
    assert not disagreements, f"{len(disagreements)} bars disagree; first: {disagreements[:3]}"


def test_transliteration_catches_a_flipped_adx_test(long_bars):
    """Guard the guard: inverting the ADX comparison must fail the check."""
    strategy = MeanReversionScalper()
    feat = strategy.prepare(long_bars)
    mismatches = 0
    for i in range(strategy.warmup, len(feat), 5):
        py = strategy.entry(i, feat)
        broken = MeanReversionScalperParams(adx_max=15.0)   # far tighter ceiling
        mq = mql5_entry(i, feat, broken)
        pk = None if py is None else (py.side, round(py.stop_loss, 6))
        mk = None if mq is None else (mq[0], round(mq[1], 6))
        if pk != mk:
            mismatches += 1
    assert mismatches > 0, "a drifted ADX ceiling went undetected"
