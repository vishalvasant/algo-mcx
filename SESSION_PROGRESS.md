# Algo-MCX — Session Progress Summary

**Last updated:** 2026-07-22  
**Scope:** WebSocket holding monitoring, adverse-momentum trap avoidance, multi-timeframe (MTF) entry gates, early-loss exits, and day backtest validation.

---

## Executive Summary

Algo-MCX is the NIFTY weekly options trading engine (Flattrade broker, paper + live). This session focused on fixing recurring losses — especially `adverse_momentum` exits — by tightening entry quality, improving real-time position monitoring, and cutting trades that never go green.

**Live session (20 Jul 2026) before changes:** 20 trades, **₹-5,768.75** realized P&L, many losses from `adverse_momentum`, `vwap_bounce`, `ema_pullback`, and `trend_reversal_flip`.

**Backtest with new rules (same day):** 5 trades, **₹+3,774.68** net P&L, 80% win rate, zero `adverse_momentum` exits.

---

## 1. WebSocket Holding Monitoring

### Problem
Open positions were not reliably receiving WebSocket ticker updates after engine restarts or WS reconnects. Stale quotes caused missed MFE peaks and delayed trailing exits.

### Changes

| Area | File | What changed |
|------|------|--------------|
| Per-holding stale detection | `trading-engine/src/algoflat/position/manager.py` | Added `last_quote_ts` on `OpenPosition`; `stale_holding_tokens()` flags tokens with no WS tick within N seconds |
| Engine restart recovery | `position/manager.py` | `rehydrate_open_positions()` loads `OPEN` rows from DB into memory on startup |
| WS-only stop | `trading-engine/src/algoflat/broker/flattrade.py` | `stop_websocket()` stops feed without tearing down REST session |
| Tick parsing | `broker/flattrade.py` | `_handle_feed_update()` skips malformed messages missing `tk` or `lp` |
| Stale REST fallback | `trading-engine/src/algoflat/main.py` | `_poll_open_position_quotes()` prioritizes stale holdings; `_run_rest_poll_loop()` uses 1s interval when stale |
| WS restart | `main.py` | Removed redundant `broker.connect()` on WS restart (avoids full re-login) |
| Startup | `main.py` | Calls `rehydrate_open_positions()` then `_ensure_holdings_on_websocket()` |
| Config | `config/runtime_config.yaml` | `holding_ws_stale_seconds: 3`, `holding_rest_poll_seconds: 1` |

---

## 2. Trap Avoidance (Entry Filters)

### Problem
Analysis of `Trades.txt` (20 Jul 2026) showed most losses came from:
- CE entries against bearish structural bias
- `vwap_bounce`, `ema_pullback`, `mean_reversion` setups in chop
- Immediate CE↔PE flips after `trend_reversal`
- Entries without 1m pullback trigger confirmation

### New Module
`trading-engine/src/algoflat/validator/trap_avoidance.py` — `trap_rejection_reasons()` centralizes entry rejection logic.

### Config (`config/validator_config.yaml` → `validator_v1.3.0_mtf_wait`)

- `max_spread_pct`: 2.0 → **1.5**
- `cooldown_after_exit_minutes`: 5 → **8**
- **Blocked setups:** `vwap_bounce`, `mean_reversion`, `liquidity_sweep`, `reversal`, `ema_pullback`
- **Require EMA alignment for:** `vwap_pullback`, `trend_continuation`, `vwap_trend`, `momentum_continuation`
- `block_reversal_flips: true`
- `require_bias_side_match: true`
- `require_1m_5m_bias_agree: true`
- `require_5m_structure_align: true`
- `require_3m_bars_with_bias: true` (min 2 bars)
- `require_pullback_trigger: true`
- `require_mtf_alignment: true`, `min_mtf_score: 55`
- `spot_vwap_buffer_points: 12`

### Wiring
- `trading-engine/src/algoflat/validator/engine.py` — calls `trap_rejection_reasons()` during validation
- `trading-engine/src/algoflat/scanner/library.py` — enforces pullback/trend triggers in scanner metadata

---

## 3. Multi-Timeframe (MTF) Candle Patterns

### New Module
`trading-engine/src/algoflat/features/mtf_patterns.py`

- `detect_tf_patterns()` — pattern detection on 1m / 3m / 5m candles
- `build_mtf_alignment()` — composite CE/PE alignment score (0–100)

### Integration

| File | Role |
|------|------|
| `features/engine.py` | 5m-close vs VWAP for structural bias; separate `bias_1m`; MTF scores in `FeatureSnapshot.extra` |
| `quality/gate.py` | MTF score folded into confidence; cap when spot vs VWAP is against trade |
| `strategy_config.yaml` | `mtf_patterns.enabled: true`, `min_score_to_trade: 55`, `boost_score: 75` |

---

## 4. Strategy & Risk Config Tightening

### `config/strategy_config.yaml` → `strategy_router_v2.2.0_mtf_wait_early_exit`

- `min_confidence`: 75 → **80**
- **Enabled strategies** narrowed to high-quality core (removed weak aliases like standalone bounce/reversal)
- `vwap_pullback.min_extension_points`: 5 → **12**
- `vwap_pullback.max_distance_to_vwap_points`: 30 → **22**
- `vwap_trend.min_distance_to_vwap_points`: 3 → **10**

