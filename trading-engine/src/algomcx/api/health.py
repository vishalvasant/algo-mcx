from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse

from algomcx.broker.auth import resolve_session
from algomcx.config import get_config
from algomcx.db.connection import get_pool
from algomcx.db.paper_account import ensure_paper_account
from algomcx.models.events import CandleInterval

app = FastAPI(title="Algo-MCX Trading Engine", version="0.1.0")
_engine_state: dict[str, Any] = {"status": "starting"}


def set_engine_state(state: dict[str, Any]) -> None:
    _engine_state.update(state)


def get_engine_state() -> dict[str, Any]:
    return dict(_engine_state)


_engine_app: Any = None


def set_engine_app(app: Any) -> None:
    global _engine_app
    _engine_app = app


@app.get("/health")
async def health() -> dict[str, Any]:
    config = get_config()
    db_ok = False
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False

    session = resolve_session(config.env)
    session_info = None
    if session:
        session_info = {
            "user_id": session.user_id,
            "source": session.source,
            "expires_at": session.expires_at.isoformat(),
            "valid": session.is_valid,
        }

    return {
        "status": _engine_state.get("status", "unknown"),
        "trading_mode": config.env.trading_mode,
        "db_ok": db_ok,
        "broker_connected": _engine_state.get("broker_connected", False),
        "flattrade_session": session_info,
        "spot_ltp": _engine_state.get("spot_ltp"),
        "instrument_count": _engine_state.get("instrument_count", 0),
        "last_quote_ts": _engine_state.get("last_quote_ts"),
        "ts": datetime.now(tz=timezone.utc).isoformat(),
    }


@app.post("/control/kill-switch")
async def kill_switch(enabled: bool = True) -> dict[str, Any]:
    from zoneinfo import ZoneInfo

    pool = get_pool()
    await ensure_paper_account()
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO daily_risk_state (
                trade_date, kill_switch, entries_blocked, block_reason,
                starting_capital, available_capital, deployed_capital
            )
            VALUES ($1, $2, $3, $4, 50000, 50000, 0)
            ON CONFLICT (trade_date) DO UPDATE SET
                kill_switch = EXCLUDED.kill_switch,
                entries_blocked = EXCLUDED.entries_blocked,
                block_reason = EXCLUDED.block_reason,
                updated_at = now()
            """,
            today,
            enabled,
            enabled,
            "manual_kill_switch" if enabled else None,
        )
        await conn.execute(
            """
            INSERT INTO notifications (type, severity, title, message)
            VALUES ($1, $2, $3, $4)
            """,
            "kill_switch",
            "critical" if enabled else "info",
            "Kill switch updated",
            f"Kill switch {'enabled' if enabled else 'disabled'}",
        )
    _engine_state["kill_switch"] = enabled
    return {"kill_switch": enabled}


@app.post("/control/auto-trade")
async def auto_trade(enabled: bool = True) -> dict[str, Any]:
    """Pause/resume new entries (scans + decision logs continue)."""
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    snap = await _engine_app.orchestrator.risk.set_auto_trade(enabled)
    return {
        "auto_trade_enabled": snap.auto_trade_enabled,
        "kill_switch": snap.kill_switch,
        "entries_blocked": snap.entries_blocked,
        "block_reason": snap.block_reason,
    }


@app.post("/control/sync-missing")
async def sync_missing() -> dict[str, Any]:
    """Pull only missing universe/candles/quotes from Flattrade into DB."""
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    try:
        return await _engine_app.sync_missing_data()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/decision-logs")
async def decision_logs(limit: int = 100, event_type: str | None = None) -> dict[str, Any]:
    """Strategy decision + entry skip logs for the Decision Logs UI."""
    limit = max(1, min(limit, 500))
    pool = get_pool()
    types = (
        [event_type]
        if event_type
        else ["strategy_decision", "entry_skipped", "manual_sync"]
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT id, ts, event_type, severity, message, metadata
            FROM system_events
            WHERE event_type = ANY($1::text[])
            ORDER BY ts DESC
            LIMIT $2
            """,
            types,
            limit,
        )
        count_today = int(
            await conn.fetchval(
                """
                SELECT COUNT(*) FROM system_events
                WHERE event_type = 'strategy_decision'
                  AND (ts AT TIME ZONE 'Asia/Kolkata')::date
                      = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                """
            )
            or 0
        )
    events = []
    for r in rows:
        meta = r["metadata"]
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        events.append(
            {
                "id": str(r["id"]),
                "ts": r["ts"].isoformat(),
                "event_type": r["event_type"],
                "severity": r["severity"],
                "message": r["message"],
                "metadata": meta or {},
            }
        )
    scan_interval = 10
    if _engine_app is not None:
        scan_interval = int(_engine_app.config.runtime.get("scan_interval_seconds", 10))
    return {
        "scan_interval_seconds": scan_interval,
        "decisions_today": count_today,
        "events": events,
    }


@app.post("/control/reauth")
async def reauth(force: bool = True) -> dict[str, Any]:
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    try:
        return await _engine_app.reauthenticate(force=force)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/control/refresh-universe")
