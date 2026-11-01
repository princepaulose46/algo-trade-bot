# CoinDCX Momentum Scanner & Paper-Trading Bot

Technical implementation of the **CoinDCX Momentum Scanner & Paper-Trading Bot** (Version 1.0) built strictly to specification.

The bot detects liquid crypto markets showing early momentum after an initial move, records every qualifying candidate to eliminate selection bias, simulates rule-based paper trades with strict risk management, and produces crash-safe JSON/JSONL audit reports.

> **Read-Only Security Notice**: This bot connects only to public, read-only exchange market-data endpoints. It starts without trading API keys and **cannot place real orders or put capital at risk**.

---

## 1. Core Hypothesis

After a coin moves approximately **+5%**, a subset continues into larger moves (+10%, +20%, or +50%). The bot measures whether this continuation signal provides a statistically significant edge after fees and slippage, without assuming that a +5% move inherently predicts further continuation.

To eliminate **selection bias**, the bot logs **every qualifying candidate** (both entered and filtered) and tracks fixed-horizon forward returns (1h, 4h, 12h, 24h) and continuation milestones (+10%, +20%, +50%).

---

## 2. Architecture & Data Flow

```
CoinDCX Public Market Data (REST + WebSocket)
               │
               ▼
     Market Data Adapter (coindcx/client.py, coindcx/symbols.py)
               │
               ▼
   Normalizer + Candle Cache (market/candles.py)
               │
               ▼
   Universe / Liquidity Filter (market/universe.py)
               │
               ▼
   Momentum Candidate Scanner (strategy/scanner.py)
         │                     │
         ▼                     ▼
 Candidate Tracker      Signal Evaluator (strategy/signals.py)
 (candidates.jsonl)            │
                               ▼
                        Paper Engine (paper/engine.py, paper/sizing.py)
                               │
                               ▼
                      Exit / Position Manager
                               │
                               ▼
                   JSON Report Writer (reporting/json_store.py)
                               │
               ┌───────────────┴───────────────┐
               ▼                               ▼
       candidates.jsonl / trades.jsonl    daily_summary.json
```

---

## 3. Project Structure

```
.
├── app.py                   # Main entry point (run, scan, report modes)
├── backtest.py              # Historical candle replay & backtesting engine
├── config.yaml              # Strategy parameters, universe filters, guardrails
├── config_loader.py         # Config parser with PyYAML and fallback loader
├── requirements.txt         # Dependencies
├── README.md                # Documentation & usage guide
├── coindcx/
│   ├── __init__.py
│   ├── client.py            # Public REST & WebSocket client with backoff
│   └── symbols.py           # Symbol discovery, metadata, spot vs perp classifier
├── market/
│   ├── __init__.py
│   ├── candles.py           # Candle geometry validation, deduplication, cache
│   ├── indicators.py        # EMA, RSI, VWAP, ATR, Relative Volume, Swing Low
│   └── universe.py          # Spot/futures separation, liquidity & spread filter
├── strategy/
│   ├── __init__.py
│   ├── scanner.py           # Section 5.2 Initial trigger & candidate tracker
│   └── signals.py           # Section 5.3 Confirmation & Section 5.4 Anti-chase
├── paper/
│   ├── __init__.py
│   ├── engine.py            # Trade simulation, exits, 2R target, same-candle rule
│   └── sizing.py            # Risk-based sizing (0.5%), guardrails, notional caps
├── reporting/
│   ├── __init__.py
│   ├── json_store.py        # Atomic JSON replace & append-only JSONL writes
│   └── summaries.py         # Daily & weekly reconciled statistics
├── reports/                 # Output directory
│   ├── candidates.jsonl     # Auditable candidate log
│   ├── trades.jsonl         # Detailed trade records
│   └── daily_summary.json   # Reconciled daily summary
├── logs/
│   └── bot.log              # Persistent execution log
└── tests/                   # 35 comprehensive automated tests
    ├── test_indicators.py
    ├── test_candles.py
    ├── test_universe.py
    ├── test_scanner_and_signals.py
    ├── test_paper_engine.py
    ├── test_json_store_and_summaries.py
    ├── test_backtest.py
    └── test_integration.py
```

---

## 4. Key Rules & Strategy Specifications

