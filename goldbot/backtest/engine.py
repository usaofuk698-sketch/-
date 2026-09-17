"""Event-driven bar loop.

Bar ``i`` is processed in the order the events actually happen in the market,
and that ordering is the reason the numbers can be trusted:

1. **Open.** A signal produced at the close of bar ``i-1`` is filled here, at
   bar ``i``'s open. A strategy can never act on the bar it is looking at.
2. **Intrabar.** The open position's stop and target -- both fixed *before* this
   bar existed -- are tested against the bar's range.
3. **Close.** Session/risk rules may flatten. Then the strategy sees the newly
   closed bar: it may tighten the stop (effective from bar ``i+1``) and may emit
   a signal (executable at bar ``i+1``'s open).

Step 3 never affects steps 1-2 of the same bar. That single constraint is what
separates a backtest from a curve-fitted fantasy, and
:mod:`goldbot.validation.lookahead` proves it holds rather than trusting it.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from ..contract import XAUUSD, Contract
from ..risk import RiskManager
from ..strategy.base import Strategy
from .broker import SimBroker
from .types import ExitReason, Position, Side, Signal, Trade

REQUIRED_COLUMNS = ("open", "high", "low", "close")


@dataclass
class BacktestResult:
    trades: pd.DataFrame
    equity: pd.Series
    initial_capital: float
    blocked: Counter = field(default_factory=Counter)
    strategy_name: str = ""
    params: dict = field(default_factory=dict)

    @property
    def final_equity(self) -> float:
        return float(self.equity.iloc[-1]) if len(self.equity) else self.initial_capital


class BacktestEngine:
    def __init__(
        self,
        strategy: Strategy,
        risk: RiskManager,
        broker: SimBroker | None = None,
        contract: Contract = XAUUSD,
        initial_capital: float = 10_000.0,
        max_bars_in_trade: int | None = 96,  # 8h on M5; stale trades are dead money
    ):
        self.strategy = strategy
        self.risk = risk
        self.contract = contract
        self.broker = broker or SimBroker(contract=contract)
        self.initial_capital = initial_capital
        self.max_bars_in_trade = max_bars_in_trade

    # ------------------------------------------------------------------ run
    def run(self, df: pd.DataFrame) -> BacktestResult:
        self._validate(df)
        feat = self.strategy.prepare(df)

        idx = feat.index
        o = feat["open"].to_numpy(float)
        h = feat["high"].to_numpy(float)
        l = feat["low"].to_numpy(float)
        c = feat["close"].to_numpy(float)
        n = len(feat)

        cash = self.initial_capital
        equity_curve = np.full(n, np.nan)
        pos: Position | None = None
        pending: Signal | None = None
        trades: list[Trade] = []
        blocked: Counter = Counter()
        prev_hour: int | None = None

        start = max(self.strategy.warmup, 1)

        for i in range(start, n):
            ts = idx[i]
            hour = ts.hour
            spread = self.broker.spread_model.spread_at(hour)
            equity = cash + self._unrealised(pos, c[i], spread)
            self.risk.on_bar(ts, equity)

            # --- 1. fill a pending entry at this bar's open -------------------
            if pending is not None:
                if pos is None:
                    pos, why = self._try_open(pending, i, ts, o[i], spread, cash)
                    if pos is None and why:
                        blocked[why] += 1
                else:
                    blocked["position_already_open"] += 1
                pending = None  # a signal is good for exactly one bar

            # --- swap financing on rollover ---------------------------------
            ro = self.broker.spread_model.rollover_hour
            if pos is not None and ro is not None and hour == ro and prev_hour != ro:
                fee = self.broker.swap_for_night(pos.side, pos.lots)
                pos.swap_paid += fee
                cash -= fee
            prev_hour = hour

            # --- 2. intrabar stop / target (levels set before this bar) -------
            if pos is not None:
                self.broker.update_excursions(pos, h[i], l[i], spread)
                hit = self.broker.check_exit(pos, o[i], h[i], l[i], c[i], spread)
                if hit is not None:
                    price, reason = hit
                    cash, trade = self._close(pos, price, reason, ts, i, cash)
                    trades.append(trade)
                    pos = None

            # --- 3a. risk/session forced flat at this bar's close -------------
            if pos is not None:
                stale = (
                    self.max_bars_in_trade is not None
                    and (i - pos.entry_bar) >= self.max_bars_in_trade
                )
                if self.risk.must_flatten(ts) or stale:
                    reason = (
                        ExitReason.MAX_BARS if stale and not self.risk.must_flatten(ts)
                        else ExitReason.DAILY_LOSS_LIMIT if self.risk.state.halted
                        else ExitReason.SESSION_CLOSE
                    )
                    price = self.broker.market_exit_fill(pos.side, c[i], spread)
                    cash, trade = self._close(pos, price, reason, ts, i, cash)
                    trades.append(trade)
                    pos = None

            # --- 3b. strategy revises the stop, effective NEXT bar ------------
            if pos is not None:
                new_sl, new_tp = self.strategy.manage(i, feat, pos)
                pos.stop_loss = self._tighten_only(pos, new_sl)
                pos.take_profit = new_tp

            # --- 3c. strategy emits a signal for NEXT bar's open --------------
            if pos is None and i + 1 < n:
                sig = self.strategy.entry(i, feat)
                if sig is not None:
                    pending = sig

            equity_curve[i] = cash + self._unrealised(pos, c[i], spread)

        # --- liquidate anything still open at the end of the data ------------
        if pos is not None:
            i = n - 1
            spread = self.broker.spread_model.spread_at(idx[i].hour)
            price = self.broker.market_exit_fill(pos.side, c[i], spread)
            cash, trade = self._close(pos, price, ExitReason.END_OF_DATA, idx[i], i, cash)
            trades.append(trade)
            equity_curve[i] = cash

        equity = pd.Series(equity_curve, index=idx, name="equity")
        equity = equity.ffill().fillna(self.initial_capital)

        trades_df = pd.DataFrame([t.as_dict() for t in trades])
        return BacktestResult(
            trades=trades_df,
            equity=equity,
            initial_capital=self.initial_capital,
            blocked=blocked,
            strategy_name=self.strategy.name,
            params=self.strategy.params.to_dict(),
        )

    # -------------------------------------------------------------- helpers
    @staticmethod
    def _validate(df: pd.DataFrame) -> None:
        missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise ValueError(f"missing OHLC columns: {missing}")
        if not isinstance(df.index, pd.DatetimeIndex):
            raise TypeError("index must be a DatetimeIndex")
        if not df.index.is_monotonic_increasing:
            raise ValueError("index must be sorted ascending")
        if df.index.has_duplicates:
            raise ValueError("index contains duplicate timestamps")
        bad = df["high"] < df["low"]
        if bool(bad.any()):
            raise ValueError(f"{int(bad.sum())} bars have high < low")

    def _unrealised(self, pos: Position | None, close_bid: float, spread: float) -> float:
        if pos is None:
            return 0.0
        mark = close_bid if pos.side is Side.LONG else close_bid + spread
        return self.broker.gross_pnl(pos, mark)

    @staticmethod
    def _tighten_only(pos: Position, new_sl: float) -> float:
        """Guard the strategy contract: a revised stop may only reduce risk."""
        if not np.isfinite(new_sl):
            return pos.stop_loss
        if pos.side is Side.LONG:
            return max(pos.stop_loss, new_sl)
        return min(pos.stop_loss, new_sl)

    def _try_open(
        self,
        sig: Signal,
        i: int,
        ts: pd.Timestamp,
        bar_open: float,
        spread: float,
        equity: float,
    ) -> tuple[Position | None, str]:
        ok, why = self.risk.may_open(ts, open_positions=0)
        if not ok:
            return None, why

        fill = self.broker.entry_fill(sig.side, bar_open, spread)

        # The stop was computed on the previous close. If this bar opened at or
        # through it, the trade is already dead -- do not enter it.
        if sig.side is Side.LONG and fill <= sig.stop_loss:
            return None, "gapped_through_stop"
        if sig.side is Side.SHORT and fill >= sig.stop_loss:
            return None, "gapped_through_stop"
        if sig.take_profit is not None:
            if sig.side is Side.LONG and sig.take_profit <= fill:
                return None, "gapped_past_target"
            if sig.side is Side.SHORT and sig.take_profit >= fill:
                return None, "gapped_past_target"

        lots, why = self.risk.size(equity, fill, sig.stop_loss)
        if lots <= 0:
            return None, why or "zero_size"

        return (
            Position(
                side=sig.side,
                lots=lots,
                entry_price=fill,
                entry_time=ts,
                stop_loss=sig.stop_loss,
                take_profit=sig.take_profit,
                entry_bar=i,
                initial_risk=abs(fill - sig.stop_loss),
                reason=sig.reason,
            ),
            "",
        )

    def _close(
        self,
        pos: Position,
        exit_price: float,
        reason: ExitReason,
        ts: pd.Timestamp,
        i: int,
        cash: float,
    ) -> tuple[float, Trade]:
        gross = self.broker.gross_pnl(pos, exit_price)
        comm = self.broker.commission(pos.lots)
        net = gross - comm - pos.swap_paid
        cash += gross - comm  # swap was already deducted as it accrued

        risk_usd = pos.initial_risk * self.contract.value_per_price_unit(pos.lots)
        r_multiple = net / risk_usd if risk_usd > 0 else 0.0

        self.risk.on_trade_closed(net, cash)
        return cash, Trade(
            symbol=self.contract.symbol,
            side=pos.side,
            lots=pos.lots,
            entry_time=pos.entry_time,
            exit_time=ts,
            entry_price=pos.entry_price,
            exit_price=exit_price,
            stop_loss=pos.stop_loss,
            take_profit=pos.take_profit,
            gross_pnl=gross,
            commission=comm,
            swap=pos.swap_paid,
            net_pnl=net,
            r_multiple=r_multiple,
            exit_reason=reason,
            bars_held=i - pos.entry_bar,
            mae=pos.mae,
            mfe=pos.mfe,
            equity_after=cash,
            reason=pos.reason,
        )
