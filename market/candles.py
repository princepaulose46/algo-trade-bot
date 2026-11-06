"""Candle normalization, validation, deduplication, and cache management."""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("market.candles")


@dataclass
class Candle:
    timestamp_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    is_closed: bool = True

    @property
    def typical_price(self) -> float:
        """Typical price for VWAP: (High + Low + Close) / 3."""
        return (self.high + self.low + self.close) / 3.0

    @property
    def iso_time(self) -> str:
        """UTC ISO-8601 formatted string."""
        dt = datetime.datetime.fromtimestamp(self.timestamp_ms / 1000.0, tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def is_valid(self) -> bool:
        """Validate candle geometry and non-negative values."""
        if any(v is None for v in (self.open, self.high, self.low, self.close, self.volume)):
            return False
        if self.open <= 0 or self.high <= 0 or self.low <= 0 or self.close <= 0:
            return False
        if self.volume < 0:
            return False
        if self.high < self.low:
            return False
        if self.high < self.open or self.high < self.close:
            return False
        if self.low > self.open or self.low > self.close:
            return False
        return True


class CandleCache:
    """Manages candles for a symbol, enforcing deduplication and completed-only rules."""

    def __init__(self, symbol: str, interval: str = "15m", max_size: int = 200) -> None:
        self.symbol = symbol
        self.interval = interval
        self.max_size = max_size
        self._candles: List[Candle] = []
        self._timestamps: set[int] = set()
        self._last_processed_candle_time: int = 0

    @property
    def candles(self) -> List[Candle]:
        return list(self._candles)

    @property
    def last_processed_candle_time(self) -> int:
        return self._last_processed_candle_time

    def set_last_processed_candle_time(self, ts: int) -> None:
        self._last_processed_candle_time = max(self._last_processed_candle_time, ts)

    def is_new_completed_candle(self) -> bool:
        """Check if the latest completed candle has not yet been processed."""
        if not self._candles:
            return False
        latest = self._candles[-1]
        return latest.timestamp_ms > self._last_processed_candle_time

    def mark_latest_as_processed(self) -> None:
        if self._candles:
            self._last_processed_candle_time = self._candles[-1].timestamp_ms

    def add_candle(self, candle: Candle) -> bool:
        """Add a validated candle. Returns True if accepted and newly inserted."""
        if not candle.is_valid():
            logger.debug("Rejected invalid candle for %s: %s", self.symbol, candle)
            return False

        if candle.timestamp_ms in self._timestamps:
            # Duplicate candle timestamp
            return False

        self._timestamps.add(candle.timestamp_ms)
        self._candles.append(candle)
        self._candles.sort(key=lambda c: c.timestamp_ms)

        if len(self._candles) > self.max_size:
            removed = self._candles.pop(0)
            self._timestamps.discard(removed.timestamp_ms)

        return True

    def ingest_raw_candles(
        self,
        raw_list: List[Dict[str, Any]],
        exclude_unfinished: bool = True,
        interval_minutes: int = 15,
    ) -> int:
        """Normalize and store candles from CoinDCX API list.

        CoinDCX raw schema:
        [{"open": float, "high": float, "low": float, "close": float, "volume": float, "time": ms}]
        """
        if not raw_list:
            return 0

        # Sort raw list by time ascending
        parsed_candles: List[Candle] = []
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        interval_ms = interval_minutes * 60 * 1000

        for item in raw_list:
            try:
                # Time can be named 'time' or 'timestamp'
                ts = int(item.get("time") or item.get("timestamp") or 0)
                if ts <= 0:
                    continue
                # Normalize seconds to milliseconds epoch if needed
                if 0 < ts < 10000000000:
                    ts *= 1000

                open_p = float(item.get("open", 0))
                high_p = float(item.get("high", 0))
                low_p = float(item.get("low", 0))
                close_p = float(item.get("close", 0))
                vol = float(item.get("volume", 0))

                # Section 5.4: "Use completed candles only; do not act on unfinished candles."
                # A candle whose start time + interval_ms > now_ms is still actively forming
                is_closed = (now_ms - ts) >= interval_ms

                if exclude_unfinished and not is_closed:
                    continue

                c = Candle(
                    timestamp_ms=ts,
                    open=open_p,
                    high=high_p,
                    low=low_p,
                    close=close_p,
                    volume=vol,
                    is_closed=is_closed,
                )
                if c.is_valid():
                    parsed_candles.append(c)
            except (ValueError, TypeError) as exc:
                logger.debug("Failed parsing raw candle for %s: %s (%s)", self.symbol, item, exc)

        parsed_candles.sort(key=lambda c: c.timestamp_ms)
        added_count = 0
        for candle in parsed_candles:
            if self.add_candle(candle):
                added_count += 1

        return added_count

    def is_stale(self, max_allowed_gap_minutes: int = 45) -> bool:
        """Check if candle data is stale compared to current wall-clock time."""
        if not self._candles:
            return True
        latest = self._candles[-1]
        now_ms = int(datetime.datetime.now(datetime.timezone.utc).timestamp() * 1000)
        gap_minutes = (now_ms - latest.timestamp_ms) / (60 * 1000)
        return gap_minutes > max_allowed_gap_minutes