### 4.1 Market Universe & Eligibility (Section 5.1)
- **Separate Spot and Perpetual Futures**: Never mixes spot volume with futures open interest.
- **Minimum 24h Quote Volume**: Default $\ge$ 100,000 USDT/INR.
- **Maximum Bid-Ask Spread**: Default $\le$ 0.20%.
- **Active Market Status**: Excludes halted, delisted, or missing metadata.
- **Audit Trail**: Records exact exclusion reason for every skipped market (`MARKET_TYPE_MISMATCH`, `INSUFFICIENT_VOLUME`, `EXCESSIVE_SPREAD`, etc.).

### 4.2 Initial Candidate Trigger (Section 5.2)
A market qualifies as a candidate when:
1. `24h percentage change >= +5.0%` (calculated consistently via defined method).
2. `Latest completed 15m candle is valid` (strictly completed candles; no forming candles).
3. `15m momentum > 0` (positive return on current completed candle).
4. `Relative volume >= 2.0` (candle volume $\ge 2 \times$ 20-period volume SMA).
5. `Spread <= configured maximum`.
6. `Quote volume >= configured minimum`.

### 4.3 Continuation Confirmation (Section 5.3)
To confirm an entry signal for a candidate:
- `15m close > 15m VWAP`
- `15m close > 15m EMA(50)`
- `RSI(14) > 55`
- `Relative Volume >= 2.0`
- `Breakout of recent range high` (past 20 candles)
- Derivatives context (OI / funding): Recorded if available; if not exposed on public feeds, recorded as `null` without fabricating data.

### 4.4 Anti-Chase Rules (Section 5.4)
- Never enters solely because 24h change crossed +5%.
- Completed candles only; rejects unfinished bars.
- **EMA Extension Guard**: Rejects entries if close is excessively extended ($> 6\%$) from EMA(50).
- **Per-Symbol Cooldown**: Applies an 8-candle cooldown after a failed signal or stopped trade.
- **Stop Distance Sanitization**: Rejects trades where stop distance is impractical ($< 0.2\%$ or $> 15\%$).

### 4.5 Paper-Trading Guardrails & Sizing (Section 6)
- **Simulated Exposure**: 1× exposure (no leverage distortion in v1).
- **Starting Paper Equity**: 20,000 INR.
- **Risk Per Trade**: 0.5% of current equity (100 INR at 20,000 INR).
- **Position Sizing Formula**:
  $$\text{risk\_amount} = \text{equity} \times \text{risk\_fraction}$$
  $$\text{risk\_per\_unit} = \text{entry\_price} - \text{stop\_price}$$
  $$\text{quantity} = \frac{\text{risk\_amount}}{\text{risk\_per\_unit}}$$
  $$\text{notional} = \text{quantity} \times \text{entry\_price}$$
- **Concurrent Trades Limit**: Maximum 3 open trades.
- **Daily Loss Guardrail**: Maximum 1% loss of start-of-day equity; stops opening new trades upon breach.
- **No Martingale / No Widening**: Stop prices are frozen at entry.

### 4.6 Exit Model (Section 6.3)
- **Initial Stop**: Frozen below retest swing low with ATR-based buffer.
- **Initial Target**: 2R (twice entry-to-stop risk).
- **Conservative Same-Candle Resolution**: If stop and target are both hit within the same candle, **stop is assumed first**.
- **Trailing Stop**: Moves stop to entry (breakeven) once price reaches 1R profit.
- **Time Stop**: Position closed after 24 hours if neither stop nor target triggered.
- **Cost Modeling**: 0.1% fee per side + 5 bps slippage per side modeled on all exits.

---

## 5. JSON Data Model & Crash-Safety (Section 8 & 11)

- **Atomic Writes**: Writes full temporary files, flushes, syncs to disk (`fsync`), then atomically replaces (`os.replace`) to ensure zero file corruption on crashes.
- **JSONL Append-Only**: Candidates and trades are written line-by-line with immediate flushing.
- **Startup Recovery**: On startup, auto-recovers from existing reports, ignores partially written corrupted lines, and reloads open paper trades.
- **Deduplication**: Deduplicates events by `symbol + candle_timestamp + event_type`.

