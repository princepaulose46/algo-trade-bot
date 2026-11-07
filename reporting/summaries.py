"""Daily and weekly performance summaries and reconciliation engine."""

from __future__ import annotations

import datetime
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger("reporting.summaries")


class SummaryGenerator:
    """Generates reconciled daily and weekly performance reports from raw records."""

    @staticmethod
    def generate_daily_summary(
        date_utc: str,
        markets_checked: int,
        candidates: List[Dict[str, Any]],
        trades: List[Dict[str, Any]],
        ending_equity: float,
        starting_equity: float = 20000.0,
    ) -> Dict[str, Any]:
        """Generate daily summary matching Section 9 schema.

        Reconciles directly from raw candidate and trade records.
        """
        # Filter candidates detected on date_utc
        daily_candidates = [
            c for c in candidates if c.get("detected_at", "").startswith(date_utc)
        ]
        candidates_detected = len(daily_candidates)
        entered_candidates = sum(
            1 for c in daily_candidates if c.get("decision") == "ENTERED"
        )
        filtered_candidates = sum(
            1 for c in daily_candidates if c.get("decision") != "ENTERED"
        )

        # Candidate outcomes across all daily candidates
        reached_10pct = sum(1 for c in daily_candidates if c.get("reached_10pct") is True)
        reached_20pct = sum(1 for c in daily_candidates if c.get("reached_20pct") is True)
        reached_50pct = sum(1 for c in daily_candidates if c.get("reached_50pct") is True)

        # Trading metrics (deduplicated by trade_id to obtain true current state)
        latest_trades_map: Dict[str, Dict[str, Any]] = {}
        for idx, t in enumerate(trades):
            tid = t.get("trade_id") or f"anon_{idx}"
            latest_trades_map[tid] = t

        open_trades_count = sum(1 for t in latest_trades_map.values() if t.get("status") == "OPEN")

        # Closed trades exited on date_utc or overall
        daily_closed_trades = [
            t
            for t in latest_trades_map.values()
            if t.get("status") == "CLOSED"
            and (t.get("exit_at") or "").startswith(date_utc)
        ]

        closed_count = len(daily_closed_trades)
        wins = sum(1 for t in daily_closed_trades if (t.get("net_pnl") or 0.0) > 0)
        losses = sum(1 for t in daily_closed_trades if (t.get("net_pnl") or 0.0) <= 0)
        win_rate = (wins / closed_count * 100.0) if closed_count > 0 else 0.0

        gross_pnl = sum(float(t.get("gross_pnl") or 0.0) for t in daily_closed_trades)
        fees = sum(float(t.get("fees_estimate") or 0.0) for t in daily_closed_trades)
        slippage = sum(float(t.get("slippage_estimate") or 0.0) for t in daily_closed_trades)
        net_pnl = sum(float(t.get("net_pnl") or 0.0) for t in daily_closed_trades)

        # Max drawdown calculation
        peak = starting_equity
        max_dd_pct = 0.0
        running_equity = starting_equity
        for t in trades:
            if t.get("status") == "CLOSED":
                running_equity += float(t.get("net_pnl") or 0.0)
                if running_equity > peak:
                    peak = running_equity
                dd = ((peak - running_equity) / peak) * 100.0 if peak > 0 else 0.0
                if dd > max_dd_pct:
                    max_dd_pct = dd

        summary = {
            "date_utc": date_utc,
            "scanner": {
                "markets_checked": markets_checked,
                "candidates_detected": candidates_detected,
                "entered_paper_trades": entered_candidates,
                "filtered_candidates": filtered_candidates,
            },
            "trading": {
                "closed_trades": closed_count,
                "open_trades": open_trades_count,
                "wins": wins,
                "losses": losses,
                "win_rate_pct": round(win_rate, 2),
                "gross_pnl": round(gross_pnl, 2),
                "fees_and_slippage": round(fees + slippage, 2),
                "net_pnl": round(net_pnl, 2),
                "ending_equity": round(ending_equity, 2),
                "max_drawdown_pct": round(max_dd_pct, 2),
            },
            "candidate_outcomes": {
                "reached_10pct": reached_10pct,
                "reached_20pct": reached_20pct,
                "reached_50pct": reached_50pct,
            },
        }
        return summary

    @staticmethod
    def generate_weekly_summary(
        trades: List[Dict[str, Any]],
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """Generate weekly summary analyzing performance by symbol, signal variant, and continuation."""
        latest_trades_map: Dict[str, Dict[str, Any]] = {}
        for idx, t in enumerate(trades):
            tid = t.get("trade_id") or f"anon_{idx}"
            latest_trades_map[tid] = t

        closed = [t for t in latest_trades_map.values() if t.get("status") == "CLOSED"]
        total_closed = len(closed)

        total_gross = sum(float(t.get("gross_pnl") or 0.0) for t in closed)
        total_costs = sum(
            float(t.get("fees_estimate") or 0.0) + float(t.get("slippage_estimate") or 0.0)
            for t in closed
        )
        total_net = sum(float(t.get("net_pnl") or 0.0) for t in closed)

        win_trades = [t for t in closed if float(t.get("net_pnl") or 0.0) > 0]
        loss_trades = [t for t in closed if float(t.get("net_pnl") or 0.0) <= 0]

        total_profit = sum(float(t.get("net_pnl") or 0.0) for t in win_trades)
        total_loss = abs(sum(float(t.get("net_pnl") or 0.0) for t in loss_trades))
        profit_factor = (total_profit / total_loss) if total_loss > 0 else (999.0 if total_profit > 0 else 0.0)

        # Breakdown by symbol
        by_symbol: Dict[str, Dict[str, Any]] = {}
        for t in closed:
            sym = t["symbol"]
            pnl = float(t.get("net_pnl") or 0.0)
            if sym not in by_symbol:
                by_symbol[sym] = {"trades": 0, "wins": 0, "net_pnl": 0.0}
            by_symbol[sym]["trades"] += 1
            if pnl > 0:
                by_symbol[sym]["wins"] += 1
            by_symbol[sym]["net_pnl"] += pnl

        # Breakdown by signal variant
        by_variant: Dict[str, Dict[str, Any]] = {}
        for t in closed:
            variant = t.get("strategy_version", "default")
            pnl = float(t.get("net_pnl") or 0.0)
            if variant not in by_variant:
                by_variant[variant] = {"trades": 0, "wins": 0, "net_pnl": 0.0}
            by_variant[variant]["trades"] += 1
            if pnl > 0:
                by_variant[variant]["wins"] += 1
            by_variant[variant]["net_pnl"] += pnl

        # Continuation rates across all candidates
        total_candidates = len(candidates)
        reached_10 = sum(1 for c in candidates if c.get("reached_10pct"))
        reached_20 = sum(1 for c in candidates if c.get("reached_20pct"))
        reached_50 = sum(1 for c in candidates if c.get("reached_50pct"))

        return {
            "sample_size": total_closed,
            "total_candidates_tracked": total_candidates,
            "win_rate_pct": round((len(win_trades) / total_closed * 100.0), 2) if total_closed > 0 else 0.0,
            "profit_factor": round(profit_factor, 2),
            "gross_pnl": round(total_gross, 2),
            "total_costs": round(total_costs, 2),
            "net_pnl": round(total_net, 2),
            "performance_by_symbol": by_symbol,
            "performance_by_variant": by_variant,
            "candidate_continuation": {
                "rate_10pct": round(reached_10 / total_candidates * 100.0, 2) if total_candidates > 0 else 0.0,
                "rate_20pct": round(reached_20 / total_candidates * 100.0, 2) if total_candidates > 0 else 0.0,
                "rate_50pct": round(reached_50 / total_candidates * 100.0, 2) if total_candidates > 0 else 0.0,
            },
        }

