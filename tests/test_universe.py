"""Unit tests for Universe and Liquidity filtering."""

import unittest
from coindcx.symbols import MarketMetadata, SymbolRegistry
from market.universe import UniverseFilter, UniverseFilterConfig


class TestUniverseFilter(unittest.TestCase):

    def setUp(self):
        self.config = UniverseFilterConfig(
            market_type="perpetual",
            min_quote_volume_24h=100000.0,
            max_spread_pct=0.20,
            min_history_candles=60,
        )
        self.filter = UniverseFilter(self.config)

    def test_market_type_separation(self):
        # Spot market when perpetual is required
        spot_meta = MarketMetadata(
            symbol="BTCUSDT",
            market_type="spot",
            base_asset="BTC",
            quote_asset="USDT",
            status="active",
        )
        spot_ticker = {
            "market": "BTCUSDT",
            "last_price": 50000.0,
            "volume": 10.0,
            "quote_volume": 500000.0,
            "bid": 49990.0,
            "ask": 50010.0,
        }
        ok, reason, details = self.filter.evaluate_market("BTCUSDT", spot_ticker, spot_meta, 100)
        self.assertFalse(ok)
        self.assertEqual(reason, "MARKET_TYPE_MISMATCH")

        # Perpetual market
        perp_meta = MarketMetadata(
            symbol="B-BTC_USDT",
            market_type="perpetual",
            base_asset="BTC",
            quote_asset="USDT",
            status="active",
        )
        perp_ticker = {
            "market": "B-BTC_USDT",
            "last_price": 50000.0,
            "volume": 10.0,
            "quote_volume": 500000.0,
            "bid": 49990.0,
            "ask": 50010.0,
        }
        ok, reason, details = self.filter.evaluate_market("B-BTC_USDT", perp_ticker, perp_meta, 100)
        self.assertTrue(ok)
        self.assertIsNone(reason)

    def test_insufficient_volume_exclusion(self):
        meta = MarketMetadata("B-DOGE_USDT", "perpetual", "DOGE", "USDT", "active")
        ticker = {
            "market": "B-DOGE_USDT",
            "last_price": 0.10,
            "volume": 1000.0,
            "quote_volume": 100.0,  # Below 100,000 threshold
            "bid": 0.0999,
            "ask": 0.1001,
        }
        ok, reason, details = self.filter.evaluate_market("B-DOGE_USDT", ticker, meta, 100)
        self.assertFalse(ok)
        self.assertEqual(reason, "INSUFFICIENT_VOLUME")

    def test_excessive_spread_exclusion(self):
        meta = MarketMetadata("B-ABC_USDT", "perpetual", "ABC", "USDT", "active")
        ticker = {
            "market": "B-ABC_USDT",
            "last_price": 10.0,
            "quote_volume": 200000.0,
            "bid": 9.90,
            "ask": 10.10,  # Spread is (0.20 / 10.10) * 100 = ~1.98% > 0.20% max
        }
        ok, reason, details = self.filter.evaluate_market("B-ABC_USDT", ticker, meta, 100)
        self.assertFalse(ok)
        self.assertEqual(reason, "EXCESSIVE_SPREAD")

    def test_exclusion_audit_records(self):
        tickers = [
            {"market": "BTCUSDT", "last_price": 50000, "quote_volume": 1000000, "bid": 49990, "ask": 50010},
            {"market": "B-BTC_USDT", "last_price": 50000, "quote_volume": 1000000, "bid": 49990, "ask": 50010},
            {"market": "B-LOWVOL_USDT", "last_price": 1, "quote_volume": 500, "bid": 0.999, "ask": 1.001},
        ]
        registry = SymbolRegistry.from_tickers(tickers)
        eligible, exclusions = self.filter.filter_universe(tickers, registry)

        self.assertEqual(len(eligible), 1)
        self.assertEqual(eligible[0], "B-BTC_USDT")

        self.assertEqual(len(exclusions), 2)
        reasons = {e.symbol: e.reason for e in exclusions}
        self.assertEqual(reasons["BTCUSDT"], "MARKET_TYPE_MISMATCH")
        self.assertEqual(reasons["B-LOWVOL_USDT"], "INSUFFICIENT_VOLUME")


if __name__ == "__main__":
    unittest.main()

