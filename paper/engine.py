"""Paper-trading execution engine and position lifecycle manager."""

from __future__ import annotations

import datetime
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from market.candles import Candle
from paper.sizing import PositionSizer, PositionSizingResult, SizingConfig

logger = logging.getLogger("paper.engine")


@dataclass
class PaperTrade:
    trade_id: str
    strategy_version: str
    symbol: str
    market_type: str
    side: str  # "LONG"
    detected_at: str
    entered_at: str
    entry_price: float
    stop_price: float
    target_price: float
    quantity: float
    notional: float
    leverage_simulated: int
    risk_amount: float
    status: str = "OPEN"  # "OPEN" or "CLOSED"
    exit_price: Optional[float] = None
    exit_at: Optional[str] = None
    exit_reason: Optional[str] = None
    gross_pnl: Optional[float] = None
    fees_estimate: Optional[float] = None
    slippage_estimate: Optional[float] = None
    funding_estimate: Optional[float] = None
    net_pnl: Optional[float] = None
    net_pnl_pct_equity: Optional[float] = None
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    reached_10pct: bool = False
    reached_20pct: bool = False
    reached_50pct: bool = False

    # Internal tracking
    entry_timestamp_ms: int = 0
    highest_price_seen: float = 0.0
    lowest_price_seen: float = 0.0
    trailing_stop_active: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Convert trade record to dict matching Section 8.1 schema."""
        return {
            "trade_id": self.trade_id,
            "strategy_version": self.strategy_version,
            "symbol": self.symbol,
            "market_type": self.market_type,
            "side": self.side,
            "detected_at": self.detected_at,
            "entered_at": self.entered_at,
            "entry_price": round(self.entry_price, 8),
            "stop_price": round(self.stop_price, 8),
            "target_price": round(self.target_price, 8),
            "quantity": self.quantity,
            "notional": round(self.notional, 2),
            "leverage_simulated": self.leverage_simulated,
            "risk_amount": round(self.risk_amount, 2),
            "status": self.status,
            "exit_price": round(self.exit_price, 8) if self.exit_price is not None else None,
            "exit_at": self.exit_at,
            "exit_reason": self.exit_reason,
            "gross_pnl": round(self.gross_pnl, 2) if self.gross_pnl is not None else None,
            "fees_estimate": round(self.fees_estimate, 4) if self.fees_estimate is not None else None,
            "slippage_estimate": round(self.slippage_estimate, 4) if self.slippage_estimate is not None else None,
            "funding_estimate": self.funding_estimate,
            "net_pnl": round(self.net_pnl, 2) if self.net_pnl is not None else None,
            "net_pnl_pct_equity": round(self.net_pnl_pct_equity, 4) if self.net_pnl_pct_equity is not None else None,
            "mfe_pct": round(self.mfe_pct, 2),
            "mae_pct": round(self.mae_pct, 2),
            "reached_10pct": self.reached_10pct,
            "reached_20pct": self.reached_20pct,
            "reached_50pct": self.reached_50pct,
            "entry_timestamp_ms": self.entry_timestamp_ms,
            "highest_price_seen": round(self.highest_price_seen, 8),
            "lowest_price_seen": round(self.lowest_price_seen, 8),
            "trailing_stop_active": self.trailing_stop_active,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> PaperTrade:
        entry_ts = int(data.get("entry_timestamp_ms") or 0)
        if entry_ts <= 0 and data.get("entered_at"):
            try:
                dt = datetime.datetime.fromisoformat(data["entered_at"].replace("Z", "+00:00"))
                entry_ts = int(dt.timestamp() * 1000)
            except Exception:
                entry_ts = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)

        entry_px = float(data["entry_price"])
        highest_seen = float(data.get("highest_price_seen") or entry_px)
        lowest_seen = float(data.get("lowest_price_seen") or entry_px)
        trailing_active = bool(data.get("trailing_stop_active", False))

        trade = cls(
            trade_id=data["trade_id"],
            strategy_version=data.get("strategy_version", "momentum_v1"),
            symbol=data["symbol"],
            market_type=data.get("market_type", "perpetual"),
            side=data.get("side", "LONG"),
            detected_at=data.get("detected_at", ""),
            entered_at=data.get("entered_at", ""),
            entry_price=entry_px,
            stop_price=float(data["stop_price"]),
            target_price=float(data["target_price"]),
            quantity=float(data["quantity"]),
            notional=float(data["notional"]),
            leverage_simulated=int(data.get("leverage_simulated", 1)),
            risk_amount=float(data.get("risk_amount", 0.0)),
            status=data.get("status", "OPEN"),
            exit_price=float(data["exit_price"]) if data.get("exit_price") is not None else None,
            exit_at=data.get("exit_at"),
            exit_reason=data.get("exit_reason"),
            gross_pnl=float(data["gross_pnl"]) if data.get("gross_pnl") is not None else None,
            fees_estimate=float(data["fees_estimate"]) if data.get("fees_estimate") is not None else None,
            slippage_estimate=float(data["slippage_estimate"]) if data.get("slippage_estimate") is not None else None,
            funding_estimate=data.get("funding_estimate"),
            net_pnl=float(data["net_pnl"]) if data.get("net_pnl") is not None else None,
            net_pnl_pct_equity=float(data["net_pnl_pct_equity"]) if data.get("net_pnl_pct_equity") is not None else None,
            mfe_pct=float(data.get("mfe_pct", 0.0)),
            mae_pct=float(data.get("mae_pct", 0.0)),
            reached_10pct=bool(data.get("reached_10pct", False)),
            reached_20pct=bool(data.get("reached_20pct", False)),
            reached_50pct=bool(data.get("reached_50pct", False)),
            entry_timestamp_ms=entry_ts,
            highest_price_seen=highest_seen,
            lowest_price_seen=lowest_seen,
            trailing_stop_active=trailing_active,
        )
        return trade


class PaperEngine:
    """Simulates entries, stops, 2R targets, trailing stops, and time exits."""

    def __init__(
        self,
        sizing_config: SizingConfig,
        target_r_multiple: float = 2.0,
        max_holding_hours: int = 24,
        trailing_stop_enabled: bool = True,
        trailing_stop_activation_r: float = 1.0,
    ) -> None:
        self.sizing_config = sizing_config
        self.target_r_multiple = target_r_multiple
        self.max_holding_hours = max_holding_hours
        self.trailing_stop_enabled = trailing_stop_enabled
        self.trailing_stop_activation_r = trailing_stop_activation_r

        self.current_equity = sizing_config.starting_equity
        self.start_of_day_equity = sizing_config.starting_equity
        self.daily_loss_accumulated = 0.0
        self.current_date_utc = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

        self.sizer = PositionSizer(sizing_config)
        self.open_trades: Dict[str, PaperTrade] = {}  # trade_id -> PaperTrade
        self.closed_trades: List[PaperTrade] = []
        self._trade_counter: int = 0

    def next_trade_id(self) -> str:
        self._trade_counter += 1
        return f"PT-{self._trade_counter:06d}"

    def set_trade_counter(self, start: int) -> None:
        self._trade_counter = max(self._trade_counter, start)

    def check_day_reset(self, date_utc: str) -> None:
        """Reset daily loss limit and update start-of-day equity at 00:00 UTC."""
        if not self.current_date_utc:
            self.current_date_utc = date_utc
            return
        if date_utc > self.current_date_utc:
            self.current_date_utc = date_utc
            self.start_of_day_equity = self.current_equity
            self.daily_loss_accumulated = 0.0
            logger.info("New trading day UTC %s. SOD Equity: %.2f", date_utc, self.start_of_day_equity)

    def open_trade(
        self,
        symbol: str,
        market_type: str,
        detected_at: str,
        entered_at: str,
        entry_price: float,
        stop_price: float,
        entry_timestamp_ms: int,
        strategy_version: str = "ema_pullback_v2",
        side: str = "LONG",
        market_min_qty: float = 0.0001,
        qty_step: float = 0.0001,
    ) -> Tuple[Optional[PaperTrade], Optional[str]]:
        """Attempt to open a paper trade enforcing all risk guardrails."""
        now_date = datetime.datetime.fromtimestamp(
            entry_timestamp_ms / 1000.0, tz=datetime.timezone.utc
        ).strftime("%Y-%m-%d")
        self.check_day_reset(now_date)

        is_short = (side.upper() == "SHORT")

        # Calculate position sizing and guardrails
        sizing = self.sizer.calculate_sizing(
            equity=self.current_equity,
            entry_price=entry_price,
            stop_price=stop_price,
            current_open_trades_count=len(self.open_trades),
            daily_loss_accumulated=self.daily_loss_accumulated,
            start_of_day_equity=self.start_of_day_equity,
            side=side,
            market_min_qty=market_min_qty,
            qty_step=qty_step,
        )

        if not sizing.valid:
            return None, sizing.reason

        # Initial target: target_r_multiple * risk
        risk_per_unit = (stop_price - entry_price) if is_short else (entry_price - stop_price)
        if is_short:
            target_price = entry_price - (self.target_r_multiple * risk_per_unit)
        else:
            target_price = entry_price + (self.target_r_multiple * risk_per_unit)

        trade = PaperTrade(
            trade_id=self.next_trade_id(),
            strategy_version=strategy_version,
            symbol=symbol,
            market_type=market_type,
            side=side.upper(),
            detected_at=detected_at,
            entered_at=entered_at,
            entry_price=entry_price,
            stop_price=stop_price,
            target_price=target_price,
            quantity=sizing.quantity,
            notional=sizing.notional,
            leverage_simulated=1,
            risk_amount=sizing.risk_amount,
            status="OPEN",
            fees_estimate=sizing.estimated_fees,
            slippage_estimate=sizing.estimated_slippage,
            entry_timestamp_ms=entry_timestamp_ms,
            highest_price_seen=entry_price,
            lowest_price_seen=entry_price,
        )

        self.open_trades[trade.trade_id] = trade
        logger.info(
            "Opened paper trade %s on %s at %.4f (Stop: %.4f, Target: %.4f, Size: %.4f, Notional: %.2f)",
            trade.trade_id,
            trade.symbol,
            entry_price,
            stop_price,
            target_price,
            trade.quantity,
            trade.notional,
        )
        return trade, None

    def update_and_evaluate_exits(
        self,
        candle: Candle,
        symbol: str,
    ) -> List[PaperTrade]:
        """Update open trades for symbol with new completed candle and evaluate exits.

        Exit model (Section 6.3):
        - Stop below swing low (frozen at entry)
        - Target: 2R
        - If stop and target are both inside the same OHLC candle, ASSUME STOP FIRST!
        - Trailing stop: moves only in favorable direction after trigger (e.g. 1R reached)
        - Time stop: close after max holding hours
        """
        closed_in_candle: List[PaperTrade] = []
        c_low = candle.low
        c_high = candle.high
        c_close = candle.close
        c_time_iso = candle.iso_time
        c_ts = candle.timestamp_ms

        now_date = datetime.datetime.fromtimestamp(c_ts / 1000.0, tz=datetime.timezone.utc).strftime("%Y-%m-%d")
        self.check_day_reset(now_date)

        for trade_id, trade in list(self.open_trades.items()):
            if trade.symbol != symbol:
                continue

            is_short = (trade.side == "SHORT")

            # Update MFE / MAE and reached percentages
            if is_short:
                mfe = ((trade.entry_price - trade.lowest_price_seen) / trade.entry_price) * 100.0
                mae = -((trade.highest_price_seen - trade.entry_price) / trade.entry_price) * 100.0
            else:
                mfe = ((trade.highest_price_seen - trade.entry_price) / trade.entry_price) * 100.0
                mae = ((trade.lowest_price_seen - trade.entry_price) / trade.entry_price) * 100.0

            trade.mfe_pct = max(trade.mfe_pct, mfe)
            trade.mae_pct = min(trade.mae_pct, mae)

            if trade.mfe_pct >= 10.0:
                trade.reached_10pct = True
            if trade.mfe_pct >= 20.0:
                trade.reached_20pct = True
            if trade.mfe_pct >= 50.0:
                trade.reached_50pct = True

            # Check conservative same-candle hit rule (Section 6.3)
            # Check stop hit using current stop_price BEFORE any same-candle trailing adjustment
            current_stop = trade.stop_price
            if is_short:
                hit_stop = c_high >= current_stop
                hit_target = c_low <= trade.target_price
            else:
                hit_stop = c_low <= current_stop
                hit_target = c_high >= trade.target_price

            exit_price: Optional[float] = None
            exit_reason: Optional[str] = None

            if hit_stop and hit_target:
                # Conservative rule: Assume stop first at original stop price!
                exit_price = current_stop
                exit_reason = "STOP"
                logger.warning(
                    "Conservative same-candle collision on %s: assuming STOP first at %.4f",
                    trade.trade_id,
                    exit_price,
                )
            elif hit_stop:
                exit_price = current_stop
                exit_reason = "TRAILING_STOP" if trade.trailing_stop_active else "STOP"
            elif hit_target:
                exit_price = trade.target_price
                exit_reason = "TARGET"
            else:
                # If neither stop nor target was hit, evaluate trailing stop move
                if is_short:
                    risk_per_unit = current_stop - trade.entry_price
                    if self.trailing_stop_enabled and not trade.trailing_stop_active and risk_per_unit > 0:
                        if (trade.entry_price - trade.lowest_price_seen) >= (self.trailing_stop_activation_r * risk_per_unit):
                            trade.trailing_stop_active = True
                            new_stop = trade.entry_price
                            if new_stop < trade.stop_price:
                                trade.stop_price = new_stop
                                logger.info("Trade %s (SHORT): Trailing stop moved to entry %.4f", trade.trade_id, new_stop)
                else:
                    risk_per_unit = trade.entry_price - current_stop
                    if self.trailing_stop_enabled and not trade.trailing_stop_active and risk_per_unit > 0:
                        if (trade.highest_price_seen - trade.entry_price) >= (self.trailing_stop_activation_r * risk_per_unit):
                            trade.trailing_stop_active = True
                            new_stop = trade.entry_price
                            if new_stop > trade.stop_price:
                                trade.stop_price = new_stop
                                logger.info("Trade %s (LONG): Trailing stop moved to entry %.4f", trade.trade_id, new_stop)

                # Time stop check (Section 6.3)
                holding_hours = (c_ts - trade.entry_timestamp_ms) / (1000.0 * 3600.0)
                if holding_hours >= self.max_holding_hours:
                    exit_price = c_close
                    exit_reason = "TIME_STOP"

            if exit_price is not None and exit_reason is not None:
                self._finalize_exit(trade, exit_price, c_time_iso, exit_reason)
                del self.open_trades[trade.trade_id]
                self.closed_trades.append(trade)
                closed_in_candle.append(trade)

        return closed_in_candle

    def _finalize_exit(
        self,
        trade: PaperTrade,
        exit_price: float,
        exit_time_iso: str,
        reason: str,
    ) -> None:
        """Calculate final P&L, fees, slippage, and update portfolio equity."""
        trade.exit_price = exit_price
        trade.exit_at = exit_time_iso
        trade.exit_reason = reason
        trade.status = "CLOSED"

        # Gross P&L based on trade side
        if trade.side == "SHORT":
            gross_pnl = trade.quantity * (trade.entry_price - exit_price)
        else:
            gross_pnl = trade.quantity * (exit_price - trade.entry_price)
        trade.gross_pnl = round(gross_pnl, 2)

        # Total fees and slippage across both entry & exit
        exit_notional = trade.quantity * exit_price
        total_notional = trade.notional + exit_notional
        fees = total_notional * self.sizing_config.fee_rate_per_side
        slippage = total_notional * (self.sizing_config.slippage_bps_per_side / 10000.0)

        trade.fees_estimate = round(fees, 4)
        trade.slippage_estimate = round(slippage, 4)
        trade.funding_estimate = None

        net_pnl = gross_pnl - (fees + slippage)
        trade.net_pnl = round(net_pnl, 2)

        # Net P&L % of equity at entry
        if self.current_equity > 0:
            trade.net_pnl_pct_equity = round((net_pnl / self.current_equity) * 100.0, 4)
        else:
            trade.net_pnl_pct_equity = 0.0

        # Update equity
        self.current_equity += net_pnl

        # Track daily loss accumulated
        if net_pnl < 0:
            self.daily_loss_accumulated += abs(net_pnl)

        logger.info(
            "Closed trade %s (%s) at %.4f [Reason: %s] Gross PnL: %.2f, Net PnL: %.2f. Ending Equity: %.2f",
            trade.trade_id,
            trade.symbol,
            exit_price,
            reason,
            trade.gross_pnl,
            trade.net_pnl,
            self.current_equity,
        )

    def reload_open_trades(self, trades: List[PaperTrade]) -> None:
        """Restore active trades upon restart without duplicates."""
        for trade in trades:
            if trade.status == "OPEN":
                self.open_trades[trade.trade_id] = trade
                logger.info("Reloaded open trade %s on %s", trade.trade_id, trade.symbol)
            else:
                self.closed_trades.append(trade)
        # Update counter to avoid ID collision
        max_id = 0
        for t in trades:
            try:
                num = int(t.trade_id.replace("PT-", ""))
                max_id = max(max_id, num)
            except ValueError:
                pass
        self.set_trade_counter(max_id)
