"""Unit tests for atomic JSON store, recovery, and summary reconciliation."""

import json
import os
import shutil
import tempfile
import unittest

from reporting.json_store import SafeJsonStore
from reporting.summaries import SummaryGenerator


class TestJsonStoreAndSummaries(unittest.TestCase):

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.store = SafeJsonStore(base_dir=self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_atomic_write_and_read_json(self):
        data = {"hello": "world", "number": 42}
        self.store.atomic_write_json("test.json", data)

        loaded = self.store.read_json_safely("test.json")
        self.assertEqual(loaded, data)

    def test_append_and_recover_corrupted_jsonl(self):
        filename = "audit.jsonl"
        r1 = {"id": 1, "status": "OPEN"}
        r2 = {"id": 2, "status": "CLOSED"}

        self.store.append_jsonl(filename, r1)
        self.store.append_jsonl(filename, r2)

        # Intentionally inject a corrupted truncated line at the end
        filepath = os.path.join(self.test_dir, filename)
        with open(filepath, "a", encoding="utf-8") as f:
            f.write('{"id": 3, "status": "BROKEN')  # Incomplete JSON line

        # Read safely should recover valid lines and auto-repair
        recovered = self.store.read_jsonl_safely(filename)
        self.assertEqual(len(recovered), 2)
        self.assertEqual(recovered[0]["id"], 1)
        self.assertEqual(recovered[1]["id"], 2)

        # File should now be clean
        re_read = self.store.read_jsonl_safely(filename)
        self.assertEqual(len(re_read), 2)

    def test_deduplication_helper(self):
        # symbol, timestamp, event_type
        self.assertFalse(self.store.is_duplicate_event("BTCUSDT", 1000, "scan"))
        # Second time should return True (is duplicate)
        self.assertTrue(self.store.is_duplicate_event("BTCUSDT", 1000, "scan"))
        # Different timestamp -> False
        self.assertFalse(self.store.is_duplicate_event("BTCUSDT", 2000, "scan"))

    def test_daily_summary_reconciliation(self):
        candidates = [
            {"candidate_id": "C-1", "detected_at": "2026-11-01T10:00:00Z", "decision": "ENTERED", "reached_10pct": True, "reached_20pct": False, "reached_50pct": False},
            {"candidate_id": "C-2", "detected_at": "2026-11-01T11:00:00Z", "decision": "FILTERED", "reached_10pct": False, "reached_20pct": False, "reached_50pct": False},
            {"candidate_id": "C-3", "detected_at": "2026-11-01T12:00:00Z", "decision": "ENTERED", "reached_10pct": True, "reached_20pct": True, "reached_50pct": False},
        ]

        trades = [
            {
                "trade_id": "PT-1",
                "symbol": "B-BTC_USDT",
                "status": "CLOSED",
                "exit_at": "2026-11-01T14:00:00Z",
                "gross_pnl": 100.0,
                "fees_estimate": 10.0,
                "slippage_estimate": 5.0,
                "net_pnl": 85.0,
            },
            {
                "trade_id": "PT-2",
                "symbol": "B-ETH_USDT",
                "status": "CLOSED",
                "exit_at": "2026-11-01T15:00:00Z",
                "gross_pnl": -50.0,
                "fees_estimate": 10.0,
                "slippage_estimate": 5.0,
                "net_pnl": -65.0,
            },
            {
                "trade_id": "PT-3",
                "symbol": "B-SOL_USDT",
                "status": "OPEN",
                "exit_at": None,
                "gross_pnl": None,
                "net_pnl": None,
            },
        ]

        summary = SummaryGenerator.generate_daily_summary(
            date_utc="2026-11-01",
            markets_checked=50,
            candidates=candidates,
            trades=trades,
            ending_equity=20020.0,
            starting_equity=20000.0,
        )

        # Check scanner reconciliation
        self.assertEqual(summary["scanner"]["markets_checked"], 50)
        self.assertEqual(summary["scanner"]["candidates_detected"], 3)
        self.assertEqual(summary["scanner"]["entered_paper_trades"], 2)
        self.assertEqual(summary["scanner"]["filtered_candidates"], 1)

        # Check trading reconciliation
        self.assertEqual(summary["trading"]["closed_trades"], 2)
        self.assertEqual(summary["trading"]["open_trades"], 1)
        self.assertEqual(summary["trading"]["wins"], 1)
        self.assertEqual(summary["trading"]["losses"], 1)
        self.assertEqual(summary["trading"]["win_rate_pct"], 50.0)
        self.assertEqual(summary["trading"]["gross_pnl"], 50.0)
        self.assertEqual(summary["trading"]["fees_and_slippage"], 30.0)
        self.assertEqual(summary["trading"]["net_pnl"], 20.0)
        self.assertEqual(summary["trading"]["ending_equity"], 20020.0)

        # Check candidate outcomes
        self.assertEqual(summary["candidate_outcomes"]["reached_10pct"], 2)
        self.assertEqual(summary["candidate_outcomes"]["reached_20pct"], 1)
        self.assertEqual(summary["candidate_outcomes"]["reached_50pct"], 0)

    def test_weekly_summary_metrics(self):
        candidates = [
            {"reached_10pct": True, "reached_20pct": True, "reached_50pct": False},
            {"reached_10pct": False, "reached_20pct": False, "reached_50pct": False},
        ]
        trades = [
            {"symbol": "BTC", "status": "CLOSED", "strategy_version": "v1", "net_pnl": 100.0, "gross_pnl": 110.0, "fees_estimate": 5.0, "slippage_estimate": 5.0},
            {"symbol": "BTC", "status": "CLOSED", "strategy_version": "v1", "net_pnl": 50.0, "gross_pnl": 60.0, "fees_estimate": 5.0, "slippage_estimate": 5.0},
            {"symbol": "ETH", "status": "CLOSED", "strategy_version": "v1", "net_pnl": -30.0, "gross_pnl": -20.0, "fees_estimate": 5.0, "slippage_estimate": 5.0},
        ]

        weekly = SummaryGenerator.generate_weekly_summary(trades, candidates)
        self.assertEqual(weekly["sample_size"], 3)
        self.assertEqual(weekly["win_rate_pct"], 66.67)
        self.assertEqual(weekly["profit_factor"], 5.0)  # 150 / 30 = 5.0
        self.assertEqual(weekly["candidate_continuation"]["rate_10pct"], 50.0)
        self.assertEqual(weekly["candidate_continuation"]["rate_20pct"], 50.0)
        self.assertEqual(weekly["candidate_continuation"]["rate_50pct"], 0.0)


if __name__ == "__main__":
    unittest.main()

