"""Signal evaluator: famous Trend-Pullback and Momentum Breakout strategy (EMA 20/50 + RSI Confluence)."""

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
    find_recent_range_low,
    find_swing_high,
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
    side: str = "LONG"  # "LONG" or "SHORT"
    reason: Optional[str] = None
    metrics: Dict[str, Any] = None


class SignalEvaluator:
    """High-probability Trend-Pullback & Momentum Breakout Strategy (EMA 20/50 + RSI)."""

    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        self._cooldowns: Dict[str, int] = {}

    def set_cooldown(self, symbol: str, timestamp_ms: int) -> None:
        self._cooldowns[symbol] = timestamp_ms

    def is_in_cooldown(self, symbol: str, current_timestamp_ms: int) -> bool:
        last_failed_ts = self._cooldowns.get(symbol)
        if not last_failed_ts:
            return False
        cooldown_ms = self.config.cooldown_candles_after_failed * 15 * 60 * 1000
        return (current_timestamp_ms - last_failed_ts) < cooldown_ms

    def evaluate_signal(
        self,
        symbol: str,
        candles: List[Candle],
        derivatives_context: Optional[Dict[str, Any]] = None,
        spread_pct: float = 0.05,
        allow_shorts: bool = True,
    ) -> SignalResult:
        """Evaluate high-probability Trend-Pullback & Breakout opportunities.

        Supports both LONG and SHORT setups with clean confluence.
        """
        metrics: Dict[str, Any] = {}
        derivatives_context = derivatives_context or {}

        if len(candles) < 55:
            return SignalResult(
                confirmed=False,
                signal_type="trend_pullback_v2",
                entry_price=0.0,
                stop_price=0.0,
                side="LONG",
                reason="INSUFFICIENT_CANDLE_HISTORY",
                metrics=metrics,
            )

        latest = candles[-1]
        prev = candles[-2]
        close = latest.close
        prices = [c.close for c in candles]

        # 1. Anti-chase: Cooldown check
        if self.is_in_cooldown(symbol, latest.timestamp_ms):
            return SignalResult(
                confirmed=False,
                signal_type="trend_pullback_v2",
                entry_price=close,
                stop_price=0.0,
                side="LONG",
                reason="COOLDOWN_ACTIVE",
                metrics=metrics,
            )

        # 2. Indicators computation
        ema20 = calculate_ema(prices, period=20)
        ema50 = calculate_ema(prices, period=50)
        rsi = calculate_rsi(prices, period=14)
        vwap = calculate_vwap(candles, max_candles=96)
        atr = calculate_atr(candles, period=14) or (close * 0.01)
        rvol = calculate_relative_volume(candles, period=20) or 1.0
        range_high = find_recent_range_high(candles, lookback=10)
        range_low = find_recent_range_low(candles, lookback=10)
        swing_low = find_swing_low(candles, lookback=8) or (close - 1.5 * atr)
        swing_high = find_swing_high(candles, lookback=8) or (close + 1.5 * atr)

        metrics["close"] = close
        metrics["ema20"] = ema20
        metrics["ema50"] = ema50
        metrics["rsi"] = rsi
        metrics["vwap"] = vwap
        metrics["atr"] = atr
        metrics["rvol"] = rvol

        if ema50 is not None and ema50 > 0:
            ema_dist = abs((close - ema50) / ema50) * 100.0
            metrics["ema_distance_pct"] = round(ema_dist, 2)
            # Anti-chase: reject if overextended from EMA50
            if ema_dist > self.config.max_distance_from_ema50_pct:
                return SignalResult(
                    confirmed=False,
                    signal_type="trend_pullback_v2",
                    entry_price=close,
                    stop_price=0.0,
                    side="LONG",
                    reason="EXTENDED_FROM_EMA",
                    metrics=metrics,
                )

        if vwap is not None and vwap > 0:
            metrics["vwap_distance_pct"] = round(((close - vwap) / vwap) * 100.0, 2)

        # ==========================================
        # SETUP 1: LONG (BULLISH TREND PULLBACK / BREAKOUT)
        # ==========================================
        is_bullish_regime = (
            ema50 is not None
            and close >= ema50 * 0.99
            and (ema20 is None or ema20 >= ema50 * 0.995 or (vwap and close >= vwap))
        )

        if is_bullish_regime and rsi is not None:
            # Check A: Pullback Bounce to EMA20 / VWAP
            recent_lows = [c.low for c in candles[-4:]]
            touched_support = (ema20 is not None and min(recent_lows) <= ema20 * 1.015) or (
                vwap is not None and min(recent_lows) <= vwap * 1.015
            )
            bullish_candle = latest.close > latest.open and latest.close >= prev.close
            rsi_pullback_zone = 42.0 <= rsi <= 68.0

            if touched_support and bullish_candle and rsi_pullback_zone and rvol >= 0.7:
                stop_price = min(close - (1.2 * atr), swing_low - (0.2 * atr))
                stop_dist_pct = ((close - stop_price) / close) * 100.0
                if 0.3 <= stop_dist_pct <= 7.0:
                    return SignalResult(
                        confirmed=True,
                        signal_type="trend_pullback_v2",
                        entry_price=close,
                        stop_price=round(stop_price, 8),
                        side="LONG",
                        metrics=metrics,
                    )

            # Check B: Local Range Breakout
            if range_high is not None and (latest.close >= range_high or latest.high >= range_high):
                if latest.close > latest.open and rsi >= 50.0 and rvol >= 1.1:
                    stop_price = min(close - (1.2 * atr), swing_low - (0.2 * atr))
                    stop_dist_pct = ((close - stop_price) / close) * 100.0
                    if 0.3 <= stop_dist_pct <= 7.0:
                        return SignalResult(
                            confirmed=True,
                            signal_type="momentum_breakout_v2",
                            entry_price=close,
                            stop_price=round(stop_price, 8),
                            side="LONG",
                            metrics=metrics,
                        )

        # ==========================================
        # SETUP 2: SHORT (BEARISH TREND PULLBACK / BREAKDOWN)
        # ==========================================
        is_bearish_regime = (
            allow_shorts
            and ema50 is not None
            and close <= ema50 * 1.01
            and (ema20 is None or ema20 <= ema50 * 1.005 or (vwap and close <= vwap))
        )

        if is_bearish_regime and rsi is not None:
            # Check A: Pullback Rejection near EMA20
            recent_highs = [c.high for c in candles[-4:]]
            touched_resistance = (ema20 is not None and max(recent_highs) >= ema20 * 0.985) or (
                vwap is not None and max(recent_highs) >= vwap * 0.985
            )
            bearish_candle = latest.close < latest.open and latest.close <= prev.close
            rsi_short_zone = 32.0 <= rsi <= 58.0

            if touched_resistance and bearish_candle and rsi_short_zone and rvol >= 0.7:
                stop_price = max(close + (1.2 * atr), swing_high + (0.2 * atr))
                stop_dist_pct = ((stop_price - close) / close) * 100.0
                if 0.3 <= stop_dist_pct <= 7.0:
                    return SignalResult(
                        confirmed=True,
                        signal_type="trend_pullback_v2",
                        entry_price=close,
                        stop_price=round(stop_price, 8),
                        side="SHORT",
                        metrics=metrics,
                    )

            # Check B: Local Range Breakdown
            if range_low is not None and (latest.close <= range_low or latest.low <= range_low):
                if latest.close < latest.open and rsi <= 50.0 and rvol >= 1.1:
                    stop_price = max(close + (1.2 * atr), swing_high + (0.2 * atr))
                    stop_dist_pct = ((stop_price - close) / close) * 100.0
                    if 0.3 <= stop_dist_pct <= 7.0:
                        return SignalResult(
                            confirmed=True,
                            signal_type="momentum_breakout_v2",
                            entry_price=close,
                            stop_price=round(stop_price, 8),
                            side="SHORT",
                            metrics=metrics,
                        )

        return SignalResult(
            confirmed=False,
            signal_type="trend_pullback_v2",
            entry_price=close,
            stop_price=0.0,
            side="LONG",
            reason="NO_CONFLUENCE_TRIGGER",
            metrics=metrics,
        )

    def evaluate_long_signal(
        self,
        symbol: str,
        candles: List[Candle],
        derivatives_context: Optional[Dict[str, Any]] = None,
        spread_pct: float = 0.05,
    ) -> SignalResult:
        """Backwards compatibility adapter."""
        return self.evaluate_signal(
            symbol=symbol,
            candles=candles,
            derivatives_context=derivatives_context,
            spread_pct=spread_pct,
            allow_shorts=True,
        )
