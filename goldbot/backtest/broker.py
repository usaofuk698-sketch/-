"""Simulated broker: spread, slippage, commission, swap and intrabar fills.

The modelling choices here are deliberately pessimistic. A backtest is only
useful if it is harder than reality, and the three assumptions below are where
optimistic engines manufacture most of their imaginary profit:

1.  **Quotes are two-sided.** OHLC input is treated as the *bid* series. A long
    is bought at the ask and sold at the bid; a short is the mirror. On gold a
    0.25 spread against a 3.00 stop is 8% of the risk on every single trade --
    ignoring it is not a rounding error.
2.  **Ambiguous bars resolve against you.** When a bar's range covers both the
    stop and the target, the true sequence is unknowable from OHLC alone, so the
    stop is taken first. Assuming the target instead is the single most common
    way a losing strategy backtests as a winner.
3.  **Gaps fill at the gap, not the level.** If a bar opens through the stop,
    the fill is the open. Stops are market orders once triggered and take
    adverse slippage; targets are limit orders and do not.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .types import ExitReason, Position, Side


@dataclass
class SpreadModel:
    """Hour-of-day spread model, in price units (USD per ounce for XAUUSD).

    Gold's spread is not constant. It is tightest across the London/New York
    overlap and blows out around the daily rollover and through thin Asian
    hours. Trading costs that ignore this flatter any strategy that happens to
    trade the quiet hours.
    """

    base: float = 0.20
    hour_multipliers: dict[int, float] = field(default_factory=dict)
    rollover_hour: int | None = 0
    rollover_spread: float = 3.00

    @classmethod
    def default_xauusd(cls) -> "SpreadModel":
        """A conservative retail profile, keyed by UTC hour."""
        quiet = {h: 2.5 for h in (22, 23, 1, 2, 3, 4, 5)}  # Asia / post-rollover
        mid = {h: 1.5 for h in (6, 7, 20, 21)}
        liquid = {h: 1.0 for h in (8, 9, 10, 11, 12, 13, 14, 15, 16, 17)}
        late = {h: 1.8 for h in (18, 19)}
        return cls(base=0.20, hour_multipliers={**quiet, **mid, **liquid, **late})

    def spread_at(self, hour: int) -> float:
        if self.rollover_hour is not None and hour == self.rollover_hour:
            return self.rollover_spread
        return self.base * self.hour_multipliers.get(hour, 1.0)


@dataclass
class SimBroker:
    """Fill engine. Stateless with respect to positions; the engine owns those."""

    contract: object  # goldbot.contract.Contract
    spread_model: SpreadModel = field(default_factory=SpreadModel.default_xauusd)
    slippage: float = 0.05  # adverse slip on market/stop orders, price units
    swap_long_per_lot: float = 0.0  # USD per lot per night held
    swap_short_per_lot: float = 0.0

    # ---------------------------------------------------------------- entries
    def entry_fill(self, side: Side, bar_open_bid: float, spread: float) -> float:
        """Fill price for a market entry at the open of the execution bar."""
        if side is Side.LONG:
            return bar_open_bid + spread + self.slippage
        return bar_open_bid - self.slippage

    # ------------------------------------------------------ exit price frames
    @staticmethod
    def exit_frame(
        side: Side, o: float, h: float, l: float, c: float, spread: float
    ) -> tuple[float, float, float, float]:
        """Translate bid OHLC into the price space the exit actually fills in.

        A long exits by selling at the bid (the raw series). A short exits by
        buying at the ask, so every level is lifted by the spread.
        """
        if side is Side.LONG:
            return o, h, l, c
        return o + spread, h + spread, l + spread, c + spread

    # ----------------------------------------------------------------- exits
    def check_exit(
        self,
        pos: Position,
        o: float,
        h: float,
        l: float,
        c: float,
        spread: float,
    ) -> tuple[float, ExitReason] | None:
        """Resolve stop/target for one bar.

        Returns ``(fill_price, reason)`` or ``None`` if the position survives.
        """
        eo, eh, el, _ = self.exit_frame(pos.side, o, h, l, c, spread)
        sl, tp = pos.stop_loss, pos.take_profit

        if pos.side is Side.LONG:
            # Gap through a level at the open: the open is the fill.
            if eo <= sl:
                return eo, ExitReason.STOP_LOSS
            if tp is not None and eo >= tp:
                return eo, ExitReason.TAKE_PROFIT
            hit_sl = el <= sl
            hit_tp = tp is not None and eh >= tp
            if hit_sl:  # takes precedence over tp on an ambiguous bar
                return sl - self.slippage, ExitReason.STOP_LOSS
            if hit_tp:
                return tp, ExitReason.TAKE_PROFIT
            return None

        if eo >= sl:
            return eo, ExitReason.STOP_LOSS
        if tp is not None and eo <= tp:
            return eo, ExitReason.TAKE_PROFIT
        hit_sl = eh >= sl
        hit_tp = tp is not None and el <= tp
        if hit_sl:
            return sl + self.slippage, ExitReason.STOP_LOSS
        if hit_tp:
            return tp, ExitReason.TAKE_PROFIT
        return None

    def market_exit_fill(self, side: Side, price_bid: float, spread: float) -> float:
        """Discretionary flat (session close, signal flip) at the current bar."""
        if side is Side.LONG:
            return price_bid - self.slippage
        return price_bid + spread + self.slippage

    # ------------------------------------------------------------------ p&l
    def gross_pnl(self, pos: Position, exit_price: float) -> float:
        move = (exit_price - pos.entry_price) * pos.side.sign
        return move * self.contract.value_per_price_unit(pos.lots)

    def commission(self, lots: float) -> float:
        return self.contract.commission(lots)

    def swap_for_night(self, side: Side, lots: float) -> float:
        per_lot = (
            self.swap_long_per_lot if side is Side.LONG else self.swap_short_per_lot
        )
        return per_lot * lots

    def update_excursions(self, pos: Position, h: float, l: float, spread: float) -> None:
        """Track MAE/MFE in price units, in the position's own exit price space."""
        _, eh, el, _ = self.exit_frame(pos.side, h, h, l, l, spread)
        if pos.side is Side.LONG:
            pos.mae = max(pos.mae, pos.entry_price - el)
            pos.mfe = max(pos.mfe, eh - pos.entry_price)
        else:
            pos.mae = max(pos.mae, eh - pos.entry_price)
            pos.mfe = max(pos.mfe, pos.entry_price - el)
