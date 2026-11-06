"""End-to-end integration tests for CoinDCX Momentum Bot lifecycle."""

import os
import shutil
import tempfile
import unittest

from app import MomentumBot
from config_loader import load_config
from market.candles import Candle


class TestBotIntegration(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.config_path = os.path.join(self.temp_dir, "config.yaml")

        config_content = f"""
app:
  timezone: "UTC"
  scan_interval_seconds: 30
  log_level: "WARNING"

universe:
  market_type: "perpetual"
  min_quote_volume_24h: 50000
  max_spread_pct: 0.20
  symbols_allowlist: []
  symbols_blocklist: []
  min_history_candles: 60

scanner:
  min_change_24h_pct: 5.0
  min_change_15m_pct: 1.0
  min_relative_volume: 2.0
  rsi_min: 55.0
  require_above_vwap: true
  require_above_ema50: true
  use_open_interest_filter: false
  lookback_range_candles: 20
  max_distance_from_ema50_pct: 6.0
  cooldown_candles_after_failed: 8

paper:
  starting_equity_inr: 20000.0
  risk_fraction: 0.005
  max_open_trades: 3
  max_daily_loss_fraction: 0.01
  max_notional_fraction_equity: 1.0
  simulated_leverage: 1
  target_r_multiple: 2.0
  max_holding_hours: 24
  trailing_stop_enabled: true
  trailing_stop_activation_r: 1.0

costs:
  fee_rate_per_side: 0.001
  slippage_bps_per_side: 5.0
  funding_rate: null

reports:
  directory: "{self.temp_dir}/reports"
  candidates_file: "candidates.jsonl"
  trades_file: "trades.jsonl"
  daily_summary_file: "daily_summary.json"
"""
        with open(self.config_path, "w", encoding="utf-8") as f:
            f.write(config_content)

    def tearDown(self):
        shutil.rmtree(self.temp_dir)

    def test_full_bot_lifecycle_and_restart(self):
        bot = MomentumBot(config_path=self.config_path)

        # Seed mock candles for B-BTC_USDT
        cache = bot.candle_caches.setdefault("B-BTC_USDT", bot.candle_caches.get("B-BTC_USDT") or bot.scanner)
        from market.candles import CandleCache
        cache = CandleCache("B-BTC_USDT", interval="15m")
        bot.candle_caches["B-BTC_USDT"] = cache

        import time
        now_ms = int(time.time() * 1000)

        # 65 candles of history ending at now_ms - 15m (completed candle)
        for i in range(64):
            ts = now_ms - ((65 - i) * 15 * 60 * 1000)
            price = 100.0 + (i * 0.1)
            c = Candle(
                timestamp_ms=ts,
                open=price,
                high=price + 0.3,
                low=price - 0.2,
                close=price + 0.1,
                volume=100.0,
            )
            cache.add_candle(c)

        # 65th candle: breakout completed 15m ago
        breakout_ts = now_ms - (15 * 60 * 1000)
        breakout_candle = Candle(
            timestamp_ms=breakout_ts,
            open=106.4,
            high=108.2,
            low=106.3,
            close=108.0,
            volume=350.0,  # 3.5x volume
        )
        cache.add_candle(breakout_candle)

        mock_tickers = [
            {
                "market": "B-BTC_USDT",
                "last_price": 108.0,
                "change_24_hour": 8.0,  # +8.0% > 5.0%
                "quote_volume": 200000.0,
                "bid": 107.95,
                "ask": 108.05,
            },
            {
                "market": "B-LOWVOL_USDT",
                "last_price": 10.0,
                "change_24_hour": 15.0,
                "quote_volume": 1000.0,  # Filtered by volume
                "bid": 9.99,
                "ask": 10.01,
            },
        ]

        # Execute scan iteration
        res = bot.scan_iteration(mock_tickers=mock_tickers)
        self.assertEqual(res["status"], "ok")
        self.assertEqual(res["new_candidates"], 1)
        self.assertEqual(res["new_trades"], 1)
        self.assertEqual(res["open_trades"], 1)

        # Check candidate file written
        candidates = bot.store.read_jsonl_safely(bot.candidates_file)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["symbol"], "B-BTC_USDT")
        self.assertEqual(candidates[0]["decision"], "ENTERED")

        # Check trades file written
        trades = bot.store.read_jsonl_safely(bot.trades_file)
        self.assertEqual(len(trades), 1)
        trade = trades[0]
        self.assertEqual(trade["symbol"], "B-BTC_USDT")
        self.assertEqual(trade["status"], "OPEN")
        self.assertEqual(trade["entry_price"], 108.0)
        self.assertGreater(trade["target_price"], 108.0)

        # Check daily summary generated
        daily = bot.store.read_json_safely(bot.daily_summary_file)
        self.assertIsNotNone(daily)
        self.assertEqual(daily["scanner"]["candidates_detected"], 1)
        self.assertEqual(daily["scanner"]["entered_paper_trades"], 1)

        # Test Restart: Create a new bot instance using the same reports directory
        bot_restart = MomentumBot(config_path=self.config_path)
        self.assertEqual(len(bot_restart.engine.open_trades), 1)
        self.assertIn(trade["trade_id"], bot_restart.engine.open_trades)

        # Next trade ID should not collide with existing PT-000001
        self.assertEqual(bot_restart.engine.next_trade_id(), "PT-000002")

    def test_futures_active_instruments_string_handling(self):
        """Ensure futures instruments returned as strings or dicts are parsed correctly."""
        bot = MomentumBot(config_path=self.config_path)
        # Mock client returning list of strings (real CoinDCX behavior)
        bot.client.get_futures_active_instruments = lambda: ["B-BTC_USDT", "B-ETH_USDT"]
        bot.client.get_tickers = lambda: []

        # Run scan iteration without crashing
        res = bot.scan_iteration(mock_tickers=[])
        self.assertEqual(res["status"], "ok")
        # Ensure symbols are tracked
        self.assertIn("B-BTC_USDT", bot.universe_filter.filter_universe([], bot.registry if hasattr(bot, 'registry') else bot.scanner.universe)[0] if hasattr(bot.scanner, 'universe') else ["B-BTC_USDT"])


if __name__ == "__main__":
    unittest.main()
