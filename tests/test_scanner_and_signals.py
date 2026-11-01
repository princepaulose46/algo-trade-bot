"""Unit tests for scanner triggers, signal evaluation, and anti-chase rules."""

import unittest
from market.candles import Candle
from strategy.scanner import CandidateRecord, MomentumScanner, ScannerConfig
from strategy.signals import SignalEvaluator


class TestScannerAndSignals(unittest.TestCase):

    def setUp(self):
        self.config = ScannerConfig(
            min_change_24h_pct=5.0,
            min_change_15m_pct=1.0,
            min_relative_volume=2.0,
            rsi_min=55.0,
            require_above_vwap=True,
            require_above_ema50=True,
            use_open_interest_filter=False,
            lookback_range_candles=20,
            max_distance_from_ema50_pct=6.0,
            cooldown_candles_after_failed=8,
        )
        self.scanner = MomentumScanner(self.config)
        self.evaluator = SignalEvaluator(self.config)

    def _generate_candles(self, base_price=100.0, count=70, breakout=True, high_volume=True):
        candles = []
        for i in range(count - 1):
            # Slow steady uptrend
            price = base_price + (i * 0.2)
            c = Candle(
                timestamp_ms=(i + 1) * 15 * 60 * 1000,
                open=price,
                high=price + 0.5,
                low=price - 0.4,
                close=price + 0.2,
                volume=100.0,
            )
            candles.append(c)

        # Latest candle: momentum breakout
        last_price = base_price + ((count - 1) * 0.2)
        close_price = last_price + 1.5 if breakout else last_price - 0.5
        vol = 300.0 if high_volume else 80.0

        latest = Candle(
            timestamp_ms=count * 15 * 60 * 1000,
            open=last_price,
            high=close_price + 0.3 if breakout else last_price + 0.2,
            low=last_price - 0.2,
            close=close_price,
            volume=vol,
        )
        candles.append(latest)
        return candles

    def test_initial_trigger_success(self):
        candles = self._generate_candles(breakout=True, high_volume=True)
        ticker = {"market": "B-BTC_USDT", "change_24_hour": 7.5}
        triggered, metrics, reason = self.scanner.check_initial_trigger(
            symbol="B-BTC_USDT",
            ticker=ticker,
            candles=candles,
            spread_pct=0.08,
            quote_volume=500000.0,
            min_quote_volume=100000.0,
            max_spread_pct=0.20,
        )
        self.assertTrue(triggered)
        self.assertIsNone(reason)
        self.assertEqual(metrics["change_24h_pct"], 7.5)
        self.assertGreaterEqual(metrics["relative_volume"], 2.0)

    def test_initial_trigger_fails_low_24h_change(self):
        candles = self._generate_candles(breakout=True, high_volume=True)
        ticker = {"market": "B-BTC_USDT", "change_24_hour": 3.0}  # Below 5.0%
        triggered, metrics, reason = self.scanner.check_initial_trigger(
            symbol="B-BTC_USDT",
            ticker=ticker,
            candles=candles,
            spread_pct=0.08,
            quote_volume=500000.0,
            min_quote_volume=100000.0,
            max_spread_pct=0.20,
        )
        self.assertFalse(triggered)
        self.assertEqual(reason, "CHANGE_24H_BELOW_MIN")

    def test_initial_trigger_fails_low_rvol(self):
        candles = self._generate_candles(breakout=True, high_volume=False)  # RVOL < 2.0
        ticker = {"market": "B-BTC_USDT", "change_24_hour": 6.5}
        triggered, metrics, reason = self.scanner.check_initial_trigger(
            symbol="B-BTC_USDT",
            ticker=ticker,
            candles=candles,
            spread_pct=0.08,
            quote_volume=500000.0,
            min_quote_volume=100000.0,
            max_spread_pct=0.20,
        )
        self.assertFalse(triggered)
        self.assertEqual(reason, "INSUFFICIENT_RELATIVE_VOLUME")

    def test_continuation_confirmation_success(self):
        candles = self._generate_candles(breakout=True, high_volume=True)
        sig = self.evaluator.evaluate_long_signal("B-BTC_USDT", candles)
        self.assertTrue(sig.confirmed)
        self.assertIsNone(sig.reason)
        self.assertGreater(sig.entry_price, 0)
        self.assertLess(sig.stop_price, sig.entry_price)

    def test_anti_chase_extended_from_ema(self):
        # Create prices with extreme spike (extended > 6%)
        candles = self._generate_candles(breakout=True, high_volume=True)
        last = candles[-1]
        candles[-1] = Candle(
            timestamp_ms=last.timestamp_ms,
            open=last.open,
            high=last.open * 1.15,
            low=last.open,
            close=last.open * 1.12,  # +12% sudden extension above EMA
            volume=500.0,
        )
        sig = self.evaluator.evaluate_long_signal("B-BTC_USDT", candles)
        self.assertFalse(sig.confirmed)
        self.assertEqual(sig.reason, "EXTENDED_FROM_EMA")

    def test_cooldown_rejection(self):
        candles = self._generate_candles(breakout=True, high_volume=True)
        latest_ts = candles[-1].timestamp_ms

        # Set cooldown 2 candles ago
        self.evaluator.set_cooldown("B-BTC_USDT", latest_ts - (2 * 15 * 60 * 1000))
        sig = self.evaluator.evaluate_long_signal("B-BTC_USDT", candles)
        self.assertFalse(sig.confirmed)
        self.assertEqual(sig.reason, "COOLDOWN_ACTIVE")

    def test_candidate_outcome_tracking(self):
        candidate = CandidateRecord(
            candidate_id="C-000001",
            symbol="B-BTC_USDT",
            detected_at="2026-11-01T14:32:00Z",
            change_24h_pct=5.5,
            change_15m_pct=2.0,
            relative_volume=3.0,
            spread_pct=0.08,
            vwap_distance_pct=0.5,
            rsi_15m=62.0,
            entry_reference_price=100.0,
            detected_timestamp_ms=10000,
        )

        # Future candles moving up +25%
        future = [
            Candle(10000 + (i * 15 * 60 * 1000), 100 + i, 101 + i, 99 + i, 100.5 + i, 50)
            for i in range(1, 25)
        ]
        # At i=24, high is 101 + 24 = 125 (+25% gain)
        self.scanner.update_candidate_outcomes(candidate, future)

        self.assertTrue(candidate.reached_10pct)
        self.assertTrue(candidate.reached_20pct)
        self.assertFalse(candidate.reached_50pct)
        self.assertIsNotNone(candidate.max_return_1h_pct)
        self.assertIsNotNone(candidate.max_return_4h_pct)


if __name__ == "__main__":
    unittest.main()
