"""Unit tests for technical indicators."""

import unittest
from market.candles import Candle
from market.indicators import (
    calculate_atr,
    calculate_ema,
    calculate_relative_volume,
    calculate_rsi,
    calculate_vwap,
    calculate_15m_momentum,
    find_recent_range_high,
    find_swing_low,
)


class TestIndicators(unittest.TestCase):

    def test_calculate_ema(self):
        prices = [float(i) for i in range(1, 60)]
        ema = calculate_ema(prices, period=50)
        self.assertIsNotNone(ema)
        self.assertGreater(ema, 30.0)
        self.assertLess(ema, 50.0)

        # Insufficient prices returns None
        self.assertIsNone(calculate_ema([1.0, 2.0], period=50))

    def test_calculate_rsi(self):
        # Monotonically increasing prices -> RSI should approach 100
        up_prices = [float(10 + i * 2) for i in range(30)]
        rsi_up = calculate_rsi(up_prices, period=14)
        self.assertIsNotNone(rsi_up)
        self.assertGreater(rsi_up, 90.0)

        # Monotonically decreasing prices -> RSI should approach 0
        down_prices = [float(100 - i * 2) for i in range(30)]
        rsi_down = calculate_rsi(down_prices, period=14)
        self.assertIsNotNone(rsi_down)
        self.assertLess(rsi_down, 10.0)

        # Insufficient prices
        self.assertIsNone(calculate_rsi([10.0, 11.0], period=14))

    def test_calculate_vwap(self):
        # 3 candles with known volume and typical prices
        candles = [
            Candle(1000, open=10, high=12, low=8, close=10, volume=100),   # TP = 10, vol = 100 -> 1000
            Candle(2000, open=10, high=14, low=10, close=12, volume=200), # TP = 12, vol = 200 -> 2400
            Candle(3000, open=12, high=16, low=12, close=14, volume=300), # TP = 14, vol = 300 -> 4200
        ]
        # Total TP*vol = 7600, total vol = 600 -> VWAP = 7600 / 600 = 12.66666667
        vwap = calculate_vwap(candles)
        self.assertIsNotNone(vwap)
        self.assertAlmostEqual(vwap, 7600.0 / 600.0, places=4)

    def test_calculate_atr(self):
        candles = [
            Candle(i * 1000, open=100, high=105, low=95, close=102, volume=50)
            for i in range(25)
        ]
        atr = calculate_atr(candles, period=14)
        self.assertIsNotNone(atr)
        self.assertAlmostEqual(atr, 10.0, places=1)

    def test_calculate_relative_volume(self):
        # 20 candles with volume 100, then latest candle with volume 300 -> RVOL = 3.0
        candles = [
            Candle(i * 1000, open=10, high=12, low=9, close=11, volume=100.0)
            for i in range(20)
        ]
        candles.append(Candle(21000, open=11, high=15, low=10, close=14, volume=300.0))

        rvol = calculate_relative_volume(candles, period=20)
        self.assertIsNotNone(rvol)
        self.assertEqual(rvol, 3.0)

    def test_calculate_15m_momentum(self):
        c = [Candle(1000, open=100.0, high=110.0, low=99.0, close=105.0, volume=100.0)]
        # (105 - 100) / 100 * 100 = 5.0%
        mom = calculate_15m_momentum(c)
        self.assertEqual(mom, 5.0)

    def test_find_recent_range_high_and_swing_low(self):
        candles = [
            Candle(i * 1000, open=100, high=100 + i, low=90 + i, close=95 + i, volume=100)
            for i in range(25)
        ]
        # Prior 20 candles excluding the last one
        range_high = find_recent_range_high(candles, lookback=20)
        self.assertIsNotNone(range_high)
        # Last candle was i=24 (high=124). Prior candle at i=23 has high=123
        self.assertEqual(range_high, 123.0)

        swing_low = find_swing_low(candles, lookback=5)
        self.assertIsNotNone(swing_low)
        self.assertEqual(swing_low, 110.0)  # low at i=20 is 90+20=110


if __name__ == "__main__":
    unittest.main()
