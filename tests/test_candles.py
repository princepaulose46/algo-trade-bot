"""Unit tests for candle validation, cache, deduplication, and stale checks."""

import time
import unittest
from market.candles import Candle, CandleCache


class TestCandles(unittest.TestCase):

    def test_candle_geometry_validation(self):
        # Valid candle
        c = Candle(1600000000000, open=100.0, high=105.0, low=98.0, close=102.0, volume=50.0)
        self.assertTrue(c.is_valid())

        # Invalid: high < low
        c_bad1 = Candle(1600000000000, open=100.0, high=95.0, low=98.0, close=96.0, volume=50.0)
        self.assertFalse(c_bad1.is_valid())

        # Invalid: low > open
        c_bad2 = Candle(1600000000000, open=95.0, high=105.0, low=98.0, close=102.0, volume=50.0)
        self.assertFalse(c_bad2.is_valid())

        # Invalid: negative volume
        c_bad3 = Candle(1600000000000, open=100.0, high=105.0, low=98.0, close=102.0, volume=-10.0)
        self.assertFalse(c_bad3.is_valid())

        # Invalid: zero or negative price
        c_bad4 = Candle(1600000000000, open=0.0, high=105.0, low=98.0, close=102.0, volume=10.0)
        self.assertFalse(c_bad4.is_valid())

    def test_cache_deduplication(self):
        cache = CandleCache("B-BTC_USDT")
        c1 = Candle(1000, 100, 105, 95, 101, 10)
        c2 = Candle(1000, 100, 105, 95, 101, 10)  # Duplicate timestamp

        self.assertTrue(cache.add_candle(c1))
        self.assertFalse(cache.add_candle(c2))  # Should reject duplicate
        self.assertEqual(len(cache.candles), 1)

    def test_cache_out_of_order_sorting(self):
        cache = CandleCache("B-BTC_USDT")
        c1 = Candle(3000, 102, 106, 98, 105, 10)
        c2 = Candle(1000, 100, 105, 95, 101, 10)
        c3 = Candle(2000, 101, 104, 96, 102, 10)

        cache.add_candle(c1)
        cache.add_candle(c2)
        cache.add_candle(c3)

        self.assertEqual([c.timestamp_ms for c in cache.candles], [1000, 2000, 3000])

    def test_ingest_raw_candles_and_exclude_unfinished(self):
        cache = CandleCache("B-BTC_USDT")
        now_ms = int(time.time() * 1000)

        raw = [
            {"time": now_ms - (30 * 60 * 1000), "open": 100, "high": 102, "low": 99, "close": 101, "volume": 50},
            {"time": now_ms - (16 * 60 * 1000), "open": 101, "high": 103, "low": 100, "close": 102, "volume": 60},
            # This candle is only 2 minutes old, so it's still forming (<15m)
            {"time": now_ms - (2 * 60 * 1000), "open": 102, "high": 104, "low": 101, "close": 103, "volume": 10},
        ]

        added = cache.ingest_raw_candles(raw, exclude_unfinished=True, interval_minutes=15)
        self.assertEqual(added, 2)
        self.assertEqual(len(cache.candles), 2)


if __name__ == "__main__":
    unittest.main()

