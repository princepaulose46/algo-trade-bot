"""Symbol discovery, metadata parsing, and market classification for CoinDCX."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger("coindcx.symbols")


@dataclass
class MarketMetadata:
    symbol: str  # e.g., "B-BTC_USDT" or "BTCUSDT"
    market_type: str  # "perpetual" or "spot"
    base_asset: str  # e.g., "BTC"
    quote_asset: str  # e.g., "USDT" or "INR"
    status: str  # "active", "halted", "delisted"
    min_quantity: float = 0.0001
    step_size: float = 0.0001
    min_price: float = 0.00000001
    tick_size: float = 0.01

    @property
    def is_active(self) -> bool:
        return self.status.lower() in ("active", "trading", "open")


class SymbolRegistry:
    """Manages active market symbols and separates spot vs perpetual markets."""

    def __init__(self) -> None:
        self._markets: Dict[str, MarketMetadata] = {}

    def register(self, metadata: MarketMetadata) -> None:
        self._markets[metadata.symbol] = metadata

    def get(self, symbol: str) -> Optional[MarketMetadata]:
        return self._markets.get(symbol)

    def all_symbols(self) -> List[str]:
        return list(self._markets.keys())

    @staticmethod
    def classify_market_type(symbol: str) -> str:
        """Classify a symbol as 'perpetual' or 'spot'.

        On CoinDCX:
        Derivatives/Futures typically have prefixes like 'B-' (Binance liquidity)
        or explicit suffix 'USDT-PERP' or contain '_USDT' with 'B-' prefix.
        Spot pairs are typically like 'BTCUSDT', 'ETHINR', 'BTC_INR'.
        """
        sym = symbol.strip().upper()
        if sym.startswith("B-") or "PERP" in sym or sym.startswith("F-"):
            return "perpetual"
        return "spot"

    @classmethod
    def parse_market_details(cls, raw_list: List[Dict[str, Any]]) -> SymbolRegistry:
        registry = cls()
        for item in raw_list:
            symbol = item.get("symbol") or item.get("pair") or item.get("coindcx_name")
            if not symbol:
                continue

            market_type = cls.classify_market_type(symbol)
            base_asset = item.get("target_currency_short_name") or item.get("base_currency") or ""
            quote_asset = item.get("base_currency_short_name") or item.get("quote_currency") or ""
            status = item.get("status", "active")
            min_quantity = float(item.get("min_quantity") or item.get("min_qty") or 0.0001)
            step_size = float(item.get("step") or item.get("step_size") or 0.0001)
            min_price = float(item.get("min_price") or 0.00000001)
            tick_size = float(item.get("tick_size") or item.get("price_step") or 0.01)

            metadata = MarketMetadata(
                symbol=symbol,
                market_type=market_type,
                base_asset=base_asset,
                quote_asset=quote_asset,
                status=status,
                min_quantity=min_quantity,
                step_size=step_size,
                min_price=min_price,
                tick_size=tick_size,
            )
            registry.register(metadata)

        return registry

    @classmethod
    def from_tickers(cls, tickers: List[Dict[str, Any]]) -> SymbolRegistry:
        """Synthesize basic metadata from tickers list when market_details is unavailable."""
        registry = cls()
        for item in tickers:
            symbol = item.get("market") or item.get("symbol")
            if not symbol:
                continue
            market_type = cls.classify_market_type(symbol)
            metadata = MarketMetadata(
                symbol=symbol,
                market_type=market_type,
                base_asset="",
                quote_asset="USDT" if "USDT" in symbol else ("INR" if "INR" in symbol else ""),
                status="active",
            )
            registry.register(metadata)
        return registry

