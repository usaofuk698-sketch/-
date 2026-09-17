"""Instrument specification for spot gold (XAUUSD).

All sizing and P&L math funnels through this module so that a change of broker
or instrument is a config change, never a code change.
"""
from __future__ import annotations

from dataclasses import dataclass
from math import floor


@dataclass(frozen=True)
class Contract:
    """Static description of a tradable instrument.

    Attributes:
        symbol: Broker symbol.
        contract_size: Units of the underlying in one standard lot. For XAUUSD
            this is 100 troy ounces, so a $1.00 move on 1.00 lot is $100.
        tick_size: Smallest price increment the broker quotes.
        min_lot / max_lot / lot_step: Broker volume constraints.
        commission_per_lot_per_side: USD charged per lot on entry and again on
            exit. Many gold accounts are commission-free with a wider spread;
            set this to 0.0 there.
    """

    symbol: str = "XAUUSD"
    contract_size: float = 100.0
    tick_size: float = 0.01
    min_lot: float = 0.01
    max_lot: float = 100.0
    lot_step: float = 0.01
    commission_per_lot_per_side: float = 0.0

    def value_per_price_unit(self, lots: float) -> float:
        """USD P&L for a 1.00 move in price while holding `lots`."""
        return lots * self.contract_size

    def round_lots(self, lots: float) -> float:
        """Clamp to broker limits and floor to the nearest valid lot step.

        Floors rather than rounds: rounding up would silently take more risk
        than the risk model authorised.
        """
        if lots < self.min_lot:
            return 0.0
        lots = min(lots, self.max_lot)
        steps = floor(round(lots / self.lot_step, 9))
        return round(steps * self.lot_step, 8)

    def round_price(self, price: float) -> float:
        ticks = round(price / self.tick_size)
        return round(ticks * self.tick_size, 8)

    def commission(self, lots: float) -> float:
        """Round-turn commission (both sides) for `lots`."""
        return 2.0 * lots * self.commission_per_lot_per_side


XAUUSD = Contract()
