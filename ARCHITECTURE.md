# Algo-MCX — Technical Architecture Blueprint (V1)

Production-style hybrid intraday options trading system for NSE NIFTY weekly options via Flattrade Pi API.

**Status:** Design blueprint — implementation follows phased plan in Section 14.

**Related:** [Req.txt](./Req.txt) (requirements + locked decisions)

---

## Table of Contents

1. [High-Level Architecture](#1-high-level-architecture)
2. [Module Responsibilities](#2-module-responsibilities)
3. [Data Flow](#3-data-flow)
4. [Event Flow (Live Trading)](#4-event-flow-live-trading)
5. [Database Schema](#5-database-schema)
6. [File / Folder Structure](#6-file--folder-structure)
7. [Config Schema](#7-config-schema)
8. [Paper Trading Workflow](#8-paper-trading-workflow)
9. [Live Trading Workflow](#9-live-trading-workflow)
10. [Optional ML Filter](#10-optional-ml-filter)
11. [Failure Handling & Reconnect](#11-failure-handling--reconnect)
12. [Logging, Analytics & Audit Trail](#12-logging-analytics--audit-trail)
13. [Minimal V1 Scope](#13-minimal-v1-scope)
14. [Future V2 Extensions](#14-future-v2-extensions)
15. [Example Event Objects](#15-example-event-objects)
16. [Sample V1 Config (JSON)](#16-sample-v1-config-json)
17. [Development Phases](#17-development-phases)
18. [Technology Stack](#18-technology-stack)

---

## 1. High-Level Architecture

### 1.1 System diagram

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                         Docker Compose (Mac / Server)                        │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                              │
│  ┌──────────────────────┐         ┌──────────────────────┐                  │
│  │   trading-engine     │         │      web-app         │                  │
│  │   (Python asyncio)   │◄───────►│  (API + frontend)    │                  │
│  │                      │  REST   │                      │                  │
│  │  ┌────────────────┐  │         │  - Dashboard         │                  │
│  │  │ Event Bus      │  │         │  - Notifications UI  │                  │
│  │  │ (in-proc V1)   │  │         │  - Kill switch UI    │                  │
│  │  └───────┬────────┘  │         └──────────┬───────────┘                  │
│  │          │           │                    │                               │
│  │  ┌───────▼──────────────────────────────────────────────────────────┐   │
│  │  │ Broker Adapter ◄──► Flattrade REST + WebSocket                   │   │
│  │  └───────┬──────────────────────────────────────────────────────────┘   │
│  │          │                                                                │
│  │  Market Data ─► Option/Greeks ─► Feature ─► Rule Scanner ─► Validator   │
│  │          │                                                    │           │
│  │          │                              Risk Engine ◄────────┘           │
│  │          │                                    │                           │
│  │          │                         ML Filter (off)                        │
│  │          │                                    │                           │
│  │          │                         Execution Engine                       │
│  │          │                                    │                           │
│  │          └────────────────────► Position Manager                         │
│  │                                               │                           │
│  │                         Journal / Analytics ◄─┘                           │
│  │  Contract Selector (startup + ATM band refresh)                           │
│  └──────────────────────┬───────────────────────────────────────────────────┘
│                         │                                                    │
│  ┌──────────────────────▼──────────┐   ┌─────────────┐   ┌──────────────┐   │
│  │         PostgreSQL              │   │ Redis (opt) │   │   Volumes    │   │
│  │  candles, signals, trades, ...  │   │ notify pub  │   │  pgdata/logs │   │
│  └─────────────────────────────────┘   └─────────────┘   └──────────────┘   │
└─────────────────────────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                          Flattrade Pi API (external)
                          https://pi.flattrade.in/docs
```

### 1.2 Design principles

| Principle | Implementation |
|-----------|----------------|
| Separation of concerns | Scanner produces signals; validator filters; risk approves; execution places orders; position manager exits |
| Paper first | `BrokerAdapter` interface with `FlattradeAdapter` (live) and `PaperBrokerAdapter` (LTP simulation) |
| Rule first, ML second | ML filter disabled by default; shadow mode only until validated |
| Risk supremacy | Risk engine can veto any signal; kill switch halts all new entries |
| Audit everything | Every rejection logged; every trade stores feature snapshot + lifecycle |
| Broker realism | Static IP for live orders; Greeks degraded on Flattrade; limit orders for non-registered algo |
| Testability | Each module has defined inputs/outputs; mock adapter for unit tests; session replay for integration |

### 1.3 Runtime model (V1)

- **Single process:** `trading-engine` runs one asyncio event loop
- **Internal event bus:** `asyncio.Queue` per event type (candle, quote, signal, order, system)
- **Web app:** separate service; reads Postgres + optional Redis subscribe for real-time notifications
- **No multi-process bus in V1** unless notification latency requires Redis

---

## 2. Module Responsibilities

### 2.1 Broker Adapter (`broker/`)

**Responsibility:** Sole module that talks to Flattrade. All other modules use abstract interface.

| Function | Details |
|----------|---------|
| Auth | OAuth daily token; persist in `broker_sessions`; auto-refresh before expiry |
| Historical | `get_time_price_series` for 1m/3m/5m NIFTY candles |
| WebSocket | `start_websocket`, `subscribe` touchline for index + option tokens |
| Orders | `place_order`, `modify_order`, `cancel_order` via REST (live only) |
| Order stream | `order_update_callback` on same WebSocket |
| Reconnect | Exponential backoff; resubscribe all tokens; emit `system_events` |
| Stale detection | No tick for `stale_feed_seconds` → mark feed unhealthy |

**Interface (abstract):**

```python
class BrokerAdapter(Protocol):
    async def connect(self) -> None: ...
    async def get_candles(self, token, interval, start, end) -> list[Candle]: ...
    async def subscribe(self, tokens: list[str]) -> None: ...
    async def place_order(self, request: ExecutionRequest) -> OrderAck: ...
    async def get_positions(self) -> list[Position]: ...
    async def get_option_chain(self, symbol, strike, count) -> list[Instrument]: ...
```

**Implementations:** `FlattradeAdapter`, `PaperBrokerAdapter`

---

### 2.2 Market Data Engine (`market_data/`)

**Responsibility:** Normalize and maintain live + historical market state for NIFTY and subscribed options.

- Fetch and store candles (1m, 3m, 5m) — broker-native intervals, no self-aggregation in V1
- Maintain in-memory `MarketState` per instrument: LTP, bid/ask, volume, OI, last_update_ts
- Compute session VWAP for NIFTY from 1m candles + volume (used by feature engine)
- Separate **stream fields** (WebSocket) vs **snapshot fields** (`get_quotes` poll fallback)
- Publish `CandleUpdate` and `QuoteUpdate` events to bus
- On session start: backfill today's candles from API

---

### 2.3 Option Data / Greeks Layer (`option_data/`)

**Responsibility:** Unified option-state for all contracts in ATM ±300 band.

| Field | V1 source | Availability |
|-------|-----------|--------------|
| LTP, bid, ask | WebSocket touchline | Available |
| Volume, OI | WebSocket touchline | Available |
| IV, delta, gamma, theta, vega | — | **Unavailable** (Flattrade) |
| Lot size, tick size | `get_option_chain` / `instruments` table | Available |

- On startup: probe WebSocket for Greek fields; log result to `field_availability_log`
- V1 runs in **degraded mode** for Greeks; features that need Greeks are skipped
- Expose `OptionState` dataclass with `field_flags: dict[str, bool]`
- Periodic snapshot poll via `get_quotes` for fields missing from stream (if any)

---

### 2.4 Contract Selector (`contract_selector/`)

**Responsibility:** Resolve tradable universe and active ATM contract.

| Task | Rule |
|------|------|
| Expiry | Current weekly NIFTY expiry (NFO) |
| Strike band | ATM −300 to ATM +300 (50-pt steps → 13 strikes × 2 = 26 contracts) |
| Tradable | **ATM strike only** — one CE + one PE token tracked for execution |
| Side | Not selected here — Rule Scanner + 5m bias pick CE or PE |
| Refresh | Recompute ATM when spot crosses 50-pt boundary; update WS subscriptions |
| Master data | Upsert `instruments` table with token, tsym, strike, expiry, lot_size |

**WebSocket budget:** ~27 subscriptions (26 options + NIFTY index `NSE|26000`)

---

### 2.5 Feature Engine (`features/`)

**Responsibility:** Compute feature snapshot from market state. **Does not generate signals.**

**Index features (NIFTY):**

| Timeframe | Features |
|-----------|----------|
| 5m | `bias` (bullish/bearish/neutral), close vs VWAP, structure (HH/HL or LH/LL simplified) |
| 3m | `setup_active`, distance_to_vwap, bars_below/above_vwap |
| 1m | `reclaim_trigger`, close vs VWAP, bar range |
| Session | `minutes_since_open`, `is_expiry_day`, session VWAP |

**Option features (ATM CE/PE):**

- `spread_pct`, `bid`, `ask`, `ltp`, `volume`, `oi`, `oi_change` (if prior snapshot exists)
- Greek-dependent features: omitted in V1 (flagged unavailable)

**Output:** `FeatureSnapshot` — immutable, versioned, stored with every candidate signal

---

### 2.6 Rule Scanner (`scanner/`)

**Responsibility:** Detect candidate setups only. **Never places orders.**

**V1 plugin:** `VwapReclaimScanner`

| Stage | Bullish (CE) | Bearish (PE) |
|-------|--------------|--------------|
| 5m bias | Close above session VWAP; not making lower lows | Close below VWAP; not making higher highs |
| 3m setup | Price was below VWAP in last N bars; now within X pts | Mirror above VWAP |
| 1m trigger | Candle closes above VWAP | Candle closes below VWAP |

**Output:** `CandidateSignal` with `setup_type: "vwap_reclaim"`, `side: "CE"|"PE"`, `instrument_token`, `feature_snapshot_id`

**V2 plugin slots:** `BreakoutContinuationScanner`, `RejectionReversalScanner` — same interface

---

### 2.7 Rule Validator (`validator/`)

**Responsibility:** Filter candidates; log every failure with reason.

| Filter | Default behavior |
|--------|------------------|
| Time window | No entries before 09:25 or after 15:00 IST |
| Spread | `spread_pct` < max (e.g. 2%) |
| Liquidity | min volume + OI on ATM option |
| Expiry | Stricter or no entries on expiry day (config) |
| Regime | 5m bias must match signal side (CE ↔ bullish) |
| Cooldown | No re-entry for N minutes after exit |
| One position | Reject if any open position |
| Risk pre-check | Delegate to risk engine |
| Field availability | Required fields for VWAP setup must be present |

**Output:** `ValidationResult` — `passed: bool`, `rejection_reasons: list[str]`

---

### 2.8 Optional ML Filter (`ml/`)

**V1:** Disabled (`ml_config.enabled: false`)

| Mode | Behavior |
|------|----------|
| Off | Pass all validated candidates through |
| Shadow | Score and log to `ml_scores`; do not block trades |
| Active | Score must exceed threshold to proceed (Phase 5+) |

**Input:** Validated `CandidateSignal` + `FeatureSnapshot`  
**Output:** `MLScore` — `score`, `recommendation: take|skip`, `confidence`, `model_version`

Risk engine always overrides ML.

---

### 2.9 Risk Engine (`risk/`)

**Responsibility:** Final gate before execution; intraday state in `daily_risk_state`.

| Rule | Action |
|------|--------|
| Max daily loss | Block new entries; notify web app |
| Max loss per trade | Set stop distance cap |
| Max trades per day | Block entries |
| Max consecutive losses | Block entries + cooldown |
| Force exit time | Position manager squares off (e.g. 15:20) |
| Cooldown after SL | Timer before next entry |
| Kill switch | Manual (web UI) or auto — block all entries, optional flatten |

---

### 2.10 Execution Engine (`execution/`)

**Responsibility:** Convert approved signal to order; track lifecycle.

| Mode | Behavior |
|------|----------|
| Paper | `PaperBrokerAdapter` fills at LTP instantly |
| Live | `FlattradeAdapter` — LMT or SL-LMT per `execution_config` |

- Generate unique `client_order_id` for idempotency
- Log: signal_ts → order_sent_ts → ack_ts → fill_ts → slippage
- Handle: rejection, partial fill (live), cancel/retry with limits
- Store all events in `orders` table

---

### 2.11 Position Manager (`position/`)

**Responsibility:** Monitor open position; exit logic **separate from entry**.

| Exit type | Trigger |
|-----------|---------|
| Stop-loss | Price hits SL (LTP-based in paper) |
| Target | Price hits target |
| Time stop | Max hold minutes exceeded |
| Momentum failure | 1m close crosses VWAP against position |
| Force square-off | `force_exit_time` from risk config |
| Emergency | Stale feed, broker state mismatch, kill switch flatten |

- Update MFE/MAE on every option LTP tick while open
- On close: write `closed_trades` with full lifecycle summary

---

### 2.12 Journal / Analytics (`journal/`)

**Responsibility:** Persist all events; compute performance metrics.

- Write path: all modules emit → journal subscribers → Postgres
- Analytics queries: expectancy, win rate, avg win/loss, slippage, time-of-day, setup-wise stats
- Exposed to web-app via REST API

---

### 2.13 Web Application (`web-app/`)

**Responsibility:** Human-facing UI and in-app notifications only.

| V1 feature | Description |
|------------|-------------|
| Dashboard | Open position, today's P&L, trade count |
| Trades list | Closed + open with entry/exit reason |
| Rejections feed | Recent validation failures |
| Notifications | Risk breach, stale feed, order rejected, position closed, kill switch |
| Kill switch | Manual toggle → trading-engine API |

**Real-time:** SSE or WebSocket from web-api; optional Redis pub/sub from trading-engine

---

## 3. Data Flow

```
Flattrade API
     │
     ▼
Broker Adapter ──► Market Data Engine ──► PostgreSQL (candles, option_quotes)
     │                      │
     │                      ▼
     │              Option Data Layer ──► option_snapshots, field_availability_log
     │                      │
     ▼                      ▼
Contract Selector ◄── NIFTY spot (ATM band, instruments table)
     │
     ▼
Feature Engine ──► FeatureSnapshot (in-memory + optional option_features table)
     │
     ▼
Rule Scanner ──► candidate_signals
     │
     ▼
Rule Validator ──► validation_results
     │
     ▼
Risk Engine ◄── daily_risk_state
     │
     ▼
ML Filter (bypass if disabled) ──► ml_scores
     │
     ▼
Execution Engine ──► orders ──► Broker Adapter
     │
     ▼
Position Manager ──► positions ──► closed_trades
     │
     ▼
Journal / Analytics ──► all tables + web-app API
     │
     ▼
Web App ──► notifications table + UI
```

**Key rule:** Feature Engine runs on candle/quote events. Scanner reads **FeatureSnapshot** only — never recomputes features.

---

## 4. Event Flow (Live Trading)

### 4.1 Session lifecycle

```
STARTUP
  ├─ Load config
  ├─ Connect Postgres
  ├─ Auth Flattrade → broker_sessions
  ├─ Contract Selector: resolve weekly expiry + ATM ±300 band
  ├─ Backfill today's 1m/3m/5m candles
  ├─ Start WebSocket; subscribe NIFTY + 26 options
  ├─ Reconciliation: broker positions vs DB (live mode)
  └─ Emit SYSTEM_READY

MARKET OPEN LOOP
  ├─ QuoteUpdate (tf/tk) → Market State → Option State
  ├─ CandleUpdate (on bar close) → Feature Engine → FeatureSnapshot
  ├─ If no open position:
  │     Scanner → CandidateSignal?
  │     Validator → passed?
  │     Risk → approved?
  │     ML → pass/skip?
  │     Execution → order → fill
  │     Position Manager → OPEN
  └─ If open position:
        Position Manager → exit check on each QuoteUpdate
        On exit → closed_trades → cooldown timer

SHUTDOWN / FORCE EXIT
  ├─ force_exit_time → flatten open position
  ├─ Unsubscribe WebSocket
  ├─ Flush journal buffers
  └─ Emit SYSTEM_SHUTDOWN
```

### 4.2 Reconnect flow

```
WebSocket disconnect
  ├─ Emit FEED_DISCONNECTED → notification
  ├─ Block new entries (risk flag)
  ├─ If position open → emergency mode (widen monitoring, no new entries)
  ├─ Reconnect with backoff
  ├─ Resubscribe all tokens
  ├─ get_quotes snapshot poll for gap fill
  ├─ Backfill missed candles if gap > 1 bar
  ├─ Reconciliation check
  └─ Emit FEED_RECONNECTED → clear stale flag if quotes fresh
```

---

## 5. Database Schema

**Engine:** PostgreSQL 16+  
**Naming:** snake_case tables; UUID primary keys; `timestamptz` for all times (IST displayed in app)

### 5.1 `instruments`

```sql
CREATE TABLE instruments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    exchange        TEXT NOT NULL,           -- 'NFO'
    token           TEXT NOT NULL,
    tsym            TEXT NOT NULL,
    underlying      TEXT NOT NULL,           -- 'NIFTY'
    expiry_date     DATE NOT NULL,
    strike          NUMERIC(10,2) NOT NULL,
    option_type     TEXT NOT NULL,           -- 'CE' | 'PE'
    lot_size        INT NOT NULL,
    tick_size       NUMERIC(10,4),
    is_atm          BOOLEAN DEFAULT FALSE,
    in_band         BOOLEAN DEFAULT FALSE,   -- within ATM ±300
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (exchange, token)
);
CREATE INDEX idx_instruments_expiry ON instruments (expiry_date, underlying);
```

### 5.2 `broker_sessions`

```sql
CREATE TABLE broker_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    access_token    TEXT NOT NULL,           -- encrypt at rest in production
    expires_at      TIMESTAMPTZ,
    static_ip       TEXT,
    api_version     TEXT DEFAULT 'v2',
    created_at      TIMESTAMPTZ DEFAULT now()
);
```

### 5.3 `candles_1m` / `candles_3m` / `candles_5m`

```sql
-- Same structure for each; table name reflects interval
CREATE TABLE candles_1m (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_token TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    open            NUMERIC(12,4) NOT NULL,
    high            NUMERIC(12,4) NOT NULL,
    low             NUMERIC(12,4) NOT NULL,
    close           NUMERIC(12,4) NOT NULL,
    volume          BIGINT,
  UNIQUE (instrument_token, ts)
);
CREATE INDEX idx_candles_1m_token_ts ON candles_1m (instrument_token, ts DESC);
```

### 5.4 `option_quotes` (tick-level stream samples)

```sql
CREATE TABLE option_quotes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_token TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    ltp             NUMERIC(12,4),
    bid             NUMERIC(12,4),
    ask             NUMERIC(12,4),
    volume          BIGINT,
    oi              BIGINT,
    source          TEXT NOT NULL            -- 'websocket' | 'poll'
);
CREATE INDEX idx_option_quotes_token_ts ON option_quotes (instrument_token, ts DESC);
```

### 5.5 `option_snapshots` (periodic aggregated state)

```sql
CREATE TABLE option_snapshots (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_token TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    ltp             NUMERIC(12,4),
    bid             NUMERIC(12,4),
    ask             NUMERIC(12,4),
    spread_pct      NUMERIC(8,4),
    volume          BIGINT,
    oi              BIGINT,
    field_flags     JSONB NOT NULL DEFAULT '{}'
);
```

### 5.6 `option_features`

```sql
CREATE TABLE option_features (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_id     UUID,                    -- links to feature snapshot
    instrument_token TEXT NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    features        JSONB NOT NULL
);
```

### 5.7 `candidate_signals`

```sql
CREATE TABLE candidate_signals (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL,
    setup_type      TEXT NOT NULL,           -- 'vwap_reclaim'
    side            TEXT NOT NULL,           -- 'CE' | 'PE'
    instrument_token TEXT NOT NULL,
    tsym            TEXT NOT NULL,
    feature_snapshot JSONB NOT NULL,
    strategy_version TEXT NOT NULL,
    scanner_metadata JSONB DEFAULT '{}'
);
CREATE INDEX idx_candidate_signals_ts ON candidate_signals (ts DESC);
```

### 5.8 `validation_results`

```sql
CREATE TABLE validation_results (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_signal_id UUID NOT NULL REFERENCES candidate_signals(id),
    ts              TIMESTAMPTZ NOT NULL,
    passed          BOOLEAN NOT NULL,
    rejection_reasons TEXT[] NOT NULL DEFAULT '{}',
    validator_version TEXT NOT NULL
);
```

### 5.9 `ml_scores`

```sql
CREATE TABLE ml_scores (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_signal_id UUID NOT NULL REFERENCES candidate_signals(id),
    ts              TIMESTAMPTZ NOT NULL,
    model_version   TEXT NOT NULL,
    score           NUMERIC(8,6),
    confidence      NUMERIC(8,6),
    recommendation  TEXT NOT NULL,           -- 'take' | 'skip'
    mode            TEXT NOT NULL            -- 'shadow' | 'active'
);
```

### 5.10 `orders`

```sql
CREATE TABLE orders (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_order_id TEXT NOT NULL UNIQUE,
    broker_order_id TEXT,
    candidate_signal_id UUID REFERENCES candidate_signals(id),
    ts_created      TIMESTAMPTZ NOT NULL,
    ts_sent         TIMESTAMPTZ,
    ts_ack          TIMESTAMPTZ,
    ts_filled       TIMESTAMPTZ,
    exchange        TEXT NOT NULL,
    tsym            TEXT NOT NULL,
    side            TEXT NOT NULL,           -- 'BUY'
    quantity        INT NOT NULL,
    order_type      TEXT NOT NULL,
    limit_price     NUMERIC(12,4),
    fill_price      NUMERIC(12,4),
    filled_qty      INT DEFAULT 0,
    status          TEXT NOT NULL,
    slippage        NUMERIC(12,4),
    latency_ms      INT,
    mode            TEXT NOT NULL,           -- 'paper' | 'live'
    rejection_reason TEXT
);
```

### 5.11 `positions`

```sql
CREATE TABLE positions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id        UUID NOT NULL REFERENCES orders(id),
    instrument_token TEXT NOT NULL,
    tsym            TEXT NOT NULL,
    side            TEXT NOT NULL,
    quantity        INT NOT NULL,
    entry_price     NUMERIC(12,4) NOT NULL,
    entry_ts        TIMESTAMPTZ NOT NULL,
    stop_loss       NUMERIC(12,4),
    target          NUMERIC(12,4),
    mfe             NUMERIC(12,4) DEFAULT 0,
    mae             NUMERIC(12,4) DEFAULT 0,
    status          TEXT NOT NULL,           -- 'open' | 'closing' | 'closed'
    mode            TEXT NOT NULL
);
```

### 5.12 `closed_trades`

```sql
CREATE TABLE closed_trades (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id     UUID NOT NULL REFERENCES positions(id),
    candidate_signal_id UUID REFERENCES candidate_signals(id),
    entry_ts        TIMESTAMPTZ NOT NULL,
    exit_ts         TIMESTAMPTZ NOT NULL,
    entry_price     NUMERIC(12,4) NOT NULL,
    exit_price      NUMERIC(12,4) NOT NULL,
    quantity        INT NOT NULL,
    pnl             NUMERIC(12,4) NOT NULL,
    pnl_pct         NUMERIC(8,4),
    mfe             NUMERIC(12,4),
    mae             NUMERIC(12,4),
    exit_reason     TEXT NOT NULL,
    hold_seconds    INT,
    signal_snapshot JSONB NOT NULL,
    setup_type      TEXT NOT NULL,
    mode            TEXT NOT NULL
);
```

### 5.13 `daily_risk_state`

```sql
CREATE TABLE daily_risk_state (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_date      DATE NOT NULL UNIQUE,
    realized_pnl    NUMERIC(12,4) DEFAULT 0,
    trade_count     INT DEFAULT 0,
    consecutive_losses INT DEFAULT 0,
    kill_switch     BOOLEAN DEFAULT FALSE,
    entries_blocked BOOLEAN DEFAULT FALSE,
    block_reason    TEXT,
    updated_at      TIMESTAMPTZ DEFAULT now()
);
```

### 5.14 `system_events`

```sql
CREATE TABLE system_events (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type      TEXT NOT NULL,
    severity        TEXT NOT NULL,           -- 'info' | 'warning' | 'critical'
    message         TEXT NOT NULL,
    metadata        JSONB DEFAULT '{}'
);
```

### 5.15 `field_availability_log`

```sql
CREATE TABLE field_availability_log (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    field_name      TEXT NOT NULL,
    source          TEXT NOT NULL,
    available       BOOLEAN NOT NULL,
    notes           TEXT
);
```

### 5.16 `notifications`

```sql
CREATE TABLE notifications (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts              TIMESTAMPTZ NOT NULL DEFAULT now(),
    type            TEXT NOT NULL,
    severity        TEXT NOT NULL,
    title           TEXT NOT NULL,
    message         TEXT NOT NULL,
    read            BOOLEAN DEFAULT FALSE,
    related_entity  TEXT,
    related_id      UUID
);
CREATE INDEX idx_notifications_unread ON notifications (read, ts DESC);
```

### 5.17 Retention policy (V1 defaults)

| Table | Retention |
|-------|-----------|
| `option_quotes` | 7 days (tick data) |
| `candles_*` | 90 days |
| `candidate_signals`, `validation_results` | 1 year |
| `closed_trades` | indefinite |
| `notifications` | 30 days |

---

## 6. File / Folder Structure

```
Algo-MCX/
├── Req.txt
├── ARCHITECTURE.md
├── docker-compose.yml
├── docker-compose.override.yml.example
├── .env.example
├── README.md
│
├── config/
│   ├── broker_config.yaml
│   ├── symbols_config.yaml
│   ├── strategy_config.yaml
│   ├── validator_config.yaml
│   ├── risk_config.yaml
│   ├── execution_config.yaml
│   ├── paper_trading_config.yaml
│   ├── market_session_config.yaml
│   ├── runtime_config.yaml
│   ├── logging_config.yaml
│   ├── ml_config.yaml
│   └── data_availability_config.yaml
│
├── trading-engine/
│   ├── pyproject.toml
│   ├── Dockerfile
│   ├── src/
│   │   ├── main.py
│   │   ├── bus/
│   │   │   └── event_bus.py
│   │   ├── broker/
│   │   │   ├── base.py
│   │   │   ├── flattrade.py
│   │   │   └── paper.py
│   │   ├── market_data/
│   │   │   ├── engine.py
│   │   │   └── vwap.py
│   │   ├── option_data/
│   │   │   └── layer.py
│   │   ├── contract_selector/
│   │   │   └── selector.py
│   │   ├── features/
│   │   │   └── engine.py
│   │   ├── scanner/
│   │   │   ├── base.py
│   │   │   └── vwap_reclaim.py
│   │   ├── validator/
│   │   │   └── engine.py
│   │   ├── ml/
│   │   │   └── filter.py
│   │   ├── risk/
│   │   │   └── engine.py
│   │   ├── execution/
│   │   │   └── engine.py
│   │   ├── position/
│   │   │   └── manager.py
│   │   ├── journal/
│   │   │   ├── writer.py
│   │   │   └── analytics.py
│   │   ├── models/
│   │   │   └── events.py
│   │   └── db/
│   │       ├── connection.py
│   │       └── repositories/
│   └── tests/
│       ├── unit/
│       ├── integration/
│       └── fixtures/
│
├── web-app/
│   ├── Dockerfile
│   ├── backend/
│   │   ├── main.py
│   │   ├── api/
│   │   │   ├── trades.py
│   │   │   ├── positions.py
│   │   │   ├── notifications.py
│   │   │   └── control.py          # kill switch
│   │   └── sse/
│   │       └── stream.py
│   └── frontend/
│       ├── package.json
│       └── src/
│           ├── pages/
│           │   ├── Dashboard.tsx
│           │   ├── Trades.tsx
│           │   └── Notifications.tsx
│           └── components/
│
├── db/
│   └── migrations/
│       └── 001_initial_schema.sql
│
└── scripts/
    ├── phase0_spike.py
    └── session_replay.py
```

---

## 7. Config Schema

Config files are YAML; secrets via environment variables only.

### 7.1 Environment variables (`.env`)

```bash
FLATTRADE_USER_ID=
FLATTRADE_API_KEY=
FLATTRADE_API_SECRET=
FLATTRADE_REDIRECT_URL=
DATABASE_URL=postgresql://algoflat:algoflat@postgres:5432/algoflat
REDIS_URL=redis://redis:6379/0          # optional
TRADING_MODE=paper                       # paper | live
LOG_LEVEL=INFO
```

### 7.2 Key config sections

See [Section 16](#16-sample-v1-config-json) for full merged JSON example.

| File | Purpose |
|------|---------|
| `broker_config` | API base URL, reconnect backoff, stale_feed_seconds |
| `symbols_config` | NIFTY token, strike band ±300, exchange NFO |
| `strategy_config` | VWAP reclaim thresholds, strategy_version |
| `validator_config` | Spread, liquidity, time windows |
| `risk_config` | Daily loss, per-trade loss, force_exit_time |
| `execution_config` | order_type, product (MIS), retry limits |
| `paper_trading_config` | fill_model: ltp, partial_fills: false |
| `market_session_config` | IST hours, holidays file, expiry day rules |
| `runtime_config` | event queue sizes, reconciliation interval |
| `ml_config` | enabled: false, mode: shadow |
| `data_availability_config` | required fields per setup; poll intervals |

---

## 8. Paper Trading Workflow

```
1. TRADING_MODE=paper → PaperBrokerAdapter active
2. Session start: same data path as live (real Flattrade market data)
3. Scanner produces CandidateSignal
4. Validator + Risk approve
5. Execution Engine calls PaperBrokerAdapter.place_order()
   - fill_price = current option LTP
   - fill_ts = now
   - slippage = 0 (LTP model)
   - status = COMPLETE immediately
6. Position Manager monitors real LTP ticks for SL/target/time exits
7. Exit fill at LTP → closed_trades row
8. All rows tagged mode='paper'
9. Analytics computed identically to live for comparison
10. No static IP required (no real orders sent)
```

**Important:** Paper uses **real** market data but **simulated** execution. Metrics are optimistic vs live bid/ask fills.

---

## 9. Live Trading Workflow

```
Prerequisites:
  - TRADING_MODE=live
  - Server with Flattrade-registered static IP
  - Paper metrics validated per internal checklist
  - Manual supervision required in V1

1. FlattradeAdapter authenticates; token in broker_sessions
2. Same signal path through Validator + Risk
3. Execution Engine:
   - Builds LMT or SL-LMT order per execution_config
   - product_type: MIS (intraday)
   - client_order_id for idempotency
   - place_order via REST from static IP
4. Order updates via WebSocket → update orders table
5. On fill → Position Manager opens tracking
6. Exit: place opposite order; monitor partial fills
7. Reconciliation every N minutes: broker positions vs DB
8. All rows tagged mode='live'
9. slippage = fill_price - signal_reference_ltp
```

---

## 10. Optional ML Filter

```
                    ┌─────────────┐
Validated Signal ──►│  ML Filter  │──► ml_scores (always logged in shadow)
                    │  (optional) │
                    └──────┬──────┘
                           │
              ┌────────────┴────────────┐
              │ enabled=false │ skip    │
              │ shadow        │ log only│
              │ active        │ threshold gate │
              └────────────┬────────────┘
                           ▼
                      Risk Engine  ◄── always final authority
```

**Training data source:** `closed_trades` + `candidate_signals` where `validation_results.passed = true`  
**Label:** profitable trade = 1, loss = 0 (or regression on R-multiple)  
**V1:** Module stub returns pass-through; no model file required

---

## 11. Failure Handling & Reconnect

| Failure | Detection | Response |
|---------|-----------|----------|
| WebSocket disconnect | socket_close_callback | Block entries; reconnect backoff; notify |
| Stale feed | no tick > stale_feed_seconds | Block entries; if position open → emergency exit option |
| Order rejected | order update status | Log; notify; do not retry blindly |
| Partial fill (live) | fillshares < qty | Position manager tracks actual qty |
| Token expiry | 401 / scheduled check | Refresh OAuth; pause entries during refresh |
| DB unavailable | connection error | Buffer critical events to disk queue; halt trading |
| Broker position mismatch | reconciliation | Emit critical notification; optional flatten |
| Kill switch | web UI / daily loss | Block entries; flatten if configured |

**Reconnect backoff:** 1s → 2s → 4s → 8s → max 60s  
**Idempotency:** `client_order_id` = `{date}_{signal_id}_{side}` — never reuse

---

## 12. Logging, Analytics & Audit Trail

### 12.1 Structured log fields (every log line)

```json
{
  "ts": "2026-07-13T10:15:00+05:30",
  "level": "INFO",
  "module": "scanner.vwap_reclaim",
  "event": "candidate_detected",
  "trace_id": "uuid",
  "instrument": "NIFTY24JUL24500CE",
  "mode": "paper"
}
```

### 12.2 Audit requirements

| Event | Required fields |
|-------|-----------------|
| Candidate signal | feature_snapshot, strategy_version, scanner_metadata |
| Validation failure | rejection_reasons[] (all failed filters) |
| Order | signal_ts, sent_ts, ack_ts, fill_ts, slippage, latency_ms |
| Position | entry_price, SL, target, live MFE/MAE updates |
| Close | exit_reason, hold_seconds, pnl, final MFE/MAE |
| System | feed health, reconnect count, kill switch activations |

### 12.3 Analytics (Phase 4 queries)

- Win rate, expectancy, profit factor
- Avg win / avg loss / R-multiple
- Slippage distribution (live vs paper)
- Performance by time-of-day bucket
- Performance by setup_type
- Rejection reason frequency (tune validator)
- MAE/MFE efficiency (exit timing quality)

---

## 13. Minimal V1 Scope

**In scope:**

- NSE NIFTY weekly options
- ATM ±300 monitored; trade ATM only
- 5m bias → CE/PE; VWAP reclaim scanner
- 1m/3m/5m candles from Flattrade
- WebSocket LTP/OI/volume/bid/ask
- Paper trading with LTP fills
- PostgreSQL persistence
- Docker Compose (trading-engine + web-app + postgres)
- In-app notifications + minimal dashboard
- One open position at a time
- Kill switch via web UI

**Out of scope (V2+):**

- BSE / SENSEX
- Breakout / rejection scanners
- Derived Greeks
- ML active filtering
- Multi-position / multi-strategy
- Email / Telegram alerts
- Mobile app

---

## 14. Future V2 Extensions

| Extension | Notes |
|-----------|-------|
| BSE / SENSEX | symbols_config per underlying; contract selector generalization |
| Breakout / rejection scanners | Plugin interface already defined |
| Derived Greeks | Black-Scholes layer when rules require delta/IV |
| ML active mode | After N paper/live labeled trades |
| Multi-strike selection | ITM/OTM by delta band |
| Redis event bus | If single-process notification latency insufficient |
| Session replay tool | Record WS feed; replay through scanner offline |
| Registered algo API key | Higher order rate; market orders if permitted |

---

## 15. Example Event Objects

### 15.1 Candle update

```json
{
  "event_type": "candle_update",
  "ts": "2026-07-13T10:15:00+05:30",
  "instrument_token": "26000",
  "exchange": "NSE",
  "interval": "1m",
  "open": 24512.5,
  "high": 24528.0,
  "low": 24510.0,
  "close": 24525.3,
  "volume": 184200,
  "is_closed": true
}
```

### 15.2 Option snapshot update

```json
{
  "event_type": "option_snapshot_update",
  "ts": "2026-07-13T10:15:01+05:30",
  "instrument_token": "12345678",
  "tsym": "NIFTY14JUL26C24500",
  "ltp": 142.5,
  "bid": 142.0,
  "ask": 143.0,
  "spread_pct": 0.7,
  "volume": 1250000,
  "oi": 4500000,
  "field_flags": {
    "ltp": true,
    "bid": true,
    "ask": true,
    "oi": true,
    "iv": false,
    "delta": false
  },
  "source": "websocket"
}
```

### 15.3 Candidate signal

```json
{
  "event_type": "candidate_signal",
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "ts": "2026-07-13T10:15:02+05:30",
  "setup_type": "vwap_reclaim",
  "side": "CE",
  "instrument_token": "12345678",
  "tsym": "NIFTY14JUL26C24500",
  "strategy_version": "vwap_reclaim_v1.0.0",
  "feature_snapshot": {
    "nifty_spot": 24525.3,
    "session_vwap": 24498.7,
    "bias_5m": "bullish",
    "setup_3m": "dip_and_reclaim",
    "trigger_1m": "close_above_vwap",
    "option_spread_pct": 0.7,
    "option_oi": 4500000
  }
}
```

### 15.4 Validation result

```json
{
  "event_type": "validation_result",
  "candidate_signal_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "ts": "2026-07-13T10:15:02+05:30",
  "passed": false,
  "rejection_reasons": [
    "spread_filter: spread_pct 2.3% exceeds max 2.0%",
    "liquidity_filter: volume below minimum"
  ],
  "validator_version": "validator_v1.0.0"
}
```

### 15.5 Execution request

```json
{
  "event_type": "execution_request",
  "client_order_id": "20260713_a1b2c3d4_BUY",
  "ts": "2026-07-13T10:15:03+05:30",
  "candidate_signal_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "exchange": "NFO",
  "tsym": "NIFTY14JUL26C24500",
  "side": "BUY",
  "quantity": 75,
  "order_type": "LMT",
  "limit_price": 143.0,
  "product": "MIS",
  "reference_ltp": 142.5,
  "mode": "paper"
}
```

### 15.6 Order update

```json
{
  "event_type": "order_update",
  "ts": "2026-07-13T10:15:03+05:30",
  "client_order_id": "20260713_a1b2c3d4_BUY",
  "broker_order_id": "PAPER-00001",
  "status": "COMPLETE",
  "report_type": "Fill",
  "fill_price": 142.5,
  "filled_qty": 75,
  "avg_price": 142.5,
  "slippage": 0.0,
  "latency_ms": 12,
  "mode": "paper"
}
```

### 15.7 Position update

```json
{
  "event_type": "position_update",
  "position_id": "p1p2p3p4-1111-2222-3333-444444444444",
  "ts": "2026-07-13T10:16:30+05:30",
  "status": "open",
  "instrument_token": "12345678",
  "tsym": "NIFTY14JUL26C24500",
  "entry_price": 142.5,
  "current_ltp": 148.0,
  "unrealized_pnl": 412.5,
  "mfe": 6.2,
  "mae": -1.5,
  "stop_loss": 135.0,
  "target": 155.0,
  "hold_seconds": 87
}
```

### 15.8 Trade close summary

```json
{
  "event_type": "trade_close_summary",
  "closed_trade_id": "t1t2t3t4-aaaa-bbbb-cccc-dddddddddddd",
  "position_id": "p1p2p3p4-1111-2222-3333-444444444444",
  "entry_ts": "2026-07-13T10:15:03+05:30",
  "exit_ts": "2026-07-13T10:18:45+05:30",
  "entry_price": 142.5,
  "exit_price": 151.0,
  "quantity": 75,
  "pnl": 637.5,
  "pnl_pct": 5.96,
  "mfe": 9.5,
  "mae": -2.0,
  "exit_reason": "target_hit",
  "hold_seconds": 222,
  "setup_type": "vwap_reclaim",
  "mode": "paper",
  "signal_snapshot": { "bias_5m": "bullish", "trigger_1m": "close_above_vwap" }
}
```

---

## 16. Sample V1 Config (JSON)

Merged reference config (split into YAML files in implementation):

```json
{
  "broker_config": {
    "api_base_url": "https://piconnect.flattrade.in/PiConnectTP",
    "websocket_url": "wss://piconnect.flattrade.in/PiConnectWSTp/",
    "reconnect_backoff_seconds": [1, 2, 4, 8, 16, 32, 60],
    "stale_feed_seconds": 5,
    "token_refresh_before_expiry_minutes": 30
  },
  "symbols_config": {
    "underlying": "NIFTY",
    "exchange_spot": "NSE",
    "exchange_options": "NFO",
    "spot_token": "26000",
    "expiry_type": "weekly",
    "strike_band_points": 300,
    "strike_step": 50,
    "tradable_strike": "ATM"
  },
  "strategy_config": {
    "active_scanner": "vwap_reclaim",
    "strategy_version": "vwap_reclaim_v1.0.0",
    "vwap_reclaim": {
      "bias_timeframe": "5m",
      "setup_timeframe": "3m",
      "trigger_timeframe": "1m",
      "setup_lookback_bars": 5,
      "max_distance_to_vwap_points": 15,
      "neutral_bias_blocks_trade": true
    }
  },
  "validator_config": {
    "validator_version": "validator_v1.0.0",
    "entry_start_time": "09:25",
    "entry_end_time": "15:00",
    "max_spread_pct": 2.0,
    "min_option_volume": 100000,
    "min_option_oi": 500000,
    "expiry_day_block_entries": true,
    "cooldown_after_exit_minutes": 5,
    "required_fields": ["ltp", "bid", "ask", "oi"]
  },
  "risk_config": {
    "max_daily_loss": 5000,
    "max_loss_per_trade": 1500,
    "max_trades_per_day": 10,
    "max_consecutive_losses": 3,
    "force_exit_time": "15:20",
    "cooldown_after_stop_loss_minutes": 10,
    "kill_switch_flatten": true
  },
  "execution_config": {
    "order_type": "LMT",
    "product": "MIS",
    "price_buffer_ticks": 1,
    "max_retry_attempts": 2,
    "retry_delay_seconds": 1
  },
  "paper_trading_config": {
    "fill_model": "ltp",
    "partial_fills": false,
    "simulate_latency_ms": 0
  },
  "market_session_config": {
    "timezone": "Asia/Kolkata",
    "market_open": "09:15",
    "market_close": "15:30",
    "pre_warm_minutes": 5,
    "holidays_file": "config/nse_holidays.json"
  },
  "runtime_config": {
    "event_queue_max_size": 10000,
    "reconciliation_interval_seconds": 60,
    "option_quote_sample_interval_seconds": 1
  },
  "logging_config": {
    "level": "INFO",
    "format": "json",
    "log_dir": "/var/log/algomcx"
  },
  "ml_config": {
    "enabled": false,
    "mode": "shadow",
    "model_path": null,
    "score_threshold": 0.6
  },
  "data_availability_config": {
    "greek_fields_expected": false,
    "poll_fallback_interval_seconds": 30,
    "degraded_mode_allowed": true
  }
}
```

---

## 17. Development Phases

### Phase 0 — Flattrade spike (2–3 days)

- [ ] OAuth token flow
- [ ] Fetch 1m/3m/5m NIFTY candles
- [ ] WebSocket subscribe NIFTY + 2 option contracts
- [ ] `get_option_chain` for weekly expiry
- [ ] Confirm touchline fields (OI, no Greeks)
- [ ] Document rate limits and errors

### Phase 1 — Data + journaling (1–2 weeks)

- [ ] Docker Compose: postgres, trading-engine skeleton
- [ ] DB migrations (all tables)
- [ ] Broker adapter + market data engine
- [ ] Contract selector (ATM ±300)
- [ ] Journal writer persisting candles + quotes
- [ ] Field availability probe logged

### Phase 2 — Scanner + validator (1 week)

- [ ] Feature engine (VWAP, bias, setup, trigger)
- [ ] VWAP reclaim scanner
- [ ] Validator with all filters
- [ ] Unit tests with fixture snapshots
- [ ] Rejection logging verified

### Phase 3 — Paper execution (1 week)

- [ ] PaperBrokerAdapter (LTP fills)
- [ ] Execution engine
- [ ] Position manager (SL, target, time, momentum, force exit)
- [ ] MFE/MAE tracking
- [ ] End-to-end paper session test

### Phase 4 — Web app + analytics (1–2 weeks)

- [ ] Web API: trades, positions, rejections, P&L
- [ ] Notification center (SSE/WebSocket)
- [ ] Kill switch UI
- [ ] Analytics queries + dashboard
- [ ] Tune VWAP thresholds from paper data

### Phase 5 — ML shadow (optional)

- [ ] ML filter stub + shadow logging
- [ ] Export training dataset from closed_trades
- [ ] Evaluate before enabling active mode

### Phase 6 — Controlled live (after paper validation)

- [ ] Deploy to static-IP server
- [ ] FlattradeAdapter live orders (LMT/SL-LMT)
- [ ] Reconciliation + slippage monitoring
- [ ] Supervised session only; compare live vs paper metrics

---

## 18. Technology Stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.12+ | Flattrade official Python API; asyncio |
| Async | asyncio + aiohttp/websockets | Single-process event loop |
| Database | PostgreSQL 16 | User requirement; JSONB for snapshots |
| Migrations | Alembic or raw SQL in `db/migrations/` | Simple V1 |
| Config | PyYAML + pydantic-settings | Validated config load |
| Logging | structlog | JSON structured logs |
| Web backend | FastAPI | SSE, REST, async |
| Web frontend | React + Vite (or Next.js) | Minimal dashboard |
| Containers | Docker Compose | Mac dev + server deploy |
| Optional cache | Redis 7 | Notification pub/sub |
| Testing | pytest + pytest-asyncio | Unit + integration |
| HTTP client | httpx | Async REST to Flattrade |

**Flattrade SDK:** Start from [flattrade/pythonAPI](https://github.com/flattrade/pythonAPI); wrap behind `BrokerAdapter` interface — do not leak SDK types into other modules.

---

*End of blueprint. Implementation begins at Phase 0 upon approval.*