async def refresh_universe() -> dict[str, Any]:
    """Force reload of weekly option chain (Pi API or Flattrade scripmaster) into DB."""
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    try:
        ok = await _engine_app._refresh_universe(reason="manual_refresh")
        universe = _engine_app._universe
        return {
            "ok": ok,
            "expiry_symbol": universe.expiry_symbol if universe else None,
            "instrument_count": len(universe.instruments) if universe else 0,
            "atm_strike": float(universe.atm_strike) if universe else None,
            "atm_ce": universe.atm_ce.tsym if universe and universe.atm_ce else None,
            "atm_pe": universe.atm_pe.tsym if universe and universe.atm_pe else None,
        }
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/trade-blotter")
async def trade_blotter(limit: int = 200) -> dict[str, Any]:
    if _engine_app is None:
        return {"open_positions": [], "closed_trades": []}
    snap = _engine_app.get_watchlist_snapshot()
    closed = await _engine_app.get_trade_blotter(limit=limit)
    return {
        "open_positions": snap.get("open_positions", []),
        "closed_trades": closed,
    }


@app.post("/control/reset-paper-account")
async def reset_paper_account() -> dict[str, Any]:
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    try:
        return await _engine_app.reset_paper_account()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/control/exit-position")
async def exit_position(position_id: str) -> dict[str, Any]:
    """Manually square-off an open position at current option LTP."""
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    from decimal import Decimal
    from uuid import UUID

    try:
        pid = UUID(position_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid position_id") from exc

    pos = next(
        (
            p
            for p in _engine_app.orchestrator.positions.open_positions
            if p.position_id == pid
        ),
        None,
    )
    if pos is None:
        raise HTTPException(status_code=404, detail="Position not open")

    state = _engine_app.option_data.get(pos.instrument_token)
    ltp = state.ltp if state and state.ltp is not None else None
    try:
        return await _engine_app.orchestrator.positions.manual_exit(
            pid, Decimal(str(ltp)) if ltp is not None else None
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="Position not open") from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/watchlist")
async def watchlist() -> dict[str, Any]:
    if _engine_app is None:
        return {
            "underlying": get_config().symbols.get("underlying", "NIFTY"),
            "spot_ltp": None,
            "atm_strike": None,
            "instrument_count": 0,
            "last_quote_ts": None,
            "items": [],
        }
    snapshot = _engine_app.get_watchlist_snapshot()
    snapshot["last_quote_ts"] = _engine_state.get("last_quote_ts")
    return snapshot


@app.get("/quotes/stream")
async def quotes_stream() -> StreamingResponse:
    """SSE stream of option-chain snapshots for the live ticker UI."""

    async def generate():
        cfg = get_config()
        interval_ms = int(cfg.runtime.get("watchlist_stream_interval_ms", 750))
        interval = max(0.25, interval_ms / 1000.0)
        while True:
            if _engine_app is None:
                payload = {"items": [], "feed_mode": "offline"}
            else:
                payload = _engine_app.get_watchlist_snapshot()
                payload["last_quote_ts"] = _engine_state.get("last_quote_ts")
                payload["ws_open"] = bool(getattr(_engine_app.broker, "websocket_open", False))
            yield f"data: {json.dumps(payload, default=str)}\n\n"
            await asyncio.sleep(interval)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/market-summary")
async def market_summary() -> dict[str, Any]:
    if _engine_app is None:
        return {
            "underlying": "NIFTY",
            "spot_ltp": None,
            "session_vwap": None,
            "spot_vs_vwap": None,
            "atm_strike": None,
            "bias_5m": "NEUTRAL",
            "market_session": "CLOSED",
            "market_open": False,
            "strategy": "vwap_reclaim",
            "trading_mode": "paper",
            "ist_time": datetime.now(tz=timezone.utc).isoformat(),
            "starting_capital": 50000.0,
            "available_capital": 50000.0,
            "deployed_capital": 0.0,
            "today_pnl": 0.0,
            "trade_count": 0,
            "has_open_position": False,
        }
    summary = _engine_app.get_market_summary()
    try:
        snap = await _engine_app.orchestrator.risk.ensure_daily_state()
        summary["starting_capital"] = float(snap.starting_capital)
        summary["available_capital"] = float(snap.available_capital)
        summary["deployed_capital"] = float(snap.deployed_capital)
        summary["used_margin"] = float(snap.deployed_capital)
        summary["today_pnl"] = float(snap.realized_pnl)
        summary["trade_count"] = snap.trade_count
        summary["consecutive_losses"] = snap.consecutive_losses
        summary["kill_switch"] = snap.kill_switch
        summary["entries_blocked"] = snap.entries_blocked
        summary["block_reason"] = snap.block_reason
        summary["auto_trade_enabled"] = snap.auto_trade_enabled
        summary["scan_interval_seconds"] = int(
            _engine_app.config.runtime.get("scan_interval_seconds", 10)
        )
        summary["has_open_position"] = _engine_app.orchestrator.positions.has_open_position
        summary["open_position_count"] = _engine_app.orchestrator.positions.open_count
        unrealized = _engine_app.orchestrator.positions.unrealized_pnl(
            _engine_app.option_data
        )
        summary["unrealized_pnl"] = float(unrealized)
        summary["equity"] = float(snap.equity + unrealized)
        open_positions = []
        for pos in _engine_app.orchestrator.positions.open_positions:
            state = _engine_app.option_data.get(pos.instrument_token)
            ltp = state.ltp if state and state.ltp is not None else pos.entry_price
            open_positions.append(
                {
                    "position_id": str(pos.position_id),
                    "tsym": pos.tsym,
                    "side": pos.option_side,
                    "quantity": pos.quantity,
                    "entry_price": float(pos.entry_price),
                    "entry_ts": pos.entry_ts.isoformat(),
                    "current_ltp": float(ltp),
                    "unrealized_pnl": float((ltp - pos.entry_price) * pos.quantity),
                    "premium_deployed": float(pos.premium_deployed),
                    "setup_type": pos.setup_type,
                }
            )
        summary["open_positions"] = open_positions
        if open_positions:
            summary["open_position"] = open_positions[0]
        summary["auto_trading_active"] = (
            summary.get("market_open", False)
            and snap.auto_trade_enabled
        )
        pool = get_pool()
        async with pool.acquire() as conn:
            summary["candidate_count"] = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM candidate_signals
                    WHERE ts::date = CURRENT_DATE
                    """
                )
                or 0
            )
            summary["rejection_count"] = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM validation_results vr
                    JOIN candidate_signals cs ON cs.id = vr.candidate_signal_id
                    WHERE cs.ts::date = CURRENT_DATE AND NOT vr.passed
                    """
                )
                or 0
            )
            rows = await conn.fetch(
                """
                SELECT vr.rejection_reasons, cs.tsym
                FROM validation_results vr
                JOIN candidate_signals cs ON cs.id = vr.candidate_signal_id
                WHERE cs.ts::date = CURRENT_DATE AND NOT vr.passed
                ORDER BY vr.ts DESC
                LIMIT 5
                """
            )
            summary["recent_rejections"] = [
                {
                    "tsym": r["tsym"],
                    "reasons": list(r["rejection_reasons"] or []),
                }
                for r in rows
            ]
    except Exception:
        pass
    return summary


