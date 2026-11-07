"""Risk-based position sizing and guardrails for paper trading."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("paper.sizing")


@dataclass
class SizingConfig:
    starting_equity: float = 20000.0
    risk_fraction: float = 0.005  # 0.5% per trade risk
    max_open_trades: int = 4  # Up to 4 active trades at a time
    max_daily_loss_fraction: float = 0.02  # 2.0% daily max loss
    max_notional_fraction_equity: float = 1.0  # 1x simulated exposure
    max_trade_fraction_equity: float = 0.25  # Max 25% of capital per single trade
    fee_rate_per_side: float = 0.001  # 0.1%
    slippage_bps_per_side: float = 5.0  # 5 bps = 0.05%


@dataclass
class PositionSizingResult:
    valid: bool
    risk_amount: float = 0.0
    risk_per_unit: float = 0.0
    quantity: float = 0.0
    notional: float = 0.0
    estimated_fees: float = 0.0
    estimated_slippage: float = 0.0
    reason: Optional[str] = None


class PositionSizer:
    """Calculates risk-based position sizes and enforces paper-trading guardrails."""

    def __init__(self, config: SizingConfig) -> None:
        self.config = config

    def calculate_sizing(
        self,
        equity: float,
        entry_price: float,
        stop_price: float,
        current_open_trades_count: int,
        daily_loss_accumulated: float,
        start_of_day_equity: float,
        side: str = "LONG",
        remaining_capital: Optional[float] = None,
        market_min_qty: float = 0.0001,
        qty_step: float = 0.0001,
    ) -> PositionSizingResult:
        """Calculate quantity, notional, and fees according to Section 6.1 & 6.2."""
        # 1. Max concurrent paper trades check
        if current_open_trades_count >= self.config.max_open_trades:
            return PositionSizingResult(
                valid=False,
                reason="MAX_CONCURRENT_TRADES_REACHED",
            )

        # 2. Maximum daily simulated loss check (1% of start-of-day equity)
        max_allowed_daily_loss = start_of_day_equity * self.config.max_daily_loss_fraction
        if daily_loss_accumulated >= max_allowed_daily_loss:
            return PositionSizingResult(
                valid=False,
                reason="MAX_DAILY_LOSS_EXCEEDED",
            )

        # 3. Basic equity and price validation
        if equity <= 0.0:
            return PositionSizingResult(valid=False, reason="INSUFFICIENT_EQUITY")

        if entry_price <= 0.0 or stop_price <= 0.0:
            return PositionSizingResult(valid=False, reason="INVALID_PRICE")

        # 4. Risk per unit depending on trade side
        if side.upper() == "SHORT":
            risk_per_unit = stop_price - entry_price
            if risk_per_unit <= 0.0:
                return PositionSizingResult(valid=False, reason="STOP_BELOW_OR_EQUAL_TO_ENTRY")
        else:
            risk_per_unit = entry_price - stop_price
            if risk_per_unit <= 0.0:
                return PositionSizingResult(valid=False, reason="STOP_ABOVE_OR_EQUAL_TO_ENTRY")

        # Available capital for this trade: maximum of 25% of capital or remaining amount
        rem_cap = remaining_capital if remaining_capital is not None else equity
        if rem_cap <= 0.0:
            return PositionSizingResult(valid=False, reason="NO_AVAILABLE_CAPITAL")

        max_trade_cap = equity * self.config.max_trade_fraction_equity
        max_allowed_notional = min(max_trade_cap, rem_cap)

        # 5. Risk amount
        risk_amount = equity * self.config.risk_fraction
        if risk_amount <= 0.0:
            return PositionSizingResult(valid=False, reason="INVALID_RISK_AMOUNT")

        # 6. Raw quantity
        raw_quantity = risk_amount / risk_per_unit

        # Align with step size
        if qty_step > 0:
            steps = round(raw_quantity / qty_step)
            quantity = steps * qty_step
        else:
            quantity = raw_quantity

        if quantity < market_min_qty:
            quantity = market_min_qty

        notional = quantity * entry_price

        # 7. Cap notional to max 25% of capital or remaining amount
        if notional > max_allowed_notional:
            capped_quantity = max_allowed_notional / entry_price
            if qty_step > 0:
                capped_quantity = (round(capped_quantity / qty_step)) * qty_step

            if capped_quantity < market_min_qty:
                return PositionSizingResult(valid=False, reason="NOTIONAL_EXCEEDS_CAP")

            quantity = capped_quantity
            notional = quantity * entry_price
            risk_amount = quantity * risk_per_unit
        elif notional < max_allowed_notional:
            # Allocate up to max 25% of capital if risk stays within safe 1.5% bounds
            notional_at_max_risk = (equity * 0.015) / (risk_per_unit / entry_price)
            allocated_notional = min(max_allowed_notional, notional_at_max_risk)
            scaled_qty = allocated_notional / entry_price
            if qty_step > 0:
                scaled_qty = (round(scaled_qty / qty_step)) * qty_step
            if scaled_qty >= market_min_qty:
                quantity = scaled_qty
                notional = quantity * entry_price
                risk_amount = quantity * risk_per_unit

        # 8. Fees & slippage estimation (both entry and exit sides)
        slippage_fraction = (self.config.slippage_bps_per_side / 10000.0) * 2.0
        fee_fraction = self.config.fee_rate_per_side * 2.0

        estimated_fees = notional * fee_fraction
        estimated_slippage = notional * slippage_fraction

        return PositionSizingResult(
            valid=True,
            risk_amount=round(risk_amount, 2),
            risk_per_unit=round(risk_per_unit, 8),
            quantity=round(quantity, 6),
            notional=round(notional, 2),
            estimated_fees=round(estimated_fees, 4),
            estimated_slippage=round(estimated_slippage, 4),
            reason=None,
        )

