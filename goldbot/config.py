"""YAML configuration.

Everything that differs between brokers, accounts and experiments lives in a
config file, not in code. Costs in particular: the difference between a 0.15 and
a 0.45 spread decides whether an M5 gold system is viable, and that number has
to come from *your* broker's statement, not from a default in a repository.
"""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from .backtest.broker import SimBroker, SpreadModel
from .contract import Contract
from .risk import RiskConfig, SessionWindow
from .strategy.breakout_momentum import BreakoutMomentum, BreakoutMomentumParams
from .strategy.mean_reversion_scalper import (
    MeanReversionScalper,
    MeanReversionScalperParams,
)
from .strategy.trend_pullback import TrendPullback, TrendPullbackParams
from .strategy.volume_profile_wyckoff import (
    VolumeProfileWyckoff,
    VolumeProfileWyckoffParams,
)

STRATEGIES = {
    "trend_pullback": (TrendPullback, TrendPullbackParams),
    "mean_reversion_scalper": (MeanReversionScalper, MeanReversionScalperParams),
    "breakout_momentum": (BreakoutMomentum, BreakoutMomentumParams),
    "volume_profile_wyckoff": (VolumeProfileWyckoff, VolumeProfileWyckoffParams),
}


@dataclass
class AppConfig:
    symbol: str = "XAUUSD"
    timeframe: str = "5min"
    initial_capital: float = 10_000.0
    max_bars_in_trade: int | None = 96
    data_path: str | None = None
    data_timezone: str = "UTC"
    strategy_name: str = "trend_pullback"
    raw: dict = field(default_factory=dict)

    contract: Contract = field(default_factory=Contract)
    spread: SpreadModel = field(default_factory=SpreadModel.default_xauusd)
    slippage: float = 0.05
    swap_long_per_lot: float = 0.0
    swap_short_per_lot: float = 0.0
    risk: RiskConfig = field(default_factory=RiskConfig)
    strategy_params: dict = field(default_factory=dict)

    def build_strategy(self, overrides: dict | None = None):
        cls, params_cls = STRATEGIES[self.strategy_name]
        merged = {**self.strategy_params, **(overrides or {})}
        return cls(params_cls.from_dict(merged))

    def build_broker(self) -> SimBroker:
        return SimBroker(
            contract=self.contract,
            spread_model=self.spread,
            slippage=self.slippage,
            swap_long_per_lot=self.swap_long_per_lot,
            swap_short_per_lot=self.swap_short_per_lot,
        )


def _filter_kwargs(cls, data: dict) -> dict:
    allowed = {f.name for f in fields(cls)}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown {cls.__name__} keys: {sorted(unknown)}")
    return {k: v for k, v in data.items() if k in allowed}


def _parse_risk(data: dict) -> RiskConfig:
    data = dict(data)
    if "sessions" in data:
        data["sessions"] = tuple(
            SessionWindow(int(w["start_hour"]), int(w["end_hour"])) for w in data["sessions"]
        )
    if "trading_days" in data:
        data["trading_days"] = tuple(int(d) for d in data["trading_days"])
    if "news_blackout" in data:
        data["news_blackout"] = tuple(
            (pd.Timestamp(a), pd.Timestamp(b)) for a, b in data["news_blackout"]
        )
    return RiskConfig(**_filter_kwargs(RiskConfig, data))


def _parse_spread(data: dict) -> SpreadModel:
    if not data:
        return SpreadModel.default_xauusd()
    data = dict(data)
    if data.pop("preset", None) == "xauusd_default":
        base = SpreadModel.default_xauusd()
        for k, v in data.items():
            setattr(base, k, v)
        return base
    if "hour_multipliers" in data:
        data["hour_multipliers"] = {int(k): float(v) for k, v in data["hour_multipliers"].items()}
    return SpreadModel(**_filter_kwargs(SpreadModel, data))


def load(path: str | Path) -> AppConfig:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text()) or {}

    costs = raw.get("costs", {}) or {}
    strategy = raw.get("strategy", {}) or {}
    name = strategy.get("name", "trend_pullback")
    if name not in STRATEGIES:
        raise ValueError(f"unknown strategy {name!r}; have {sorted(STRATEGIES)}")

    data_cfg = raw.get("data", {}) or {}
    account = raw.get("account", {}) or {}

    cfg = AppConfig(
        symbol=raw.get("symbol", "XAUUSD"),
        timeframe=raw.get("timeframe", "5min"),
        initial_capital=float(account.get("initial_capital", 10_000.0)),
        max_bars_in_trade=raw.get("max_bars_in_trade", 96),
        data_path=data_cfg.get("path"),
        data_timezone=data_cfg.get("timezone", "UTC"),
        strategy_name=name,
        raw=raw,
        contract=Contract(**_filter_kwargs(Contract, raw.get("contract", {}) or {})),
        spread=_parse_spread(costs.get("spread", {}) or {}),
        slippage=float(costs.get("slippage", 0.05)),
        swap_long_per_lot=float(costs.get("swap_long_per_lot", 0.0)),
        swap_short_per_lot=float(costs.get("swap_short_per_lot", 0.0)),
        risk=_parse_risk(raw.get("risk", {}) or {}),
        strategy_params=strategy.get("params", {}) or {},
    )
    return cfg
