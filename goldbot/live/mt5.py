"""MetaTrader 5 feed and executor.

``MetaTrader5`` is a Windows-only package and is imported lazily, so the rest of
the toolkit -- research, backtesting, validation -- runs anywhere without it.

Read :mod:`goldbot.live.runner` before wiring this up. In particular, verify
that ``data.timezone`` in the config matches your broker's server time: MT5
returns bar stamps in server time, and if the session filter is silently off by
two or three hours the live system trades different hours than the one you
tested.
"""
from __future__ import annotations

import logging

import pandas as pd

from ..backtest.types import Side
from .runner import BrokerPosition, Executor, MarketFeed, OrderRequest

log = logging.getLogger("goldbot.live.mt5")

_TIMEFRAMES = {
    "1min": "TIMEFRAME_M1", "5min": "TIMEFRAME_M5", "15min": "TIMEFRAME_M15",
    "30min": "TIMEFRAME_M30", "1h": "TIMEFRAME_H1", "4h": "TIMEFRAME_H4", "1d": "TIMEFRAME_D1",
}


def _mt5():
    try:
        import MetaTrader5 as mt5  # type: ignore
    except ImportError as exc:  # pragma: no cover - platform dependent
        raise RuntimeError(
            "MetaTrader5 is not installed. It is Windows-only:\n"
            "    pip install MetaTrader5\n"
            "Backtesting and validation do not need it."
        ) from exc
    return mt5


def initialize(login: int | None = None, password: str | None = None, server: str | None = None):
    """Connect to a running MT5 terminal.

    Credentials are arguments, never config-file entries: a YAML file gets
    committed to git eventually, and a trading password in a repository is a
    problem you only get to have once. Read them from environment variables or
    a secrets manager at the call site.
    """
    mt5 = _mt5()
    ok = (
        mt5.initialize(login=login, password=password, server=server)
        if login else mt5.initialize()
    )
    if not ok:
        raise RuntimeError(f"MT5 initialize failed: {mt5.last_error()}")
    return mt5


class Mt5Feed(MarketFeed):
    def fetch(self, symbol: str, timeframe: str, count: int) -> pd.DataFrame:
        mt5 = _mt5()
        if timeframe not in _TIMEFRAMES:
            raise ValueError(f"unsupported timeframe {timeframe!r}; have {sorted(_TIMEFRAMES)}")
        tf = getattr(mt5, _TIMEFRAMES[timeframe])
        rates = mt5.copy_rates_from_pos(symbol, tf, 0, count)
        if rates is None or len(rates) == 0:
            raise RuntimeError(f"no rates for {symbol}: {mt5.last_error()}")
        df = pd.DataFrame(rates)
        df["time"] = pd.to_datetime(df["time"], unit="s")
        df = df.set_index("time").rename(columns={"tick_volume": "volume"})
        return df[["open", "high", "low", "close", "volume"]]


class Mt5Executor(Executor):
    """Live order routing. Constructing this does not arm it -- ``LiveRunner``
    still requires ``confirm_live=True``."""

    def __init__(self, deviation_points: int = 20):
        self.deviation = deviation_points

    def equity(self) -> float:
        mt5 = _mt5()
        info = mt5.account_info()
        if info is None:
            raise RuntimeError(f"account_info failed: {mt5.last_error()}")
        return float(info.equity)

    def positions(self, symbol: str, magic: int) -> list[BrokerPosition]:
        mt5 = _mt5()
        raw = mt5.positions_get(symbol=symbol) or ()
        out = []
        for p in raw:
            if p.magic != magic:
                continue  # never manage a position this bot did not open
            out.append(
                BrokerPosition(
                    ticket=int(p.ticket), symbol=p.symbol,
                    side=Side.LONG if p.type == mt5.POSITION_TYPE_BUY else Side.SHORT,
                    lots=float(p.volume), entry_price=float(p.price_open),
                    stop_loss=float(p.sl), take_profit=float(p.tp) or None, magic=int(p.magic),
                )
            )
        return out

    def open(self, req: OrderRequest) -> BrokerPosition | None:
        mt5 = _mt5()
        tick = mt5.symbol_info_tick(req.symbol)
        if tick is None:
            raise RuntimeError(f"no tick for {req.symbol}: {mt5.last_error()}")
        is_long = req.side is Side.LONG
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": req.symbol,
            "volume": float(req.lots),
            "type": mt5.ORDER_TYPE_BUY if is_long else mt5.ORDER_TYPE_SELL,
            "price": tick.ask if is_long else tick.bid,
            "sl": float(req.stop_loss),
            "deviation": self.deviation,
            "magic": req.magic,
            "comment": req.comment,
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        }
        if req.take_profit is not None:
            request["tp"] = float(req.take_profit)

        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            log.error("order_send failed: %s", getattr(result, "comment", mt5.last_error()))
            return None
        log.info("LIVE open #%s %s %.2f lots @ %.2f",
                 result.order, req.side.value, req.lots, result.price)
        return BrokerPosition(
            ticket=int(result.order), symbol=req.symbol, side=req.side, lots=req.lots,
            entry_price=float(result.price), stop_loss=req.stop_loss,
            take_profit=req.take_profit, magic=req.magic,
        )

    def modify(self, ticket: int, stop_loss: float, take_profit: float | None) -> bool:
        mt5 = _mt5()
        req = {"action": mt5.TRADE_ACTION_SLTP, "position": int(ticket), "sl": float(stop_loss)}
        if take_profit is not None:
            req["tp"] = float(take_profit)
        result = mt5.order_send(req)
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        if not ok:
            log.error("modify #%d failed: %s", ticket, getattr(result, "comment", None))
        return ok

    def close(self, ticket: int) -> bool:
        mt5 = _mt5()
        matches = [p for p in (mt5.positions_get(ticket=ticket) or ())]
        if not matches:
            return False
        p = matches[0]
        tick = mt5.symbol_info_tick(p.symbol)
        is_long = p.type == mt5.POSITION_TYPE_BUY
        result = mt5.order_send({
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": p.symbol,
            "volume": float(p.volume),
            "type": mt5.ORDER_TYPE_SELL if is_long else mt5.ORDER_TYPE_BUY,
            "position": int(ticket),
            "price": tick.bid if is_long else tick.ask,
            "deviation": self.deviation,
            "magic": int(p.magic),
            "type_time": mt5.ORDER_TIME_GTC,
            "type_filling": mt5.ORDER_FILLING_IOC,
        })
        ok = result is not None and result.retcode == mt5.TRADE_RETCODE_DONE
        if not ok:
            log.error("close #%d failed: %s", ticket, getattr(result, "comment", None))
        return ok
