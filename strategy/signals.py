"""Signal evaluator: continuation confirmation and anti-chase rules."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from market.candles import Candle
from market.indicators import (
    calculate_atr,
    calculate_ema,
    calculate_relative_volume,
    calculate_rsi,
    calculate_vwap,
    find_recent_range_high,
    find_swing_low,
)
from strategy.scanner import ScannerConfig

logger = logging.getLogger("strategy.signals")


@dataclass
class SignalResult:
    confirmed: bool
    signal_type: str
    entry_price: float
    stop_price: float
    reason: Optional[str] = None
    metrics: Dict[str, Any] = None


class SignalEvaluator:
    """Evaluates continuation confirmation rules and anti-chase protections."""

    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        # Tracks per-symbol cooldown: symbol -> last_failed_candle_timestamp_ms
        self._cooldowns: Dict[str, int] = {}

    def set_cooldown(self, symbol: str, timestamp_ms: int) -> None:
        self._cooldowns[symbol] = timestamp_ms

    def is_in_cooldown(self, symbol: str, current_timestamp_ms: int) -> bool:
        last_failed_ts = self._cooldowns.get(symbol)
        if not last_failed_ts:
            return False
        # Cooldown duration = cooldown_candles * 15 minutes in ms
        cooldown_ms = self.config.cooldown_candles_after_failed * 15 * 60 * 1000
        return (current_timestamp_ms - last_failed_ts) < cooldown_ms

    def evaluate_long_signal(
        self,
        symbol: str,
        candles: List[Candle],
        derivatives_context: Optional[Dict[str, Any]] = None,
        spread_pct: float = 0.05,
    ) -> SignalResult:
        """Evaluate Section 5.3 Continuation confirmation and Section 5.4 Anti-chase rules.

        Conditions:
        - 15m close > 15m VWAP
        - 15m close > 15m EMA(50)
        - RSI(14) > 55
        - relative volume >= 2.0
        - breakout of a recent range high
        - not excessively extended from VWAP/EMA50
        - swing low stop distance is practical
        - not in per-symbol cooldown
        """
        metrics: Dict[str, Any] = {}
        derivatives_context = derivatives_context or {}

        if len(candles) < max(55, self.config.lookback_range_candles + 1):
            return SignalResult(
                confirmed=False,
                signal_type="momentum_v1",
                entry_price=0.0,
                stop_price=0.0,
                reason="INSUFFICIENT_CANDLE_HISTORY",
                metrics=metrics,
            )

        latest = candles[-1]
        close = latest.close
        prices = [c.close for c in candles]

        # 1. Anti-chase: Cooldown check
        if self.is_in_cooldown(symbol, latest.timestamp_ms):
            return SignalResult(
                confirmed=False,
                signal_type="momentum_v1",
                entry_price=close,
                stop_price=0.0,
                reason="COOLDOWN_ACTIVE",
                metrics=metrics,
            )

        # 2. Indicators computation
        vwap = calculate_vwap(candles, max_candles=96)
        ema50 = calculate_ema(prices, period=50)
        rsi = calculate_rsi(prices, period=14)
        rvol = calculate_relative_volume(candles, period=20)
        atr = calculate_atr(candles, period=14)
        range_high = find_recent_range_high(candles, lookback=self.config.lookback_range_candles)
        swing_low = find_swing_low(candles, lookback=10)

        metrics["close"] = close
        metrics["vwap"] = vwap
        metrics["ema50"] = ema50
        metrics["rsi"] = rsi
        metrics["rvol"] = rvol
        metrics["atr"] = atr
        metrics["range_high"] = range_high
        metrics["swing_low"] = swing_low

        if vwap is not None and vwap > 0:
            vwap_dist = ((close - vwap) / vwap) * 100.0
            metrics["vwap_distance_pct"] = round(vwap_dist, 2)
        else:
            metrics["vwap_distance_pct"] = 0.0

        if ema50 is not None and ema50 > 0:
            ema_dist = ((close - ema50) / ema50) * 100.0
            metrics["ema_distance_pct"] = round(ema_dist, 2)
        else:
            metrics["ema_distance_pct"] = 0.0

        # 3. Anti-chase: Reject signals excessively extended from EMA50
        if metrics["ema_distance_pct"] > self.config.max_distance_from_ema50_pct:
            return SignalResult(
                confirmed=False,
                signal_type="momentum_v1",
                entry_price=close,
                stop_price=0.0,
                reason="EXTENDED_FROM_EMA",
                metrics=metrics,
            )

        # 4. Continuation Confirmation: 15m close > 15m VWAP
        if self.config.require_above_vwap and (vwap is None or close <= vwap):
            return SignalResult(
                confirmed=False,
                signal_type="momentum_v1",
                entry_price=close,
                stop_price=0.0,
                reason="BELOW_VWAP",
                metrics=metrics,
            )

        # 5. Continuation Confirmation: 15m close > 15m EMA(50)
        if self.config.require_above_ema50 and (ema50 is None or close <= ema50):
            return SignalResult(
                confirmed=False,
                signal_type="momentum_v1",
                entry_price=close,
                stop_price=0.0,
                reason="BELOW_EMA50",
                metrics=metrics,
            )

        # 6. Continuation Confirmation: RSI(14) > 55
        if rsi is None or rsi < self.config.rsi_min:
            return SignalResult(
                confirmed=False,
                signal_type="momentum_v1",
                entry_price=close,
                stop_price=0.0,
                reason="RSI_BELOW_MIN",
                metrics=metrics,
            )

        # 7. Continuation Confirmation: relative volume >= 2.0
        if rvol is None or rvol < self.config.min_relative_volume:
            return SignalResult(
                confirmed=False,
                signal_type="momentum_v1",
                entry_price=close,
                stop_price=0.0,
                reason="RVOL_BELOW_MIN",
                metrics=metrics,
            )

        # 8. Continuation Confirmation: breakout of recent range high
        # The current candle close or high should be >= range_high
        if range_high is not None and close < range_high and latest.high < range_high:
            return SignalResult(
                confirmed=False,
                signal_type="momentum_v1",
                entry_price=close,
                stop_price=0.0,
                reason="NO_RANGE_BREAKOUT",
                metrics=metrics,
            )

        # 9. Stop calculation (Section 6.3):
        # "Initial stop: below retest swing low, with an ATR-based minimum distance. Freeze stop at entry."
        atr_val = atr if (atr is not None and atr > 0) else (close * 0.01)
        base_low = swing_low if (swing_low is not None and swing_low < close) else (close - atr_val)

        # ATR buffer below swing low (e.g. 0.5 * ATR buffer)
        stop_price = min(base_low - (0.5 * atr_val), close - (1.0 * atr_val))
        stop_distance = close - stop_price
        stop_dist_pct = (stop_distance / close) * 100.0

        metrics["stop_price"] = round(stop_price, 8)
        metrics["stop_distance_pct"] = round(stop_dist_pct, 2)

        # 10. Impractical stop distance check (too tight or too wide)
        if stop_price >= close or stop_dist_pct < 0.2:
            return SignalResult(
                confirmed=False,
                signal_type="momentum_v1",
                entry_price=close,
                stop_price=stop_price,
                reason="STOP_DISTANCE_TOO_TIGHT",
                metrics=metrics,
            )

        if stop_dist_pct > 15.0:
            return SignalResult(
                confirmed=False,
                signal_type="momentum_v1",
                entry_price=close,
                stop_price=stop_price,
                reason="STOP_DISTANCE_TOO_WIDE",
                metrics=metrics,
            )

        # 11. Derivatives context check (Section 5.3):
        signal_type = "momentum_v1"
        if self.config.use_open_interest_filter:
            oi_change = derivatives_context.get("open_interest_change_pct")
            if oi_change is None:
                return SignalResult(
                    confirmed=False,
                    signal_type="momentum_v1_oi_filtered",
                    entry_price=close,
                    stop_price=stop_price,
                    reason="OI_DATA_UNAVAILABLE",
                    metrics=metrics,
                )
            if oi_change <= 0:
                return SignalResult(
                    confirmed=False,
                    signal_type="momentum_v1_oi_filtered",
                    entry_price=close,
                    stop_price=stop_price,
                    reason="OI_CHANGE_NOT_POSITIVE",
                    metrics=metrics,
                )
            signal_type = "momentum_v1_oi_confirmed"
        else:
            signal_type = (
                "momentum_v1"
                if derivatives_context.get("available")
                else "momentum_v1_derivatives_unavailable"
            )

        # All continuation and anti-chase checks passed!
        return SignalResult(
            confirmed=True,
            signal_type=signal_type,
            entry_price=close,
            stop_price=round(stop_price, 8),
            reason=None,
            metrics=metrics,
        )

