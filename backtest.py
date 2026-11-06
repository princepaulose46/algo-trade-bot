"""Historical Replay and Backtesting Module.

Chronological multi-candle replay with conservative execution modeling,
fees, slippage, and Section 10 metric evaluation.
"""

from __future__ import annotations

import argparse
import datetime
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from config_loader import load_config
from market.candles import Candle, CandleCache
from market.universe import UniverseFilter, UniverseFilterConfig
from paper.engine import PaperEngine, PaperTrade
from paper.sizing import SizingConfig
from reporting.summaries import SummaryGenerator
from strategy.scanner import CandidateRecord, MomentumScanner, ScannerConfig
from strategy.signals import SignalEvaluator

logger = logging.getLogger("backtest")


@dataclass
class BacktestResult:
    total_candles_replayed: int
    trade_count: int
    wins: int
    losses: int
    win_rate_pct: float
    gross_pnl: float
    total_costs: float
    net_pnl: float
    net_return_pct: float
    ending_equity: float
    max_drawdown_pct: float
    profit_factor: float
    average_win: float
    average_loss: float
    expectancy: float
    longest_losing_streak: int
    average_holding_hours: float
    candidate_continuation_10pct: float
    candidate_continuation_20pct: float
    candidate_continuation_50pct: float


class BacktestRunner:
    """Historical candle replay engine satisfying Section 10 rules."""

    def __init__(self, config_path: str = "config.yaml") -> None:
        self.config = load_config(config_path)

        p_cfg = self.config.get("paper", {})
        c_cfg = self.config.get("costs", {})
        self.starting_equity = float(p_cfg.get("starting_equity_inr", 20000.0))

        sizing_cfg = SizingConfig(
            starting_equity=self.starting_equity,
            risk_fraction=float(p_cfg.get("risk_fraction", 0.005)),
            max_open_trades=int(p_cfg.get("max_open_trades", 3)),
            max_daily_loss_fraction=float(p_cfg.get("max_daily_loss_fraction", 0.01)),
            max_notional_fraction_equity=float(p_cfg.get("max_notional_fraction_equity", 1.0)),
            fee_rate_per_side=float(c_cfg.get("fee_rate_per_side", 0.001)),
            slippage_bps_per_side=float(c_cfg.get("slippage_bps_per_side", 5.0)),
        )

        self.engine = PaperEngine(
            sizing_config=sizing_cfg,
            target_r_multiple=float(p_cfg.get("target_r_multiple", 2.0)),
            max_holding_hours=int(p_cfg.get("max_holding_hours", 24)),
            trailing_stop_enabled=bool(p_cfg.get("trailing_stop_enabled", True)),
            trailing_stop_activation_r=float(p_cfg.get("trailing_stop_activation_r", 1.0)),
        )

        s_cfg = self.config.get("scanner", {})
        self.scanner_config = ScannerConfig(
            min_change_24h_pct=float(s_cfg.get("min_change_24h_pct", 5.0)),
            min_change_15m_pct=float(s_cfg.get("min_change_15m_pct", 1.0)),
            min_relative_volume=float(s_cfg.get("min_relative_volume", 2.0)),
            rsi_min=float(s_cfg.get("rsi_min", 55.0)),
            require_above_vwap=bool(s_cfg.get("require_above_vwap", True)),
            require_above_ema50=bool(s_cfg.get("require_above_ema50", True)),
            use_open_interest_filter=bool(s_cfg.get("use_open_interest_filter", False)),
            lookback_range_candles=int(s_cfg.get("lookback_range_candles", 20)),
            max_distance_from_ema50_pct=float(s_cfg.get("max_distance_from_ema50_pct", 6.0)),
            cooldown_candles_after_failed=int(s_cfg.get("cooldown_candles_after_failed", 8)),
            use_rolling_24h_calculation=True,
        )
        self.scanner = MomentumScanner(self.scanner_config)
        self.signal_evaluator = SignalEvaluator(self.scanner_config)
        self.candidates: List[CandidateRecord] = []

    def run(self, symbol: str, candles: List[Candle]) -> BacktestResult:
        """Run walk-forward simulation across chronological candle sequence."""
        # Ensure chronological ordering
        candles = sorted(candles, key=lambda c: c.timestamp_ms)
        cache = CandleCache(symbol, interval="15m", max_size=500)

        # Minimum burn-in period for indicators (EMA50 + lookback)
        burn_in = 60
        if len(candles) <= burn_in:
            raise ValueError(f"Need at least {burn_in} candles for backtest, got {len(candles)}")

        # Feed initial history
        for c in candles[:burn_in]:
            cache.add_candle(c)

        # Walk-forward candle by candle
        for i in range(burn_in, len(candles)):
            curr_candle = candles[i]
            cache.add_candle(curr_candle)

            # 1. Update open positions on current candle
            self.engine.update_and_evaluate_exits(curr_candle, symbol)

            # 2. Check initial candidate trigger on completed candles
            history = cache.candles
            quote_vol = sum(c.close * c.volume for c in history[-96:])  # rolling 24h volume
            
            # Calculate 24h change relative to 96 candles ago or earliest candle
            ref_idx = max(0, len(history) - 96)
            ref_open = history[ref_idx].open
            chg_24h = ((curr_candle.close - ref_open) / ref_open) * 100.0 if ref_open > 0 else 0.0

            synthetic_ticker = {
                "market": symbol,
                "last_price": curr_candle.close,
                "change_24_hour": round(chg_24h, 2),
                "quote_volume": quote_vol,
                "bid": curr_candle.close * 0.9995,
                "ask": curr_candle.close * 1.0005,
            }
            spread_pct = 0.10

            triggered, trigger_metrics, _ = self.scanner.check_initial_trigger(
                symbol=symbol,
                ticker=synthetic_ticker,
                candles=history,
                spread_pct=spread_pct,
                quote_volume=quote_vol,
                min_quote_volume=10000.0,
                max_spread_pct=0.20,
            )

            if triggered:
                # Section 5.3 & 5.4 Continuation & anti-chase evaluation
                sig = self.signal_evaluator.evaluate_long_signal(
                    symbol=symbol,
                    candles=history,
                    derivatives_context={"available": False},
                    spread_pct=spread_pct,
                )

                cid = self.scanner.next_candidate_id()
                candidate = CandidateRecord(
                    candidate_id=cid,
                    symbol=symbol,
                    detected_at=curr_candle.iso_time,
                    change_24h_pct=trigger_metrics.get("change_24h_pct", 0.0),
                    change_15m_pct=trigger_metrics.get("change_15m_pct", 0.0),
                    relative_volume=trigger_metrics.get("relative_volume", 0.0),
                    spread_pct=spread_pct,
                    vwap_distance_pct=sig.metrics.get("vwap_distance_pct", 0.0) if sig.metrics else 0.0,
                    rsi_15m=sig.metrics.get("rsi", 0.0) if sig.metrics else 0.0,
                    decision="ENTERED" if sig.confirmed else "FILTERED",
                    reason=sig.reason,
                    entry_reference_price=curr_candle.close,
                    detected_timestamp_ms=curr_candle.timestamp_ms,
                    calculation_method="rolling_24h_candles",
                )

                if sig.confirmed:
                    trade, rej = self.engine.open_trade(
                        symbol=symbol,
                        market_type="perpetual",
                        detected_at=curr_candle.iso_time,
                        entered_at=curr_candle.iso_time,
                        entry_price=sig.entry_price,
                        stop_price=sig.stop_price,
                        entry_timestamp_ms=curr_candle.timestamp_ms,
                        strategy_version=sig.signal_type,
                        side=sig.side,
                    )
                    if not trade:
                        candidate.decision = "FILTERED"
                        candidate.reason = rej

                # Track future candles for candidate outcome evaluation
                future_slice = candles[i + 1 : i + 97]
                self.scanner.update_candidate_outcomes(candidate, future_slice)
                self.candidates.append(candidate)

        # Force close remaining open trades at last candle close
        last_candle = candles[-1]
        for tid, t in list(self.engine.open_trades.items()):
            self.engine._finalize_exit(t, last_candle.close, last_candle.iso_time, "BACKTEST_END")
            del self.engine.open_trades[tid]
            self.engine.closed_trades.append(t)

        return self._compute_statistics(len(candles))

    def _compute_statistics(self, total_candles: int) -> BacktestResult:
        closed = self.engine.closed_trades
        trade_count = len(closed)

        wins = [t for t in closed if (t.net_pnl or 0) > 0]
        losses = [t for t in closed if (t.net_pnl or 0) <= 0]
        win_count = len(wins)
        loss_count = len(losses)
        win_rate = (win_count / trade_count * 100.0) if trade_count > 0 else 0.0

        gross_pnl = sum(t.gross_pnl or 0.0 for t in closed)
        total_costs = sum((t.fees_estimate or 0.0) + (t.slippage_estimate or 0.0) for t in closed)
        net_pnl = sum(t.net_pnl or 0.0 for t in closed)
        net_return_pct = (net_pnl / self.starting_equity) * 100.0

        avg_win = (sum(t.net_pnl or 0.0 for t in wins) / win_count) if win_count > 0 else 0.0
        avg_loss = (sum(abs(t.net_pnl or 0.0) for t in losses) / loss_count) if loss_count > 0 else 0.0

        tot_win = sum(t.net_pnl or 0.0 for t in wins)
        tot_loss = sum(abs(t.net_pnl or 0.0) for t in losses)
        profit_factor = (tot_win / tot_loss) if tot_loss > 0 else (999.0 if tot_win > 0 else 0.0)

        # Expectancy: (Win% * AvgWin) - (Loss% * AvgLoss)
        p_win = win_count / trade_count if trade_count > 0 else 0.0
        p_loss = loss_count / trade_count if trade_count > 0 else 0.0
        expectancy = (p_win * avg_win) - (p_loss * avg_loss)

        # Longest losing streak
        longest_loss_streak = 0
        current_loss_streak = 0
        for t in closed:
            if (t.net_pnl or 0.0) <= 0:
                current_loss_streak += 1
                longest_loss_streak = max(longest_loss_streak, current_loss_streak)
            else:
                current_loss_streak = 0

        # Holding duration
        holding_times = []
        for t in closed:
            if t.exit_at and t.entered_at:
                try:
                    dt_in = datetime.datetime.fromisoformat(t.entered_at.replace("Z", "+00:00"))
                    dt_out = datetime.datetime.fromisoformat(t.exit_at.replace("Z", "+00:00"))
                    hours = (dt_out - dt_in).total_seconds() / 3600.0
                    holding_times.append(hours)
                except Exception:
                    pass
        avg_holding = (sum(holding_times) / len(holding_times)) if holding_times else 0.0

        # Max drawdown
        peak = self.starting_equity
        max_dd = 0.0
        running = self.starting_equity
        for t in closed:
            running += t.net_pnl or 0.0
            if running > peak:
                peak = running
            dd = ((peak - running) / peak) * 100.0 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd

        # Candidate continuation rates
        tot_cands = len(self.candidates)
        c_10 = sum(1 for c in self.candidates if c.reached_10pct)
        c_20 = sum(1 for c in self.candidates if c.reached_20pct)
        c_50 = sum(1 for c in self.candidates if c.reached_50pct)

        return BacktestResult(
            total_candles_replayed=total_candles,
            trade_count=trade_count,
            wins=win_count,
            losses=loss_count,
            win_rate_pct=round(win_rate, 2),
            gross_pnl=round(gross_pnl, 2),
            total_costs=round(total_costs, 2),
            net_pnl=round(net_pnl, 2),
            net_return_pct=round(net_return_pct, 2),
            ending_equity=round(self.engine.current_equity, 2),
            max_drawdown_pct=round(max_dd, 2),
            profit_factor=round(profit_factor, 2),
            average_win=round(avg_win, 2),
            average_loss=round(avg_loss, 2),
            expectancy=round(expectancy, 2),
            longest_losing_streak=longest_loss_streak,
            average_holding_hours=round(avg_holding, 2),
            candidate_continuation_10pct=round((c_10 / tot_cands * 100.0), 2) if tot_cands > 0 else 0.0,
            candidate_continuation_20pct=round((c_20 / tot_cands * 100.0), 2) if tot_cands > 0 else 0.0,
            candidate_continuation_50pct=round((c_50 / tot_cands * 100.0), 2) if tot_cands > 0 else 0.0,
        )