### Trade Record Format (`reports/trades.jsonl`):
```json
{
  "trade_id": "PT-000001",
  "strategy_version": "momentum_v1",
  "symbol": "B-BTC_USDT",
  "market_type": "perpetual",
  "side": "LONG",
  "detected_at": "2026-11-01T14:32:00Z",
  "entered_at": "2026-11-01T14:47:00Z",
  "entry_price": 1.2345,
  "stop_price": 1.21,
  "target_price": 1.2835,
  "quantity": 4000.0,
  "notional": 4938.0,
  "leverage_simulated": 1,
  "risk_amount": 98.0,
  "status": "OPEN",
  "exit_price": null,
  "exit_at": null,
  "exit_reason": null,
  "gross_pnl": null,
  "fees_estimate": 9.876,
  "slippage_estimate": 4.938,
  "funding_estimate": null,
  "net_pnl": null,
  "net_pnl_pct_equity": null,
  "mfe_pct": 0.0,
  "mae_pct": 0.0,
  "reached_10pct": false,
  "reached_20pct": false,
  "reached_50pct": false
}
```

### Candidate Record Format (`reports/candidates.jsonl`):
```json
{
  "candidate_id": "C-000001",
  "symbol": "B-BTC_USDT",
  "detected_at": "2026-11-01T14:32:00Z",
  "change_24h_pct": 5.4,
  "change_15m_pct": 2.1,
  "relative_volume": 3.2,
  "spread_pct": 0.08,
  "vwap_distance_pct": 0.4,
  "rsi_15m": 61.2,
  "open_interest_change_pct": null,
  "decision": "FILTERED",
  "reason": "OI_DATA_UNAVAILABLE",
  "max_return_1h_pct": 3.2,
  "max_return_4h_pct": 11.4,
  "max_return_24h_pct": 18.7,
  "reached_10pct": true,
  "reached_20pct": false,
  "reached_50pct": false
}
```

---

## 6. How to Run

### Installation
Dependencies can be installed using `pip`:
```bash
pip install -r requirements.txt
```
*(Note: Core functionality runs with zero external dependencies using Python 3.9+ standard library).*

### 1. Run Continuous Paper Bot
Monitors eligible markets every 30 seconds, logs candidates, and manages paper positions:
```bash
python3 app.py --mode run --config config.yaml
```

### 2. Single Scan Pass
Performs one scan pass across the universe and exits:
```bash
python3 app.py --mode scan --config config.yaml
```

### 3. Print Reconciled Audit Report
Generates daily summary and weekly statistics reconciled against raw JSONL records:
```bash
python3 app.py --mode report --config config.yaml
```

### 4. Run Backtesting Replay
Replays historical candle series with full fee, slippage, and conservative execution modeling:
```bash
python3 -c "
from backtest import BacktestRunner
from market.candles import Candle
runner = BacktestRunner('config.yaml')
# Feed historical candles to runner.run('B-BTC_USDT', candles)
"
```

### 5. Run Automated Test Suite
Runs all 35 unit and integration tests:
```bash
python3 -m unittest discover tests
```

---

## 7. Acceptance Checklist Compliance

| Requirement | Implementation & Verification | Status |
|:---|:---|:---:|
| Bot starts without a trading API key and cannot place orders | `CoinDCXClient` only connects to public REST/WebSocket endpoints; no trade routes exist | Verified |
| All eligible markets scanned or exclusions explained | `UniverseFilter.filter_universe()` audits every market with machine-readable reasons | Verified |
| Every candidate recorded once with timestamped indicator values | `MomentumScanner` deduplicates and records to `candidates.jsonl` | Verified |
| Reproducible entry, stop, target, size, exit reason | `PositionSizer` & `PaperEngine` enforce exact math, 2R target, swing low stops | Verified |
| Open paper trades survive restarts without duplicate entries | `MomentumBot._recover_and_initialize()` reloads open trades and increments ID counters | Verified |
| Fees & slippage assumptions visible in reports | Explicit fields `fees_estimate`, `slippage_estimate`, and separate `gross_pnl`/`net_pnl` | Verified |
| Daily/weekly summaries reconcile with raw records | `SummaryGenerator` calculates totals directly from raw candidate and trade objects | Verified |
| Stale data, API outage, malformed response stop new entries safely | Stale checks, backoff retry, and safe error handling prevent invalid entries | Verified |

