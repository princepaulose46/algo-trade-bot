"""Unit tests for BacktestRunner and historical metrics computation."""

import os
import shutil
import tempfile
import unittest

from backtest import BacktestRunner
from market.candles import Candle


class TestBacktest(unittest.TestCase):

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

    def test_backtest_runner(self):
        runner = BacktestRunner(config_path=self.config_path)

        # Build 120 candles with a clear momentum continuation trade setup
        candles = []
        base_price = 100.0
        for i in range(120):
            if i == 70:
                # Breakout candle
                price = base_price + (i * 0.1) + 1.2
                vol = 400.0
            elif i > 70 and i < 85:
                # Continuation rally reaching 2R target (+10% gain)
                price = base_price + (i * 0.1) + 8.0
                vol = 150.0
            else:
                price = base_price + (i * 0.1)
                vol = 100.0

            c = Candle(
                timestamp_ms=(i + 1) * 15 * 60 * 1000,
                open=price,
                high=price + 0.5,
                low=price - 0.3,
                close=price + 0.2,
                volume=vol,
            )
            candles.append(c)

        result = runner.run("B-BTC_USDT", candles)
        self.assertEqual(result.total_candles_replayed, 120)
        self.assertGreater(len(runner.candidates), 0)
        self.assertGreaterEqual(result.trade_count, 1)
        self.assertIsNotNone(result.win_rate_pct)
        self.assertIsNotNone(result.profit_factor)
        self.assertIsNotNone(result.expectancy)


if __name__ == "__main__":
    unittest.main()

