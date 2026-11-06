"""Universe & liquidity filtering for CoinDCX markets.

Enforces separate spot vs perpetual universes, minimum liquidity thresholds,
maximum spreads, and audit logging of exclusions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Set, Tuple
from coindcx.symbols import MarketMetadata, SymbolRegistry

logger = logging.getLogger("market.universe")


@dataclass
class UniverseFilterConfig:
    market_type: str = "perpetual"  # "perpetual" or "spot"
    min_quote_volume_24h: float = 100000.0
    max_spread_pct: float = 0.20  # In percent: 0.20 means 0.20%
    symbols_allowlist: List[str] = field(default_factory=list)
    symbols_blocklist: List[str] = field(default_factory=list)
    min_history_candles: int = 60


@dataclass
class ExclusionRecord:
    symbol: str
    reason: str
    details: Dict[str, Any] = field(default_factory=dict)


class UniverseFilter:
    """Filters market universe based on market type, liquidity, and audit constraints."""

    def __init__(self, config: UniverseFilterConfig) -> None:
        self.config = config

    def evaluate_market(
        self,
        symbol: str,
        ticker: Optional[Dict[str, Any]],
        metadata: Optional[MarketMetadata],
        candle_count: int = 100,
    ) -> Tuple[bool, Optional[str], Dict[str, Any]]:
        """Evaluate a single market against universe rules.

        Returns: (is_eligible, reason_if_excluded, details)
        """
        # 1. Allowlist / Blocklist checks
        if self.config.symbols_allowlist and symbol not in self.config.symbols_allowlist:
            return False, "NOT_IN_ALLOWLIST", {"symbol": symbol}

        if symbol in self.config.symbols_blocklist:
            return False, "BLOCKLISTED", {"symbol": symbol}

        # 2. Market type separation (Section 5.1)
        detected_type = (
            metadata.market_type if metadata else SymbolRegistry.classify_market_type(symbol)
        )
        if self.config.market_type.lower() != "all" and detected_type.lower() != self.config.market_type.lower():
            return False, "MARKET_TYPE_MISMATCH", {
                "expected": self.config.market_type,
                "actual": detected_type,
            }

        # 3. Market status check (halted / delisted)
        if metadata and not metadata.is_active:
            return False, "MARKET_INACTIVE", {"status": metadata.status}

        # 4. Ticker availability check
        if not ticker:
            return False, "MISSING_TICKER", {}

        # 5. Volume check (in quote asset)
        last_price = float(ticker.get("last_price") or 0.0)
        volume = float(ticker.get("volume") or 0.0)
        quote_volume = float(ticker.get("quote_volume") or (volume * last_price))

        # Enforce volume threshold if volume data is available in ticker
        if quote_volume > 0.0 and quote_volume < self.config.min_quote_volume_24h:
            return False, "INSUFFICIENT_VOLUME", {
                "quote_volume": round(quote_volume, 2),
                "required": self.config.min_quote_volume_24h,
            }

        # 6. Bid/Ask Spread check
        bid = float(ticker.get("bid") or 0.0)
        ask = float(ticker.get("ask") or 0.0)
        if bid > 0.0 and ask > 0.0 and ask >= bid:
            spread_pct = ((ask - bid) / ask) * 100.0
            if spread_pct > self.config.max_spread_pct:
                return False, "EXCESSIVE_SPREAD", {
                    "spread_pct": round(spread_pct, 4),
                    "max_allowed": self.config.max_spread_pct,
                }
        else:
            # If bid/ask not provided in ticker feed (common in derivatives), use nominal spread
            spread_pct = 0.05

        # 7. Candle history depth check
        if candle_count < self.config.min_history_candles:
            return False, "INSUFFICIENT_HISTORY", {
                "candle_count": candle_count,
                "required": self.config.min_history_candles,
            }

        return True, None, {"quote_volume": quote_volume, "spread_pct": spread_pct}

    def filter_universe(
        self,
        tickers: List[Dict[str, Any]],
        registry: SymbolRegistry,
        candle_counts: Optional[Dict[str, int]] = None,
    ) -> Tuple[List[str], List[ExclusionRecord]]:
        """Filter list of tickers and return eligible symbols plus exclusion audit records."""
        candle_counts = candle_counts or {}
        eligible: List[str] = []
        exclusions: List[ExclusionRecord] = []

        ticker_map: Dict[str, Dict[str, Any]] = {}
        for t in tickers:
            sym = t.get("market") or t.get("symbol")
            if sym:
                ticker_map[sym] = t

        all_symbols = set(ticker_map.keys()) | set(registry.all_symbols())

        for sym in sorted(all_symbols):
            t = ticker_map.get(sym)
            meta = registry.get(sym)
            count = candle_counts.get(sym, 100)  # Default assume sufficient if not yet fetched

            ok, reason, details = self.evaluate_market(sym, t, meta, count)
            if ok:
                eligible.append(sym)
            else:
                exclusions.append(
                    ExclusionRecord(symbol=sym, reason=reason or "UNKNOWN", details=details)
                )

        logger.info(
            "Universe filter complete: %d eligible, %d excluded (target: %s)",
            len(eligible),
            len(exclusions),
            self.config.market_type,
        )
        return eligible, exclusions

