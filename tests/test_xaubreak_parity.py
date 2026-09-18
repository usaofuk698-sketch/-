"""Parity between the Python breakout strategy and XauBreak_M5.mq5."""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from goldbot.backtest.types import Side
from goldbot.strategy.breakout_momentum import BreakoutMomentum, BreakoutMomentumParams

MQ5 = Path(__file__).resolve().parents[1] / "mt5" / "XauBreak_M5.mq5"

PARAM_MAP = {
    "InpRangeLookback": "range_lookback",
    "InpSqueezeMaxAtr": "squeeze_max_atr",
    "InpBreakMarginAtr": "break_margin_atr",
    "InpAtrPeriod": "atr_period",
    "InpMinBarRangeAtr": "min_bar_range_atr",
    "InpClosePositionMin": "close_position_min",
    "InpRegimeLookback": "regime_lookback",
    "InpAtrMinMult": "atr_min_mult",
    "InpAtrMaxMult": "atr_max_mult",
    "InpAdxPeriod": "adx_period",
    "InpAdxMin": "adx_min",
    "InpTrendEmaPeriod": "trend_ema_period",
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
    assert mq5_name in inputs, f"{mq5_name} missing from the EA"
    assert float(inputs[mq5_name]) == pytest.approx(
        float(getattr(BreakoutMomentumParams(), py_field))
    )


def test_breakeven_is_off_by_default():
    """The whole edge depends on winners reaching the target. An early
    break-even stop converts them to scratches and inverts the arithmetic."""
    assert parse_inputs()["InpUseBreakeven"] is False
    assert BreakoutMomentumParams().breakeven_at_r is None


def test_ea_refuses_a_small_reward_risk():
    """Guards against the exact edit that would recreate the losing design."""
    src = MQ5.read_text()
    assert "InpRewardRisk < 1.5" in src
    assert "wrong EA" in src


def test_range_excludes_the_current_bar():
    """If the breakout bar is part of its own range the level can never be
    cleared honestly. Python shifts; MQL5 starts the loop at index 1."""
    src = MQ5.read_text()
    assert "for(int k = 1; k <= InpRangeLookback" in src
    assert "rates[1].high, rangeLow = rates[1].low" in src


def test_ea_requires_a_close_beyond_the_level():
    src = MQ5.read_text()
    assert "close > rangeHigh + margin" in src
    assert "close < rangeLow  - margin" in src


def mql5_entry(i: int, feat, p: BreakoutMomentumParams):
    """Mirror of TryEntry() in XauBreak_M5.mq5."""
    a = {c: feat[c].to_numpy(float) for c in
         ("open", "high", "low", "close", "atr", "adx", "atr_med",
          "range_high", "range_low", "range_width", "close_pos", "bar_range", "trend_ema")}
    if i < max(p.regime_lookback, p.trend_ema_period, p.range_lookback) + 10:
        return None
    atr_v, med, adx_v = a["atr"][i], a["atr_med"][i], a["adx"][i]
    close = a["close"][i]
    rh, rl, width = a["range_high"][i], a["range_low"][i], a["range_width"][i]
    close_pos, bar_range, trend = a["close_pos"][i], a["bar_range"][i], a["trend_ema"][i]

    if any(not np.isfinite(v) for v in (atr_v, med, rh, rl, close_pos, trend)):
        return None
    if atr_v <= 0 or med <= 0:
        return None
    if not (p.atr_min_mult * med <= atr_v <= p.atr_max_mult * med):
        return None
    if p.adx_min > 0 and (not np.isfinite(adx_v) or adx_v < p.adx_min):
        return None
    if width > p.squeeze_max_atr * atr_v:
        return None
    if bar_range < p.min_bar_range_atr * atr_v:
        return None

    margin = p.break_margin_atr * atr_v
    up = close > rh + margin and close_pos >= p.close_position_min
    down = close < rl - margin and (1.0 - close_pos) >= p.close_position_min
    if p.require_trend_alignment:
        up = up and close > trend
        down = down and close < trend
    if up == down:
        return None

    if up:
        sl = min(rh - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
        return None if sl >= close else (Side.LONG, sl)
    sl = max(rl + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
    return None if sl <= close else (Side.SHORT, sl)


def test_entry_decisions_are_identical(long_bars):
    strategy = BreakoutMomentum()
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
    assert signals > 300, f"only {signals} signals"
    assert not bad, f"{len(bad)} bars disagree; first: {bad[:3]}"


def test_transliteration_catches_a_drifted_squeeze(long_bars):
    strategy = BreakoutMomentum()
    feat = strategy.prepare(long_bars)
    broken = BreakoutMomentumParams(squeeze_max_atr=1.0)
    n = sum(
        1
        for i in range(strategy.warmup, len(feat), 5)
        if (strategy.entry(i, feat) is None) != (mql5_entry(i, feat, broken) is None)
    )
    assert n > 0, "a drifted squeeze threshold went undetected"
