"""Unit tests for Paper-trading execution engine, sizing, and exits."""

import time
import unittest
from market.candles import Candle
from paper.engine import PaperEngine, PaperTrade
from paper.sizing import PositionSizer, SizingConfig


class TestPaperEngine(unittest.TestCase):

    def setUp(self):
        self.config = SizingConfig(
            starting_equity=20000.0,
            risk_fraction=0.005,  # 0.5% = 100 INR
            max_open_trades=3,
            max_daily_loss_fraction=0.01,  # 1% = 200 INR
            max_notional_fraction_equity=1.0,
            fee_rate_per_side=0.001,
            slippage_bps_per_side=5.0,
        )
        self.engine = PaperEngine(
            sizing_config=self.config,
            target_r_multiple=2.0,
            max_holding_hours=24,
            trailing_stop_enabled=True,
            trailing_stop_activation_r=1.0,
        )
        self.now_ts = int(time.time() * 1000)

    def test_position_sizing_calculation(self):
        sizer = PositionSizer(self.config)
        res = sizer.calculate_sizing(
            equity=20000.0,
            entry_price=100.0,
            stop_price=98.0,
            current_open_trades_count=0,
            daily_loss_accumulated=0.0,
            start_of_day_equity=20000.0,
        )
        self.assertTrue(res.valid)
        self.assertEqual(res.risk_amount, 100.0)
        self.assertEqual(res.risk_per_unit, 2.0)
        self.assertEqual(res.quantity, 50.0)
        self.assertEqual(res.notional, 5000.0)

    def test_max_concurrent_trades_guardrail(self):
        # Open 3 trades
        for i in range(3):
            trade, rej = self.engine.open_trade(
                symbol=f"B-COIN{i}_USDT",
                market_type="perpetual",
                detected_at="2026-11-01T10:00:00Z",
                entered_at="2026-11-01T10:00:00Z",
                entry_price=100.0,
                stop_price=98.0,
                entry_timestamp_ms=self.now_ts,
            )
            self.assertIsNotNone(trade)

        # 4th trade should be rejected
        trade4, rej = self.engine.open_trade(
            symbol="B-COIN4_USDT",
            market_type="perpetual",
            detected_at="2026-11-01T10:00:00Z",
            entered_at="2026-11-01T10:00:00Z",
            entry_price=100.0,
            stop_price=98.0,
            entry_timestamp_ms=self.now_ts,
        )
        self.assertIsNone(trade4)
        self.assertEqual(rej, "MAX_CONCURRENT_TRADES_REACHED")

    def test_max_daily_loss_limit(self):
        # Set accumulated loss to 250 INR (> 200 INR max daily loss)
        self.engine.daily_loss_accumulated = 250.0
        trade, rej = self.engine.open_trade(
            symbol="B-BTC_USDT",
            market_type="perpetual",
            detected_at="2026-11-01T10:00:00Z",
            entered_at="2026-11-01T10:00:00Z",
            entry_price=100.0,
            stop_price=98.0,
            entry_timestamp_ms=self.now_ts,
        )
        self.assertIsNone(trade)
        self.assertEqual(rej, "MAX_DAILY_LOSS_EXCEEDED")

    def test_target_exit_2r(self):
        trade, rej = self.engine.open_trade(
            symbol="B-BTC_USDT",
            market_type="perpetual",
            detected_at="2026-11-01T10:00:00Z",
            entered_at="2026-11-01T10:00:00Z",
            entry_price=100.0,
            stop_price=98.0,
            entry_timestamp_ms=self.now_ts,
        )
        self.assertIsNotNone(trade)
        self.assertEqual(trade.target_price, 104.0)

        c = Candle(self.now_ts + 15 * 60 * 1000, open=101, high=104.5, low=100.5, close=104.0, volume=100)
        closed = self.engine.update_and_evaluate_exits(c, "B-BTC_USDT")
        self.assertEqual(len(closed), 1)
        ct = closed[0]
        self.assertEqual(ct.exit_reason, "TARGET")
        self.assertEqual(ct.exit_price, 104.0)
        self.assertGreater(ct.net_pnl, 0)
        self.assertEqual(len(self.engine.open_trades), 0)

    def test_conservative_same_candle_resolution(self):
        trade, _ = self.engine.open_trade(
            symbol="B-ETH_USDT",
            market_type="perpetual",
            detected_at="2026-11-01T10:00:00Z",
            entered_at="2026-11-01T10:00:00Z",
            entry_price=100.0,
            stop_price=98.0,
            entry_timestamp_ms=self.now_ts,
        )
        # Giant candle: Low = 97.0 (hits stop), High = 105.0 (hits target)
        c = Candle(self.now_ts + 15 * 60 * 1000, open=100.0, high=105.0, low=97.0, close=101.0, volume=500)
        closed = self.engine.update_and_evaluate_exits(c, "B-ETH_USDT")
        self.assertEqual(len(closed), 1)
        ct = closed[0]
        self.assertEqual(ct.exit_reason, "STOP")
        self.assertEqual(ct.exit_price, 98.0)
        self.assertLess(ct.net_pnl, 0)

    def test_restart_reload_open_trades(self):
        raw_trade = {
            "trade_id": "PT-000042",
            "strategy_version": "momentum_v1",
            "symbol": "B-SOL_USDT",
            "market_type": "perpetual",
            "side": "LONG",
            "detected_at": "2026-11-01T14:32:00Z",
            "entered_at": "2026-11-01T14:47:00Z",
            "entry_price": 150.0,
            "stop_price": 145.0,
            "target_price": 160.0,
            "quantity": 10.0,
            "notional": 1500.0,
            "leverage_simulated": 1,
            "risk_amount": 50.0,
            "status": "OPEN",
        }
        trade_obj = PaperTrade.from_dict(raw_trade)
        self.engine.reload_open_trades([trade_obj])

        self.assertEqual(len(self.engine.open_trades), 1)
        self.assertIn("PT-000042", self.engine.open_trades)
        # Verify trade counter advanced to avoid collision
        self.assertEqual(self.engine.next_trade_id(), "PT-000043")

    def test_correlated_asset_duplicate_prevention(self):
        trade1, rej1 = self.engine.open_trade(
            symbol="B-ETH_USDT",
            market_type="perpetual",
            detected_at="2026-11-01T10:00:00Z",
            entered_at="2026-11-01T10:00:00Z",
            entry_price=2000.0,
            stop_price=1960.0,
            entry_timestamp_ms=self.now_ts,
        )
        self.assertIsNotNone(trade1)
        self.assertIsNone(rej1)

        # Attempting duplicate exposure on ETH (ETHINR or ETHUSDT) must be blocked
        trade2, rej2 = self.engine.open_trade(
            symbol="ETHINR",
            market_type="spot",
            detected_at="2026-11-01T10:00:00Z",
            entered_at="2026-11-01T10:00:00Z",
            entry_price=180000.0,
            stop_price=176000.0,
            entry_timestamp_ms=self.now_ts,
        )
        self.assertIsNone(trade2)
        self.assertEqual(rej2, "CORRELATED_ASSET_ALREADY_OPEN_ETH")

    def test_25_pct_capital_allocation(self):
        # 25% of 20,000 INR is 5,000 INR
        trade, rej = self.engine.open_trade(
            symbol="B-SOL_USDT",
            market_type="perpetual",
            detected_at="2026-11-01T10:00:00Z",
            entered_at="2026-11-01T10:00:00Z",
            entry_price=100.0,
            stop_price=99.0,  # Tight 1% stop would request 10,000 notional without cap
            entry_timestamp_ms=self.now_ts,
        )
        self.assertIsNotNone(trade)
        # Notional must be capped at 25% of capital (<= 5000 INR)
        self.assertLessEqual(trade.notional, 5000.0)

    def test_1_to_2_risk_reward_target(self):
        # Long trade with 2 INR risk per unit
        trade, _ = self.engine.open_trade(
            symbol="B-BTC_USDT",
            market_type="perpetual",
            detected_at="2026-11-01T10:00:00Z",
            entered_at="2026-11-01T10:00:00Z",
            entry_price=100.0,
            stop_price=98.0,
            entry_timestamp_ms=self.now_ts,
        )
        self.assertIsNotNone(trade)
        # Target must be 1:2 R:R (entry + 2 * risk = 100 + 4 = 104)
        self.assertEqual(trade.target_price, 104.0)


if __name__ == "__main__":
    unittest.main()
