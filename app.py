"""CoinDCX Momentum Scanner & Paper-Trading Bot.

Read-only exchange integration, momentum scanner, simulated paper execution,
and crash-safe JSON/JSONL reporting.
"""

from __future__ import annotations

import argparse
import datetime
import logging
import os
import signal
import sys
import time
from typing import Any, Dict, List, Optional

from coindcx.client import CoinDCXClient
from coindcx.symbols import MarketMetadata, SymbolRegistry
from config_loader import load_config
from market.candles import Candle, CandleCache
from market.universe import ExclusionRecord, UniverseFilter, UniverseFilterConfig
from paper.engine import PaperEngine, PaperTrade
from paper.sizing import SizingConfig
from reporting.json_store import SafeJsonStore
from reporting.summaries import SummaryGenerator
from strategy.scanner import CandidateRecord, MomentumScanner, ScannerConfig
from strategy.signals import SignalEvaluator, SignalResult


def setup_logging(log_level: str = "INFO", log_file: str = "logs/bot.log") -> logging.Logger:
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    numeric_level = getattr(logging, log_level.upper(), logging.INFO)

    root_logger = logging.getLogger()
    root_logger.setLevel(numeric_level)
    root_logger.handlers.clear()

    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    root_logger.addHandler(console_handler)

    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    return root_logger


