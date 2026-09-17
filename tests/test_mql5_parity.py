"""Parity between the Python strategy and the MQL5 Expert Advisor.

The EA in ``mt5/GoldBot_M5.mq5`` is a hand port of
``goldbot/strategy/trend_pullback.py``. Nothing in this repository compiles
MQL5, so a porting bug -- a flipped comparison, a lookback window off by one,
a default that drifted -- would otherwise be invisible until it cost money
live.

These tests close most of that gap in two ways:

1. **Default parity.** Every tunable is parsed straight out of the ``.mq5``
   source and compared against the Python dataclass. The two files cannot
   silently drift apart.
2. **Decision parity.** The EA's entry logic is transliterated below, reading
   its windows exactly the way the MQL5 loops do (``rates[0]`` is the last
   closed bar, the swing loop spans ``i < SwingLookback``, and so on), and run
   against the same features. Any disagreement on any bar fails.

What this does NOT prove: that the file compiles, or that MetaTrader's own
iMA/iATR/iRSI/iADX match this project's indicator implementations bar for bar.
Both sides here use the Python indicators, so this isolates the decision logic,
which is where porting bugs actually live.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pytest

from goldbot.backtest.types import Side
from goldbot.strategy.trend_pullback import TrendPullback, TrendPullbackParams

MQ5 = Path(__file__).resolve().parents[1] / "mt5" / "GoldBot_M5.mq5"

# MQL5 input name -> Python dataclass field
PARAM_MAP = {
    "InpEmaFast": "ema_fast",
    "InpEmaSlow": "ema_slow",
    "InpEmaTrend": "ema_trend",
    "InpAdxPeriod": "adx_period",
    "InpAdxMin": "adx_min",
    "InpAtrPeriod": "atr_period",
    "InpRegimeLookback": "regime_lookback",
    "InpAtrMinMult": "atr_min_mult",
    "InpAtrMaxMult": "atr_max_mult",
    "InpRsiPeriod": "rsi_period",
    "InpRsiPullbackLong": "rsi_pullback_long",
    "InpRsiPullbackShort": "rsi_pullback_short",
    "InpPullbackLookback": "pullback_lookback",
    "InpSwingLookback": "swing_lookback",
    "InpStopAtrBuffer": "stop_atr_buffer",
    "InpMinStopAtrMult": "min_stop_atr_mult",
    "InpRewardRisk": "reward_risk",
    "InpBreakevenAtR": "breakeven_at_r",
    "InpBreakevenOffAtr": "breakeven_offset_atr",
    "InpTrailAtR": "trail_at_r",
    "InpTrailAtrMult": "trail_atr_mult",
}


def parse_mq5_inputs() -> dict[str, float]:
    """Pull ``input <type> Name = value;`` defaults out of the EA source."""
    src = MQ5.read_text()
    pattern = re.compile(
        r"^\s*input\s+(?:double|int|long|bool)\s+(\w+)\s*=\s*([-\w.]+)\s*;", re.M
    )
    out: dict[str, float] = {}
    for name, raw in pattern.findall(src):
        if raw in ("true", "false"):
            out[name] = raw == "true"
        else:
            out[name] = float(raw)
    return out


def test_mq5_source_exists_and_parses():
    assert MQ5.exists(), f"missing {MQ5}"
    inputs = parse_mq5_inputs()
    assert len(inputs) > 25, f"only parsed {len(inputs)} inputs; the regex is wrong"


@pytest.mark.parametrize("mq5_name,py_field", sorted(PARAM_MAP.items()))
def test_default_parameters_match(mq5_name, py_field):
    """A default changed in one file and not the other is a real divergence."""
    inputs = parse_mq5_inputs()
    assert mq5_name in inputs, f"{mq5_name} not found in the EA source"
    py_value = getattr(TrendPullbackParams(), py_field)
    assert float(inputs[mq5_name]) == pytest.approx(float(py_value)), (
        f"{mq5_name}={inputs[mq5_name]} but {py_field}={py_value}"
    )


def test_risk_defaults_match_the_yaml_config():
    import yaml

    cfg = yaml.safe_load((MQ5.parents[1] / "config" / "default.yaml").read_text())
    inputs = parse_mq5_inputs()
    risk = cfg["risk"]
    assert inputs["InpRiskPercent"] == pytest.approx(risk["risk_per_trade_pct"])
    assert inputs["InpMaxDailyLossPct"] == pytest.approx(risk["max_daily_loss_pct"])
    assert inputs["InpMaxTradesPerDay"] == pytest.approx(risk["max_trades_per_day"])
    assert inputs["InpMaxConsecLosses"] == pytest.approx(risk["max_consecutive_losses"])
    assert inputs["InpSessionStartHour"] == pytest.approx(risk["sessions"][0]["start_hour"])
    assert inputs["InpSessionEndHour"] == pytest.approx(risk["sessions"][0]["end_hour"])
    assert inputs["InpFlatByHour"] == pytest.approx(risk["flat_by_hour"])
    assert inputs["InpNoNewTradesAfter"] == pytest.approx(risk["no_new_trades_after_hour"])


# --------------------------------------------------------------------------
# Transliteration of the EA's TryEntry(), indexed the way the MQL5 code is.
# --------------------------------------------------------------------------
def mql5_entry(i: int, feat, p: TrendPullbackParams):
    """Mirror of ``TryEntry`` in GoldBot_M5.mq5.

    In the EA, ``rates[0]`` / ``buffer[0]`` are the last CLOSED bar. Here bar
    ``i`` plays that role, so ``rates[k]`` maps to ``i - k``.
    """
    c = feat["close"].to_numpy(float)
    o = feat["open"].to_numpy(float)
    hi = feat["high"].to_numpy(float)
    lo = feat["low"].to_numpy(float)
    atr = feat["atr"].to_numpy(float)
    adx = feat["adx"].to_numpy(float)
    rsi = feat["rsi"].to_numpy(float)
    ema_f = feat["ema_fast"].to_numpy(float)
    ema_s = feat["ema_slow"].to_numpy(float)
    ema_t = feat["ema_trend"].to_numpy(float)
    atr_med = feat["atr_med"].to_numpy(float)

    if i < max(p.regime_lookback, p.ema_trend) + 10:
        return None
    vals = (atr[i], adx[i], rsi[i], ema_f[i], ema_s[i], ema_t[i], atr_med[i])
    if any(not np.isfinite(v) for v in vals):
        return None

    atr_v = atr[i]
    if atr_v <= 0:
        return None

    med = atr_med[i]                                  # MedianAtr(RegimeLookback)
    if med <= 0:
        return None
    if atr_v < p.atr_min_mult * med:
        return None
    if atr_v > p.atr_max_mult * med:
        return None
    if adx[i] < p.adx_min:
        return None

    close, open_ = c[i], o[i]
    prev_hi, prev_lo = hi[i - 1], lo[i - 1]           # rates[1]

    # for(k = 0; k < SwingLookback; k++)  over rates[k]
    swing_low = min(lo[i - k] for k in range(p.swing_lookback))
    swing_high = max(hi[i - k] for k in range(p.swing_lookback))

    # for(k = 0; k < PullbackLookback; k++) over rsi[k]
    rsi_min = min(rsi[i - k] for k in range(p.pullback_lookback))
    rsi_max = max(rsi[i - k] for k in range(p.pullback_lookback))

    long_ok = (
        ema_f[i] > ema_s[i] and close > ema_t[i]
        and rsi_min <= p.rsi_pullback_long and rsi[i] > p.rsi_pullback_long
        and close > prev_hi and close > open_
    )
    short_ok = (
        ema_f[i] < ema_s[i] and close < ema_t[i]
        and rsi_max >= p.rsi_pullback_short and rsi[i] < p.rsi_pullback_short
        and close < prev_lo and close < open_
    )
    if long_ok == short_ok:
        return None

    if long_ok:
        sl = min(swing_low - p.stop_atr_buffer * atr_v, close - p.min_stop_atr_mult * atr_v)
        if sl >= close:
            return None
        return (Side.LONG, sl)

    sl = max(swing_high + p.stop_atr_buffer * atr_v, close + p.min_stop_atr_mult * atr_v)
    if sl <= close:
        return None
    return (Side.SHORT, sl)


def test_entry_decisions_are_identical(long_bars):
    """Every bar must produce the same decision in both implementations."""
    strategy = TrendPullback()
    p = strategy.params
    feat = strategy.prepare(long_bars)

    disagreements = []
    signals = 0
    start = strategy.warmup
    for i in range(start, len(feat)):
        py_sig = strategy.entry(i, feat)
        mq_sig = mql5_entry(i, feat, p)

        py_key = None if py_sig is None else (py_sig.side, round(py_sig.stop_loss, 6))
        mq_key = None if mq_sig is None else (mq_sig[0], round(mq_sig[1], 6))
        if py_key is not None:
            signals += 1
        if py_key != mq_key:
            disagreements.append((i, feat.index[i], py_key, mq_key))

    assert signals > 50, f"only {signals} signals; the test data exercises too little"
    assert not disagreements, (
        f"{len(disagreements)} of {len(feat) - start} bars disagree. "
        f"First 3: {disagreements[:3]}"
    )


def test_transliteration_would_catch_a_flipped_comparison(long_bars):
    """Guard the guard: if the port drifts, this test must actually fail."""
    strategy = TrendPullback()
    feat = strategy.prepare(long_bars)
    broken = TrendPullbackParams(rsi_pullback_long=55.0)  # drifted default

    mismatches = 0
    for i in range(strategy.warmup, len(feat), 7):
        a = strategy.entry(i, feat)
        b = mql5_entry(i, feat, broken)
        ka = None if a is None else (a.side, round(a.stop_loss, 6))
        kb = None if b is None else (b[0], round(b[1], 6))
        if ka != kb:
            mismatches += 1
    assert mismatches > 0, "a drifted parameter went undetected -- the test is asleep"