@app.get("/strategy-state")
async def strategy_state() -> dict[str, Any]:
    if _engine_app is None:
        return {"error": "engine_not_ready"}
    orch = _engine_app.orchestrator
    await _engine_app.market_data.refresh_session_candles(force=True)
    features = orch.features.compute()
    m1 = _engine_app.market_data.candles(CandleInterval.M1)
    m3 = _engine_app.market_data.candles(CandleInterval.M3)
    m5 = _engine_app.market_data.candles(CandleInterval.M5)
    is_expiry = orch._is_expiry_day()
    regime = orch.regime.classify(features, m1, m5, is_expiry_day=is_expiry)
    universe = _engine_app._universe
    inst_token = None
    if features.bias_5m.value == "bullish" and universe and universe.atm_ce:
        inst_token = universe.atm_ce.token
    elif features.bias_5m.value == "bearish" and universe and universe.atm_pe:
        inst_token = universe.atm_pe.token
    option = _engine_app.option_data.get(inst_token) if inst_token else None
    decision = None
    signal = None
    if universe:
        options = {"vwap_reclaim": option, "vwap_pullback": option, "vwap_trend": option}
        decision, signal = orch.router.route(features, regime, universe, options)
    return {
        "market_open": _engine_app._is_market_open(),
        "feed_mode": _engine_app._feed_mode,
        "ws_started": _engine_app._ws_started,
        "ws_open": bool(getattr(_engine_app.broker, "websocket_open", False)),
        "candle_counts": {"m1": len(m1), "m3": len(m3), "m5": len(m5)},
        "last_m1_close": float(m1[-1].close) if m1 else None,
        "last_m3_close": float(m3[-1].close) if m3 else None,
        "features": {
            "spot": float(features.nifty_spot) if features.nifty_spot else None,
            "vwap": float(features.session_vwap) if features.session_vwap else None,
            "bias": features.bias_5m.value,
            "setup_3m": features.setup_3m,
            "trigger_1m": features.trigger_1m,
            "setup_vwap_pullback": (features.extra or {}).get("setup_vwap_pullback"),
            "trigger_vwap_pullback": (features.extra or {}).get("trigger_vwap_pullback"),
            "distance_to_vwap_points": (features.extra or {}).get("distance_to_vwap_points"),
            "structure_5m": (features.extra or {}).get("structure_5m"),
        },
        "regime": regime.model_dump(mode="json") if regime else None,
        "decision": decision.model_dump(mode="json") if decision else None,
        "would_signal": signal is not None,
        "signal_tsym": signal.tsym if signal else None,
        "signal_setup": signal.setup_type if signal else None,
        "signal_confidence": signal.confidence if signal else None,
        "option_ltp": float(option.ltp) if option and option.ltp else None,
        "is_expiry_day": is_expiry,
    }