class MomentumBot:
    """Core coordinator for CoinDCX Momentum Scanner and Paper-Trading Bot."""

    def __init__(self, config_path: str = "config.yaml") -> None:
        self.config_raw = load_config(config_path)
        self.is_running = False

        # Config extraction
        app_cfg = self.config_raw.get("app", {})
        self.scan_interval = int(app_cfg.get("scan_interval_seconds", 30))
        self.timezone = app_cfg.get("timezone", "UTC")

        # Reporting setup
        rep_cfg = self.config_raw.get("reports", {})
        self.reports_dir = rep_cfg.get("directory", "./reports")
        self.candidates_file = rep_cfg.get("candidates_file", "candidates.jsonl")
        self.trades_file = rep_cfg.get("trades_file", "trades.jsonl")
        self.daily_summary_file = rep_cfg.get("daily_summary_file", "daily_summary.json")

        self.store = SafeJsonStore(self.reports_dir)

        # CoinDCX Client
        self.client = CoinDCXClient()

        # Universe Filter
        u_cfg = self.config_raw.get("universe", {})
        self.universe_config = UniverseFilterConfig(
            market_type=u_cfg.get("market_type", "perpetual"),
            min_quote_volume_24h=float(u_cfg.get("min_quote_volume_24h", 100000)),
            max_spread_pct=float(u_cfg.get("max_spread_pct", 0.20)),
            symbols_allowlist=u_cfg.get("symbols_allowlist", []),
            symbols_blocklist=u_cfg.get("symbols_blocklist", []),
            min_history_candles=int(u_cfg.get("min_history_candles", 60)),
        )
        self.universe_filter = UniverseFilter(self.universe_config)

        # Scanner
        s_cfg = self.config_raw.get("scanner", {})
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
        )
        self.scanner = MomentumScanner(self.scanner_config)
        self.signal_evaluator = SignalEvaluator(self.scanner_config)

        # Paper Engine & Sizing
        p_cfg = self.config_raw.get("paper", {})
        c_cfg = self.config_raw.get("costs", {})
        self.sizing_config = SizingConfig(
            starting_equity=float(p_cfg.get("starting_equity_inr", 20000.0)),
            risk_fraction=float(p_cfg.get("risk_fraction", 0.005)),
            max_open_trades=int(p_cfg.get("max_open_trades", 3)),
            max_daily_loss_fraction=float(p_cfg.get("max_daily_loss_fraction", 0.01)),
            max_notional_fraction_equity=float(p_cfg.get("max_notional_fraction_equity", 1.0)),
            fee_rate_per_side=float(c_cfg.get("fee_rate_per_side", 0.001)),
            slippage_bps_per_side=float(c_cfg.get("slippage_bps_per_side", 5.0)),
        )
        self.engine = PaperEngine(
            sizing_config=self.sizing_config,
            target_r_multiple=float(p_cfg.get("target_r_multiple", 2.0)),
            max_holding_hours=int(p_cfg.get("max_holding_hours", 24)),
            trailing_stop_enabled=bool(p_cfg.get("trailing_stop_enabled", True)),
            trailing_stop_activation_r=float(p_cfg.get("trailing_stop_activation_r", 1.0)),
        )

        # Candle caches per symbol
        self.candle_caches: Dict[str, CandleCache] = {}
        self.all_candidates: List[CandidateRecord] = []
        self.markets_checked_count: int = 0

        self._recover_and_initialize()

    def _recover_and_initialize(self) -> None:
        """Section 11: Validate existing reports on startup, recover state."""
        logging.info("Validating and recovering existing state from %s", self.reports_dir)

        # 1. Recover existing trades
        raw_trades = self.store.read_jsonl_safely(self.trades_file)
        recovered_trades: List[PaperTrade] = []
        for item in raw_trades:
            try:
                trade = PaperTrade.from_dict(item)
                recovered_trades.append(trade)
            except Exception as exc:
                logging.warning("Skipping malformed trade record: %s (%s)", item, exc)

        self.engine.reload_open_trades(recovered_trades)
        logging.info(
            "Recovered %d total trades (%d currently open)",
            len(recovered_trades),
            len(self.engine.open_trades),
        )

        # 2. Recover candidates
        raw_candidates = self.store.read_jsonl_safely(self.candidates_file)
        max_candidate_id = 0
        for item in raw_candidates:
            cid = item.get("candidate_id", "")
            try:
                num = int(cid.replace("C-", ""))
                max_candidate_id = max(max_candidate_id, num)
            except ValueError:
                pass
        self.scanner.set_candidate_counter(max_candidate_id)
        logging.info("Recovered %d candidates. Next candidate ID counter: %d", len(raw_candidates), max_candidate_id + 1)

    def scan_iteration(self, mock_tickers: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Perform one scan cycle across the market universe."""
        now_dt = datetime.datetime.now(datetime.timezone.utc)
        today_utc = now_dt.strftime("%Y-%m-%d")
        now_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        now_ms = int(now_dt.timestamp() * 1000)

        # 1. Fetch market data
        tickers: List[Dict[str, Any]] = []
        if mock_tickers is not None:
            tickers = mock_tickers
        else:
            try:
                tickers = self.client.get_tickers()
            except Exception as exc:
                logging.error("Failed to fetch tickers: %s. Continuing with futures fallback.", exc)
                tickers = []

            # If perpetual or all, also load dedicated CoinDCX perpetual futures instruments
            if self.universe_config.market_type.lower() in ("perpetual", "all"):
                try:
                    futures_instruments = self.client.get_futures_active_instruments()
                    for inst in futures_instruments:
                        sym = inst.get("symbol") or inst.get("pair")
                        if sym and not any((t.get("market") == sym or t.get("symbol") == sym) for t in tickers):
                            tickers.append({
                                "market": sym,
                                "symbol": sym,
                                "last_price": 0.0,
                                "change_24_hour": 0.0,
                                "volume": 0.0,
                                "bid": 0.0,
                                "ask": 0.0,
                                "is_futures": True,
                            })
                    logging.info("Included %d perpetual futures instruments.", len(futures_instruments))
                except Exception as exc:
                    logging.warning("Failed to fetch futures active instruments: %s", exc)

        # Synthesize registry from tickers / market details
        registry = SymbolRegistry.from_tickers(tickers)
        self.markets_checked_count = len(tickers)

        # 2. Universe filtering (Section 5.1)
        eligible_symbols, exclusions = self.universe_filter.filter_universe(tickers, registry)
        logging.info("Universe filter: %d eligible, %d excluded", len(eligible_symbols), len(exclusions))

        ticker_map = {t.get("market") or t.get("symbol"): t for t in tickers}
        new_candidates: List[CandidateRecord] = []
        new_trades: List[PaperTrade] = []

        # 3. Evaluate each eligible symbol
        for symbol in eligible_symbols:
            ticker = ticker_map.get(symbol)
            if not ticker:
                continue

            # Ingest candles
            cache = self.candle_caches.get(symbol)
            if not cache:
                cache = CandleCache(symbol, interval="15m")
                self.candle_caches[symbol] = cache

            # Fetch fresh candles from exchange unless mocked
            if mock_tickers is None:
                try:
                    raw_candles = self.client.get_candles(symbol, interval="15m", limit=100)
                    cache.ingest_raw_candles(raw_candles, exclude_unfinished=True)
                except Exception as exc:
                    logging.warning("Error fetching candles for %s: %s", symbol, exc)
                    continue

            candles = cache.candles
            if not candles or len(candles) < self.universe_config.min_history_candles:
                continue

            # Stale data check (Section 11)
            if cache.is_stale(max_allowed_gap_minutes=45):
                logging.warning("Data for %s is stale. Skipping new entries safely.", symbol)
                continue

            latest_candle = candles[-1]

            # Populate price and rolling 24h metrics from candles if ticker field is missing
            if float(ticker.get("last_price") or 0.0) <= 0.0:
                ticker["last_price"] = latest_candle.close

            if float(ticker.get("change_24_hour") or 0.0) == 0.0 and len(candles) >= 10:
                ref_idx = max(0, len(candles) - 96)
                ref_price = candles[ref_idx].open
                if ref_price > 0:
                    ticker["change_24_hour"] = round(((latest_candle.close - ref_price) / ref_price) * 100.0, 2)

            # 4. If symbol already has an open paper trade, evaluate exits first!
            closed_trades = self.engine.update_and_evaluate_exits(latest_candle, symbol)
            for ct in closed_trades:
                # Save updated closed trade to log
                self.store.append_jsonl(self.trades_file, ct.to_dict())
                # If trade closed on stop, set cooldown (Section 5.4)
                if ct.exit_reason in ("STOP", "TRAILING_STOP"):
                    self.signal_evaluator.set_cooldown(symbol, latest_candle.timestamp_ms)

            # Prevent duplicate processing of the same completed candle
            if self.store.is_duplicate_event(symbol, latest_candle.timestamp_ms, "candle_scan"):
                continue

            # Compute spread
            bid = float(ticker.get("bid") or 0.0)
            ask = float(ticker.get("ask") or 0.0)
            spread_pct = ((ask - bid) / ask * 100.0) if ask > 0 else 0.0
            last_price = float(ticker.get("last_price") or 0.0)
            volume = float(ticker.get("volume") or 0.0)
            quote_vol = float(ticker.get("quote_volume") or (volume * last_price))

            # 5. Check Initial Candidate Trigger (Section 5.2)
            triggered, trigger_metrics, trigger_fail_reason = self.scanner.check_initial_trigger(
                symbol=symbol,
                ticker=ticker,
                candles=candles,
                spread_pct=spread_pct,
                quote_volume=quote_vol,
                min_quote_volume=self.universe_config.min_quote_volume_24h,
                max_spread_pct=self.universe_config.max_spread_pct,
            )

            if not triggered:
                continue

            # Section 7: "Log every qualifying candidate, including filtered and failed candidates.
            # Recording winners only would create severe selection bias."
            derivatives_ctx = self.client.get_derivatives_context(symbol)

            # 6. Evaluate Continuation Confirmation & Anti-chase (Section 5.3 & 5.4)
            signal_res = self.signal_evaluator.evaluate_long_signal(
                symbol=symbol,
                candles=candles,
                derivatives_context=derivatives_ctx,
                spread_pct=spread_pct,
            )

            candidate_id = self.scanner.next_candidate_id()
            decision = "ENTERED" if signal_res.confirmed else "FILTERED"
            reason = signal_res.reason

            # Build candidate record
            candidate = CandidateRecord(
                candidate_id=candidate_id,
                symbol=symbol,
                detected_at=now_iso,
                change_24h_pct=trigger_metrics.get("change_24h_pct", 0.0),
                change_15m_pct=trigger_metrics.get("change_15m_pct", 0.0),
                relative_volume=trigger_metrics.get("relative_volume", 0.0),
                spread_pct=spread_pct,
                vwap_distance_pct=signal_res.metrics.get("vwap_distance_pct", 0.0) if signal_res.metrics else 0.0,
                rsi_15m=signal_res.metrics.get("rsi", 0.0) if signal_res.metrics else 0.0,
                open_interest_change_pct=derivatives_ctx.get("open_interest_change_pct"),
                decision=decision,
                reason=reason,
                market_type=self.universe_config.market_type,
                entry_reference_price=latest_candle.close,
                detected_timestamp_ms=now_ms,
                calculation_method=trigger_metrics.get("calculation_method", "ticker_field"),
            )

            # 7. Paper Trade execution if signal confirmed
            if signal_res.confirmed:
                meta = registry.get(symbol)
                min_qty = meta.min_quantity if meta else 0.0001
                step_size = meta.step_size if meta else 0.0001

                trade, rej_reason = self.engine.open_trade(
                    symbol=symbol,
                    market_type=self.universe_config.market_type,
                    detected_at=now_iso,
                    entered_at=now_iso,
                    entry_price=signal_res.entry_price,
                    stop_price=signal_res.stop_price,
                    entry_timestamp_ms=now_ms,
                    strategy_version=signal_res.signal_type,
                    market_min_qty=min_qty,
                    qty_step=step_size,
                )

                if trade:
                    new_trades.append(trade)
                    self.store.append_jsonl(self.trades_file, trade.to_dict())
                else:
                    # Guardrail rejected entry (e.g. max open trades, max daily loss)
                    candidate.decision = "FILTERED"
                    candidate.reason = rej_reason

            new_candidates.append(candidate)
            self.all_candidates.append(candidate)
            self.store.append_jsonl(self.candidates_file, candidate.to_dict())

        # 8. Update future outcomes for active candidates
        for cand in self.all_candidates:
            c_cache = self.candle_caches.get(cand.symbol)
            if c_cache:
                future_c = [c for c in c_cache.candles if c.timestamp_ms > cand.detected_timestamp_ms]
                if future_c:
                    self.scanner.update_candidate_outcomes(cand, future_c)

        # 9. Generate Daily Summary (Section 9)
        all_raw_trades = self.store.read_jsonl_safely(self.trades_file)
        all_raw_candidates = self.store.read_jsonl_safely(self.candidates_file)

        daily_summary = SummaryGenerator.generate_daily_summary(
            date_utc=today_utc,
            markets_checked=self.markets_checked_count,
            candidates=all_raw_candidates,
            trades=all_raw_trades,
            ending_equity=self.engine.current_equity,
            starting_equity=self.sizing_config.starting_equity,
        )
        self.store.atomic_write_json(self.daily_summary_file, daily_summary)

        return {
            "status": "ok",
            "date_utc": today_utc,
            "markets_checked": self.markets_checked_count,
            "eligible_markets": len(eligible_symbols),
            "new_candidates": len(new_candidates),
            "new_trades": len(new_trades),
            "open_trades": len(self.engine.open_trades),
            "equity": round(self.engine.current_equity, 2),
        }

    def run_continuous(self) -> None:
        """Section 13 Milestone 5: Live observation loop without real orders."""
        self.is_running = True
        logging.info("Starting CoinDCX Momentum Paper Bot (Scan interval: %ds)", self.scan_interval)

        def handle_signal(sig, frame):
            logging.info("Shutdown signal (%d) received. Stopping cleanly...", sig)
            self.is_running = False

        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)

        while self.is_running:
            start_time = time.time()
            try:
                res = self.scan_iteration()
                logging.info(
                    "Iteration: Checked %d markets | %d candidates | %d open trades | Equity: %.2f INR",
                    res.get("markets_checked", 0),
                    res.get("new_candidates", 0),
                    res.get("open_trades", 0),
                    res.get("equity", 0.0),
                )
            except Exception as exc:
                logging.error("Unexpected error during scan cycle: %s", exc, exc_info=True)

            elapsed = time.time() - start_time
            sleep_sec = max(1.0, self.scan_interval - elapsed)

            # Sleep in small increments to allow rapid graceful exit on SIGINT
            steps = int(sleep_sec / 0.5)
            for _ in range(steps):
                if not self.is_running:
                    break
                time.sleep(0.5)

        logging.info("Bot exited gracefully. Reports saved in %s", self.reports_dir)

    def print_summary_report(self) -> None:
        """Print daily and weekly performance summaries."""
        all_raw_trades = self.store.read_jsonl_safely(self.trades_file)
        all_raw_candidates = self.store.read_jsonl_safely(self.candidates_file)
        daily_json = self.store.read_json_safely(self.daily_summary_file, default={})
        weekly = SummaryGenerator.generate_weekly_summary(all_raw_trades, all_raw_candidates)

        print("\n" + "=" * 60)
        print("          COINDCX MOMENTUM BOT AUDIT REPORT")
        print("=" * 60)
        print(f"Base Directory: {self.reports_dir}")
        print(f"Total Candidates Tracked: {len(all_raw_candidates)}")
        print(f"Total Trades Recorded:    {len(all_raw_trades)}")
        print(f"Current Paper Equity:     {self.engine.current_equity:.2f} INR")
        print("-" * 60)
        print("DAILY SUMMARY (from daily_summary.json):")
        import json
        print(json.dumps(daily_json, indent=2))
        print("-" * 60)
        print("WEEKLY / OVERALL SUMMARY:")
        print(json.dumps(weekly, indent=2))
        print("=" * 60 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="CoinDCX Momentum Scanner & Paper-Trading Bot")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument(
        "--mode",
        choices=["run", "scan", "report"],
        default="run",
        help="Execution mode: run (continuous), scan (single pass), report (print audit report)",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    log_level = cfg.get("app", {}).get("log_level", "INFO")
    setup_logging(log_level=log_level, log_file="logs/bot.log")

    bot = MomentumBot(config_path=args.config)

    if args.mode == "scan":
        logging.info("Executing single scan pass...")
        res = bot.scan_iteration()
        print(f"Scan complete: {res}")
    elif args.mode == "report":
        bot.print_summary_report()
    else:
        bot.run_continuous()


if __name__ == "__main__":
    main()