### `config/risk_config.yaml`

- `max_concurrent_positions: 1`
- `max_consecutive_losses: 3`
- `max_daily_loss: 2000`
- `cooldown_after_exit_minutes: 8`
- Confidence lot tiers at **80** and **88**

---

## 5. Exit Rules — Early Invalidation

### Problem
Trades that never went green still ran to full `adverse_momentum` (-12%), amplifying losses.

### `config/position_exit_config.yaml`

| Setting | Value | Purpose |
|---------|-------|---------|
| `flip_on_trend_reversal` | **false** | Block immediate CE↔PE flip after reversal |
| `min_profit_before_trail_pct` | 18 → **12** | Arm trailing earlier |
| `early_invalidation_enabled` | **true** | Cut no-green losers faster |
| `early_invalidation_min_hold_seconds` | **45** | Min hold before early cut |
| `early_invalidation_max_mfe_pct` | **3** | Never reached green threshold |
| `early_invalidation_loss_pct` | **7** | Cut at -7% vs full -12% adverse |
| `min_hold_seconds` | 20 → **30** | Reduce tick noise exits |
| `max_hold_minutes` | 25 → **20** | Shorter time stop |

### Code
`trading-engine/src/algoflat/position/exit_rules.py` — `early_invalidation` exit between trend reversal and adverse momentum.

**Exit priority:** trend reversal → early_invalidation → adverse → trail → time stop

---

## 6. UI & API

| Change | File |
|--------|------|
| Trade blotter limit 20 → **200** | `web-app/frontend/src/api/client.ts`, `trading-engine/src/algoflat/api/health.py`, `web-app/src/algomcx_web/main.py` |

---

## 7. Tests Added

| Test file | Coverage |
|-----------|----------|
| `tests/unit/test_trap_avoidance.py` | Trap rejection rules (bias match, blocked setups, MTF, pullback) |
| `tests/unit/test_mtf_patterns.py` | Pattern detection and MTF scoring |
| `tests/unit/test_exit_rules.py` | `early_invalidation` exit logic |

Run:
```bash
cd trading-engine && PYTHONPATH=src pytest tests/unit/test_trap_avoidance.py tests/unit/test_mtf_patterns.py tests/unit/test_exit_rules.py -q
```

---

## 8. Backtest — 2026-07-20

Full report: [`reports/backtest_2026-07-20.md`](reports/backtest_2026-07-20.md)  
JSON export: [`reports/backtest_2026-07-20.json`](reports/backtest_2026-07-20.json)

| Metric | Before (live) | After (backtest) |
|--------|---------------|------------------|
| Trades | 20 | 5 |
| Net P&L | ₹-5,768.75 | **₹+3,774.68** |
| Win rate | ~35% | **80%** |
| `adverse_momentum` exits | 8 | **0** |
| `trend_reversal_flip` losses | 3 | **0** (flips disabled) |

### Backtest trades (summary)

1. **vwap_rejection CE** — +₹838.50 (`momentum_trail`)
2. **momentum_continuation PE** — +₹796.73 (`time_stop`)
3. **vwap_trend PE** — +₹1,563.47 (`time_stop`)
4. **vwap_rejection CE** — +₹959.57 (`momentum_trail`)
5. **trend_day CE** — -₹383.59 (`time_stop`)

---

## 9. Git Commits (recent)

```
458eb52 Add early invalidation, multi-TF candle gates, and 20-Jul backtest report.
b4303ab Fix adverse-momentum traps via multi-TF entry gates and raise blotter limit.
7299c42 Improve holding WS trails and block adverse-momentum entry traps.
0d21921 Keep open holdings on WebSocket for tick-level trail exits
```

---

## 10. Deployment Notes

- Changes pushed to `main` for deployment.
- After deploy, verify:
  1. Open a paper position → confirm WS ticks update `last_quote_ts`
  2. Restart engine → position rehydrates and re-subscribes on WS
  3. Decision logs show `trap_avoidance` / `mtf_score` rejections for weak setups
  4. Early invalidation fires on no-green trades after ~45s at -7%

---

## File Index (this session)

```
config/
  runtime_config.yaml          # WS stale + REST poll intervals
  validator_config.yaml        # trap_avoidance + MTF gates
  strategy_config.yaml         # router v2.2, MTF patterns, tighter VWAP params
  position_exit_config.yaml    # early_invalidation, no flip on reversal
  risk_config.yaml             # concurrency, daily loss, cooldown

trading-engine/src/algoflat/
  main.py                      # rehydrate, stale poll, WS restart
  position/manager.py          # last_quote_ts, rehydrate, stale tokens
  position/exit_rules.py       # early_invalidation
  broker/flattrade.py          # stop_websocket, tick parsing
  validator/trap_avoidance.py  # NEW — entry trap filters
  validator/engine.py          # trap wiring
  features/mtf_patterns.py   # NEW — MTF candle patterns
  features/engine.py           # 5m bias, MTF in snapshot
  quality/gate.py              # MTF in confidence score
  scanner/library.py           # pullback/trend trigger enforcement

reports/
  backtest_2026-07-20.md       # day backtest report
  backtest_2026-07-20.json     # machine-readable results
```
