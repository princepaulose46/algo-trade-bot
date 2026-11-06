"""Momentum scanner: initial trigger detection and candidate tracking."""

from __future__ import annotations

import datetime
import logging
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from market.candles import Candle, CandleCache
from market.indicators import (
    calculate_15m_momentum,
    calculate_atr,
    calculate_ema,
    calculate_relative_volume,
    calculate_rsi,
    calculate_vwap,
)

logger = logging.getLogger("strategy.scanner")


@dataclass
class ScannerConfig:
    min_change_24h_pct: float = 0.0
    min_change_15m_pct: float = 0.0
    min_relative_volume: float = 0.7
    rsi_min: float = 42.0
    require_above_vwap: bool = False
    require_above_ema50: bool = False
    use_open_interest_filter: bool = False
    lookback_range_candles: int = 10
    max_distance_from_ema50_pct: float = 8.0
    cooldown_candles_after_failed: int = 6
    use_rolling_24h_calculation: bool = False


@dataclass
class CandidateRecord:
    candidate_id: str
    symbol: str
    detected_at: str
    change_24h_pct: float
    change_15m_pct: float
    relative_volume: float
    spread_pct: float
    vwap_distance_pct: float
    rsi_15m: float
    open_interest_change_pct: Optional[float] = None
    decision: str = "FILTERED"  # "ENTERED", "FILTERED", "EXPIRED", "DATA_INCOMPLETE"
    reason: Optional[str] = None
    market_type: str = "perpetual"
    entry_reference_price: float = 0.0
    detected_timestamp_ms: int = 0
    calculation_method: str = "ticker_field"

    # Evaluation fields tracked over fixed horizons
    max_return_1h_pct: Optional[float] = None
    max_return_4h_pct: Optional[float] = None
    max_return_12h_pct: Optional[float] = None
    max_return_24h_pct: Optional[float] = None
    max_drawdown_24h_pct: Optional[float] = None
    reached_10pct: bool = False
    reached_20pct: bool = False
    reached_50pct: bool = False
    evaluated_until_ms: int = 0

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict matching Section 8.2 format."""
        data = {
            "candidate_id": self.candidate_id,
            "symbol": self.symbol,
            "detected_at": self.detected_at,
            "change_24h_pct": round(self.change_24h_pct, 2) if self.change_24h_pct is not None else None,
            "change_15m_pct": round(self.change_15m_pct, 2) if self.change_15m_pct is not None else None,
            "relative_volume": round(self.relative_volume, 2) if self.relative_volume is not None else None,
            "spread_pct": round(self.spread_pct, 4) if self.spread_pct is not None else None,
            "vwap_distance_pct": round(self.vwap_distance_pct, 2) if self.vwap_distance_pct is not None else None,
            "rsi_15m": round(self.rsi_15m, 1) if self.rsi_15m is not None else None,
            "open_interest_change_pct": self.open_interest_change_pct,
            "decision": self.decision,
            "reason": self.reason,
            "max_return_1h_pct": round(self.max_return_1h_pct, 2) if self.max_return_1h_pct is not None else None,
            "max_return_4h_pct": round(self.max_return_4h_pct, 2) if self.max_return_4h_pct is not None else None,
            "max_return_24h_pct": round(self.max_return_24h_pct, 2) if self.max_return_24h_pct is not None else None,
            "reached_10pct": self.reached_10pct,
            "reached_20pct": self.reached_20pct,
            "reached_50pct": self.reached_50pct,
        }
        return data


class MomentumScanner:
    """Scans markets for momentum trigger conditions and creates candidates."""

    def __init__(self, config: ScannerConfig) -> None:
        self.config = config
        self._counter: int = 0

    def next_candidate_id(self) -> str:
        self._counter += 1
        return f"C-{self._counter:06d}"

    def set_candidate_counter(self, start: int) -> None:
        self._counter = max(self._counter, start)

    def calculate_24h_change(
        self,
        ticker: Dict[str, Any],
        candles: List[Candle],
    ) -> Tuple[float, str]:
        """Calculate 24h percentage change consistently.

        Section 5.2: "Calculate 24h change consistently using a defined rolling
        reference or exchange ticker field, and record which method was used."
        """
        if self.config.use_rolling_24h_calculation and len(candles) >= 96:
            # 96 15m candles = 24 hours
            ref_candle = candles[-96]
            curr_candle = candles[-1]
            if ref_candle.open > 0:
                change = ((curr_candle.close - ref_candle.open) / ref_candle.open) * 100.0
                return round(change, 4), "rolling_24h_candles"

        # Fallback to exchange ticker 24h field
        change = float(ticker.get("change_24_hour") or ticker.get("price_change_percent_24h") or 0.0)
        return round(change, 4), "ticker_field"

    def check_initial_trigger(
        self,
        symbol: str,
        ticker: Dict[str, Any],
        candles: List[Candle],
        spread_pct: float,
        quote_volume: float,
        min_quote_volume: float,
        max_spread_pct: float,
    ) -> Tuple[bool, Dict[str, Any], Optional[str]]:
        """Evaluate initial trigger rules according to Section 5.2:

        - 24h percentage change >= +5.0%
        - AND latest completed 15m candle is valid
        - AND 15m momentum > 0
        - AND relative volume >= 2.0
        - AND spread <= configured maximum
        - AND quote volume >= configured minimum
        """
        metrics: Dict[str, Any] = {}

        # 1. Latest completed 15m candle valid
        if not candles:
            return False, metrics, "NO_CANDLE_DATA"
        latest = candles[-1]
        if not latest.is_valid():
            return False, metrics, "INVALID_LATEST_CANDLE"

        # 2. 24h change check (if min_change_24h_pct configured)
        change_24h, method = self.calculate_24h_change(ticker, candles)
        metrics["change_24h_pct"] = change_24h
        metrics["calculation_method"] = method
        if self.config.min_change_24h_pct > 0.0 and change_24h < self.config.min_change_24h_pct:
            return False, metrics, "CHANGE_24H_BELOW_MIN"

        # 3. 15m momentum check (if min_change_15m_pct configured)
        momentum_15m = calculate_15m_momentum(candles)
        metrics["change_15m_pct"] = momentum_15m or 0.0
        if self.config.min_change_15m_pct > 0.0 and (momentum_15m is None or momentum_15m < self.config.min_change_15m_pct):
            return False, metrics, "MOMENTUM_15M_NOT_POSITIVE"

        # 4. Relative volume check
        rvol = calculate_relative_volume(candles, period=20)
        metrics["relative_volume"] = rvol if rvol is not None else 1.0
        if rvol is not None and rvol < self.config.min_relative_volume:
            return False, metrics, "INSUFFICIENT_RELATIVE_VOLUME"

        # 5. Spread <= max
        metrics["spread_pct"] = spread_pct
        if spread_pct > max_spread_pct:
            return False, metrics, "SPREAD_EXCEEDS_MAX"

        # 6. Quote volume >= min
        metrics["quote_volume"] = quote_volume
        if quote_volume < min_quote_volume:
            return False, metrics, "QUOTE_VOLUME_BELOW_MIN"

        # Passed all Section 5.2 initial trigger conditions!
        return True, metrics, None

    def update_candidate_outcomes(
        self,
        candidate: CandidateRecord,
        future_candles: List[Candle],
    ) -> None:
        """Track maximum upside / downside over fixed horizons (1h, 4h, 12h, 24h).

        Section 7:
        - Track future maximum upside/downside over fixed horizons (1h, 4h, 12h, 24h)
        - whether +10%, +20%, +50% was reached
        - Only information available at detection time affects entry; future fields are evaluation-only.
        """
        if not future_candles or candidate.entry_reference_price <= 0:
            return

        ref_price = candidate.entry_reference_price
        c_1h = future_candles[:4]  # 4 * 15m = 1 hour
        c_4h = future_candles[:16]  # 16 * 15m = 4 hours
        c_12h = future_candles[:48]  # 48 * 15m = 12 hours
        c_24h = future_candles[:96]  # 96 * 15m = 24 hours

        if c_1h:
            max_high_1h = max(c.high for c in c_1h)
            candidate.max_return_1h_pct = ((max_high_1h - ref_price) / ref_price) * 100.0

        if c_4h:
            max_high_4h = max(c.high for c in c_4h)
            candidate.max_return_4h_pct = ((max_high_4h - ref_price) / ref_price) * 100.0

        if c_12h:
            max_high_12h = max(c.high for c in c_12h)
            candidate.max_return_12h_pct = ((max_high_12h - ref_price) / ref_price) * 100.0

        if c_24h:
            max_high_24h = max(c.high for c in c_24h)
            min_low_24h = min(c.low for c in c_24h)
            candidate.max_return_24h_pct = ((max_high_24h - ref_price) / ref_price) * 100.0
            candidate.max_drawdown_24h_pct = ((min_low_24h - ref_price) / ref_price) * 100.0

        # Check threshold milestones across all available future candles
        highest_price = max(c.high for c in future_candles)
        max_pct = ((highest_price - ref_price) / ref_price) * 100.0

        if max_pct >= 10.0:
            candidate.reached_10pct = True
        if max_pct >= 20.0:
            candidate.reached_20pct = True
        if max_pct >= 50.0:
            candidate.reached_50pct = True

        candidate.evaluated_until_ms = future_candles[-1].timestamp_ms

