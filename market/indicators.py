"""Technical indicator computations: EMA, RSI, VWAP, ATR, Relative Volume."""

from __future__ import annotations

import math
from typing import List, Optional, Tuple
from market.candles import Candle


def calculate_ema(prices: List[float], period: int = 50) -> Optional[float]:
    """Calculate Exponential Moving Average for the given price series."""
    if len(prices) < period or period <= 0:
        return None

    # Initial SMA seed
    sma = sum(prices[:period]) / period
    k = 2.0 / (period + 1.0)

    ema = sma
    for price in prices[period:]:
        ema = (price * k) + (ema * (1.0 - k))

    return round(ema, 8)


def calculate_rsi(prices: List[float], period: int = 14) -> Optional[float]:
    """Calculate Wilder's Relative Strength Index (RSI)."""
    if len(prices) <= period or period <= 0:
        return None

    deltas = [prices[i] - prices[i - 1] for i in range(1, len(prices))]

    # Initial average gain/loss
    gains = [max(d, 0.0) for d in deltas[:period]]
    losses = [max(-d, 0.0) for d in deltas[:period]]

    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period

    # Wilder's smoothing
    for d in deltas[period:]:
        gain = max(d, 0.0)
        loss = max(-d, 0.0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0.0 else 50.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return round(rsi, 2)


def calculate_vwap(candles: List[Candle], max_candles: int = 96) -> Optional[float]:
    """Calculate Volume-Weighted Average Price (VWAP) over recent candles.

    For 15m candles, 96 candles = 24 hours.
    """
    if not candles:
        return None

    window = candles[-max_candles:]
    total_cum_volume = sum(c.volume for c in window)
    if total_cum_volume <= 0.0:
        return None

    total_tp_vol = sum(c.typical_price * c.volume for c in window)
    vwap = total_tp_vol / total_cum_volume
    return round(vwap, 8)


def calculate_atr(candles: List[Candle], period: int = 14) -> Optional[float]:
    """Calculate Average True Range (ATR) over candles using Wilder's smoothing."""
    if len(candles) <= period or period <= 0:
        return None

    tr_list: List[float] = []
    # First True Range is high - low
    tr_list.append(candles[0].high - candles[0].low)

    for i in range(1, len(candles)):
        curr = candles[i]
        prev_close = candles[i - 1].close
        tr = max(
            curr.high - curr.low,
            abs(curr.high - prev_close),
            abs(curr.low - prev_close),
        )
        tr_list.append(tr)

    # Initial ATR seed
    atr = sum(tr_list[:period]) / period
    for tr in tr_list[period:]:
        atr = (atr * (period - 1) + tr) / period

    return round(atr, 8)


def calculate_relative_volume(candles: List[Candle], period: int = 20) -> Optional[float]:
    """Calculate relative volume: latest candle volume / SMA(volume, period)."""
    if len(candles) < period + 1 or period <= 0:
        return None

    # Current candle is the latest completed candle
    current_vol = candles[-1].volume
    # Prior completed candles
    prior_volumes = [c.volume for c in candles[-period - 1 : -1]]
    avg_vol = sum(prior_volumes) / period

    if avg_vol <= 0.0:
        return None

    rvol = current_vol / avg_vol
    return round(rvol, 2)


def calculate_15m_momentum(candles: List[Candle]) -> Optional[float]:
    """Calculate 15m candle return percentage: (close - open) / open * 100."""
    if not candles:
        return None
    latest = candles[-1]
    if latest.open <= 0.0:
        return None
    mom = ((latest.close - latest.open) / latest.open) * 100.0
    return round(mom, 4)


def find_recent_range_high(candles: List[Candle], lookback: int = 20) -> Optional[float]:
    """Find highest high over past lookback candles, excluding the latest candle."""
    if len(candles) < lookback + 1:
        return None
    prior = candles[-lookback - 1 : -1]
    return max(c.high for c in prior)


def find_recent_range_low(candles: List[Candle], lookback: int = 20) -> Optional[float]:
    """Find lowest low over past lookback candles, excluding the latest candle."""
    if len(candles) < lookback + 1:
        return None
    prior = candles[-lookback - 1 : -1]
    return min(c.low for c in prior)


def find_swing_low(candles: List[Candle], lookback: int = 10) -> Optional[float]:
    """Find recent swing low over the consolidation / retest period."""
    if len(candles) < lookback:
        return min(c.low for c in candles) if candles else None
    return min(c.low for c in candles[-lookback:])


def find_swing_high(candles: List[Candle], lookback: int = 10) -> Optional[float]:
    """Find recent swing high over the consolidation / retest period."""
    if len(candles) < lookback:
        return max(c.high for c in candles) if candles else None
    return max(c.high for c in candles[-lookback:])

