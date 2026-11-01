"""CoinDCX Public Market Data Client.

Provides read-only access to CoinDCX public REST endpoints and WebSocket
streaming interfaces. Never uses or requires trading API keys.
"""

from __future__ import annotations

import json
import logging
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("coindcx.client")

# Official Base URLs
API_BASE_URL = "https://api.coindcx.com"
PUBLIC_DATA_BASE_URL = "https://public.coindcx.com"
DEFAULT_TIMEOUT_SECONDS = 10.0


@dataclass
class TickerSnapshot:
    symbol: str
    last_price: float
    change_24h_pct: float
    high_24h: float
    low_24h: float
    volume: float
    bid: Optional[float] = None
    ask: Optional[float] = None
    spread_pct: Optional[float] = None
    timestamp: float = 0.0
    source_method: str = "ticker_field"


class CoinDCXClient:
    """Public read-only client for CoinDCX exchange market data."""

    def __init__(
        self,
        api_base: str = API_BASE_URL,
        public_base: str = PUBLIC_DATA_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        max_retries: int = 3,
        backoff_factor: float = 1.5,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.public_base = public_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_factor = backoff_factor
        self._last_request_time = 0.0
        self._min_interval = 0.1  # Max ~10 req/s to be conservative and safe

    def _http_get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """Execute HTTP GET with rate limiting and exponential backoff."""
        if params:
            query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            full_url = f"{url}?{query}" if query else url
        else:
            full_url = url

        headers = {
            "User-Agent": "CoinDCX-Momentum-Bot/1.0",
            "Accept": "application/json",
        }

        # Local pacing to stay well under rate limits
        now = time.time()
        elapsed = now - self._last_request_time
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)

        attempt = 0
        backoff = 1.0
        while attempt <= self.max_retries:
            try:
                self._last_request_time = time.time()
                req = urllib.request.Request(full_url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=self.timeout) as response:
                    body = response.read().decode("utf-8")
                    return json.loads(body)
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
                attempt += 1
                status = getattr(exc, "code", None)
                if status == 429:
                    logger.warning("Rate limit hit (429) for %s. Backing off for %.2fs", full_url, backoff)
                else:
                    logger.warning("Network error on attempt %d/%d for %s: %s", attempt, self.max_retries, full_url, exc)

                if attempt > self.max_retries:
                    logger.error("Failed request to %s after %d retries: %s", full_url, self.max_retries, exc)
                    raise ConnectionError(f"HTTP request failed: {url} -> {exc}") from exc
                time.sleep(backoff)
                backoff *= self.backoff_factor
            except json.JSONDecodeError as exc:
                logger.error("Malformed JSON response from %s: %s", full_url, exc)
                raise ValueError(f"Malformed JSON from {full_url}") from exc

    def get_tickers(self) -> List[Dict[str, Any]]:
        """Fetch all tickers across active markets.
        Endpoint: GET https://api.coindcx.com/exchange/ticker
        """
        url = f"{self.api_base}/exchange/ticker"
        data = self._http_get(url)
        if not isinstance(data, list):
            raise ValueError(f"Expected list of tickers, received {type(data)}")
        return data

    def get_market_details(self) -> List[Dict[str, Any]]:
        """Fetch active market specifications and precision metadata.
        Endpoint: GET https://api.coindcx.com/exchange/v1/market_details
        """
        url = f"{self.api_base}/exchange/v1/market_details"
        try:
            data = self._http_get(url)
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and "markets" in data:
                return data["markets"]
            return []
        except Exception as exc:
            logger.warning("Failed to fetch market_details, falling back to empty list: %s", exc)
            return []

    def get_active_markets(self) -> List[str]:
        """Fetch active market pair symbols.
        Endpoint: GET https://api.coindcx.com/exchange/v1/markets
        """
        url = f"{self.api_base}/exchange/v1/markets"
        data = self._http_get(url)
        if isinstance(data, list):
            return data
        return []

    def get_candles(
        self,
        pair: str,
        interval: str = "15m",
        limit: int = 100,
        start_time: Optional[int] = None,
        end_time: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """Fetch candlestick bars.
        Endpoint: GET https://public.coindcx.com/market_data/candles
        Parameters: pair, interval, limit, startTime, endTime
        """
        url = f"{self.public_base}/market_data/candles"
        params = {
            "pair": pair,
            "interval": interval,
            "limit": limit,
            "startTime": start_time,
            "endTime": end_time,
        }
        data = self._http_get(url, params=params)
        if not isinstance(data, list):
            raise ValueError(f"Expected list of candles for {pair}, got {type(data)}")
        return data

    def get_orderbook(self, pair: str) -> Dict[str, Any]:
        """Fetch orderbook snapshot for spread & liquidity calculation.
        Endpoint: GET https://public.coindcx.com/market_data/orderbook/
        """
        url = f"{self.public_base}/market_data/orderbook/"
        params = {"pair": pair}
        data = self._http_get(url, params=params)
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict orderbook for {pair}, got {type(data)}")
        return data

    def get_derivatives_context(self, pair: str) -> Dict[str, Any]:
        """Fetch open interest and funding rate if supported.
        Per Section 2 & 5.3:
        Do not assume every metric is available for every market. Missing open
        interest must be recorded as unavailable (null), not fabricated.
        """
        # CoinDCX public futures endpoints may provide mark price/funding in tickers.
        # If open interest is not exposed publicly on REST, return explicit nulls.
        return {
            "open_interest": None,
            "open_interest_change_pct": None,
            "funding_rate": None,
            "available": False,
        }

