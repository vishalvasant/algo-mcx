from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from algomcx.broker.auth import resolve_session
from algomcx.broker.credentials import (
    load_flattrade_config,
    load_flattrade_credentials_status,
    save_flattrade_credentials,
)
from algomcx.config import get_config
from algomcx.db.connection import get_pool
from algomcx.db.paper_account import ensure_paper_account
from algomcx.runtime.trading_mode import get_execution_mode, is_live_execution
from algomcx.notifications.telegram import get_telegram_notifier, maybe_send_telegram_alert
from algomcx.symbols_util import list_underlyings

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
    db_ok = False
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False

    cfg = await load_flattrade_config()
    session = resolve_session(cfg)
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
        "trading_mode": get_execution_mode(),
        "db_ok": db_ok,
        "broker_connected": _engine_state.get("broker_connected", False),
        "flattrade_session": session_info,
        "flattrade_credentials": {
            "configured": cfg.has_api_credentials(),
            "has_auto_login": cfg.has_auto_login(),
        },
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
    await maybe_send_telegram_alert(
        type_="kill_switch",
        severity="critical" if enabled else "info",
        title="Kill switch updated",
        message=f"Kill switch {'enabled' if enabled else 'disabled'}",
    )
    _engine_state["kill_switch"] = enabled
    return {"kill_switch": enabled}


@app.post("/control/trading-mode")
async def trading_mode(mode: str) -> dict[str, Any]:
    if _engine_app is None:
        raise HTTPException(status_code=503, detail="Trading engine not ready")
    normalized = str(mode).lower()
    if normalized not in ("paper", "live"):
        raise HTTPException(status_code=400, detail="mode must be paper or live")
    try:
        return await _engine_app.set_execution_mode_runtime(normalized)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


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


class FlattradeCredentialsBody(BaseModel):
    user_id: str | None = None
    api_key: str | None = None
    api_secret: str | None = None
    password: str | None = None
    totp_secret: str | None = None
    redirect_url: str | None = None


@app.get("/control/flattrade/credentials")
async def flattrade_credentials_get() -> dict[str, Any]:
    return await load_flattrade_credentials_status()


@app.put("/control/flattrade/credentials")
async def flattrade_credentials_put(body: FlattradeCredentialsBody) -> dict[str, Any]:
    try:
        return await save_flattrade_credentials(
            body.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/control/telegram/status")
async def telegram_status() -> dict[str, Any]:
    notifier = get_telegram_notifier()
    await notifier.load_chat_id_from_db()
    bot = await notifier.get_bot_info()
    bot_user = (bot.get("result") or {}) if bot.get("ok") else {}
    return {
        **notifier.status(),
        "bot": {
            "ok": bool(bot.get("ok")),
            "username": bot_user.get("username"),
            "name": bot_user.get("first_name"),
        },
    }


@app.post("/control/telegram/link")
async def telegram_link() -> dict[str, Any]:
    """Link Telegram chat after user taps Start on the bot."""
    notifier = get_telegram_notifier()
    if not notifier.configured:
        raise HTTPException(status_code=400, detail="TELEGRAM_BOT_TOKEN not configured")
    chat_id = await notifier.discover_chat_from_updates()
    if not chat_id:
        bot = await notifier.get_bot_info()
        username = (bot.get("result") or {}).get("username")
        hint = (
            f"Open Telegram, search @{username}, tap Start, then call this endpoint again."
            if username
            else "Open Telegram, start your bot, then call this endpoint again."
        )
        raise HTTPException(status_code=404, detail=hint)
    return {"ok": True, "chat_id": chat_id, **notifier.status()}


@app.post("/control/telegram/test")
async def telegram_test() -> dict[str, Any]:
    notifier = get_telegram_notifier()
    result = await notifier.send_test()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result)
    return result


@app.get("/decision-logs")
async def decision_logs(
    limit: int = 25,
    offset: int = 0,
    event_type: str | None = None,
) -> dict[str, Any]:
    """Strategy decision + entry skip logs for the Decision Logs UI (paginated)."""
    allowed = {"strategy_decision", "entry_skipped", "manual_sync"}
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    if event_type and event_type not in allowed:
        raise HTTPException(status_code=400, detail=f"Invalid event_type: {event_type}")
    types = [event_type] if event_type else sorted(allowed)
    pool = get_pool()
    async with pool.acquire() as conn:
        total = int(
            await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM system_events
                WHERE event_type = ANY($1::text[])
                """,
                types,
            )
            or 0
        )
        rows = await conn.fetch(
            """
            SELECT id, ts, event_type, severity, message, metadata
            FROM system_events
            WHERE event_type = ANY($1::text[])
            ORDER BY ts DESC
            LIMIT $2 OFFSET $3
            """,
            types,
            limit,
            offset,
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
        "total": total,
        "limit": limit,
        "offset": offset,
        "events": events,
    }


@app.get("/decision-logs/summary")
async def decision_logs_summary(minutes: int = 60) -> dict[str, Any]:
    """Aggregate NO_TRADE / regime / confidence reasons for debugging."""
    minutes = max(5, min(minutes, 480))
    pool = get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
              COALESCE(
                metadata->>'selected_reason',
                metadata->>'selected_strategy',
                message
              ) AS reason,
              COALESCE(metadata->>'scan_underlying', '—') AS underlying,
              COALESCE(metadata->'regime'->>'primary', '—') AS regime,
              COUNT(*)::int AS count,
              MAX(ts) AS last_seen
            FROM system_events
            WHERE event_type = 'strategy_decision'
              AND ts > NOW() - ($1::int * INTERVAL '1 minute')
            GROUP BY 1, 2, 3
            ORDER BY count DESC, last_seen DESC
            LIMIT 40
            """,
            minutes,
        )
        latest = await conn.fetchrow(
            """
            SELECT ts, metadata, message
            FROM system_events
            WHERE event_type = 'strategy_decision'
            ORDER BY ts DESC
            LIMIT 1
            """
        )
        stale_count = int(
            await conn.fetchval(
                """
                SELECT COUNT(*)::int FROM system_events
                WHERE event_type = 'strategy_decision'
                  AND ts > NOW() - ($1::int * INTERVAL '1 minute')
                  AND (
                    metadata->>'selected_reason' = 'stale_candle_feed'
                    OR message = 'NO_TRADE'
                       AND metadata->>'selected_reason' = 'stale_candle_feed'
                  )
                """,
                minutes,
            )
            or 0
        )
    latest_meta: dict[str, Any] = {}
    if latest:
        raw = latest["metadata"]
        if isinstance(raw, str):
            try:
                latest_meta = json.loads(raw)
            except Exception:
                latest_meta = {}
        elif isinstance(raw, dict):
            latest_meta = raw

    return {
        "window_minutes": minutes,
        "stale_feed_decisions": stale_count,
        "latest_scan": {
            "ts": latest["ts"].isoformat() if latest else None,
            "underlying": latest_meta.get("scan_underlying"),
            "strategy": latest_meta.get("selected_strategy"),
            "reason": latest_meta.get("selected_reason") or (latest["message"] if latest else None),
            "confidence": latest_meta.get("confidence"),
            "regime": (latest_meta.get("regime") or {}).get("primary")
            if isinstance(latest_meta.get("regime"), dict)
            else None,
            "trade_allowed": latest_meta.get("trade_allowed"),
            "candles_stale": latest_meta.get("candles_stale"),
        },
        "reasons": [
            {
                "reason": r["reason"],
                "underlying": r["underlying"],
                "regime": r["regime"],
                "count": r["count"],
                "last_seen": r["last_seen"].isoformat() if r["last_seen"] else None,
            }
            for r in rows
        ],
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
            "underlying": get_config().symbols.get("underlying", "GOLD"),
            "spot_ltp": None,
            "atm_strike": None,
            "instrument_count": 0,
            "last_quote_ts": None,
            "items": [],
        }
    snapshot = _engine_app.get_watchlist_snapshot()
    snapshot["last_quote_ts"] = _engine_state.get("last_quote_ts")
    return snapshot


@app.get("/chart/candles")
async def chart_candles(
    underlying: str = "GOLD",
    interval: str = "15m",
    days: int = 30,
    token: str | None = None,
    exchange: str | None = None,
    tsym: str | None = None,
) -> dict[str, Any]:
    """OHLC for dashboard chart (1m / 3m / 5m / 15m) with DB history."""
    if _engine_app is None:
        return {"underlying": underlying.upper(), "interval": interval, "bars": []}
    iv = interval.lower().strip()
    if iv in ("1m", "1"):
        bar_minutes = 1
    elif iv in ("3m", "3"):
        bar_minutes = 3
    elif iv in ("5m", "5"):
        bar_minutes = 5
    else:
        bar_minutes = 15
    day_count = max(1, min(days, 365))
    if token:
        return await _engine_app.get_contract_chart_bars(
            token=token,
            exchange=exchange or "MCX",
            tsym=tsym or "",
            minutes=bar_minutes,
            days=day_count,
        )
    return await _engine_app.get_underlying_chart_bars(
        underlying,
        minutes=bar_minutes,
        days=day_count,
    )


@app.get("/quotes/stream")
async def quotes_stream() -> StreamingResponse:
    """SSE stream of option-chain snapshots for the live ticker UI."""

    async def generate():
        cfg = get_config()
        interval_ms = int(cfg.runtime.get("watchlist_stream_interval_ms", 250))
        interval = max(0.1, interval_ms / 1000.0)
        while True:
            if _engine_app is None:
                payload = {"items": [], "feed_mode": "offline"}
            else:
                payload = _engine_app.get_watchlist_snapshot()
                payload["last_quote_ts"] = _engine_state.get("last_quote_ts")
                payload["ws_open"] = bool(getattr(_engine_app.broker, "websocket_open", False))
            yield f"data: {json.dumps(payload, default=str)}\n\n"
            if _engine_app is None:
                await asyncio.sleep(interval)
                continue
            tick = _engine_app.watchlist_tick
            tick.clear()
            try:
                await asyncio.wait_for(tick.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

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
    summary["trading_mode"] = get_execution_mode()
    try:
        from algomcx.symbols_util import uses_pooled_capital

        if is_live_execution():
            limits = await _engine_app.live_account_limits()
            summary["starting_capital"] = float(limits.cash)
            summary["available_capital"] = float(limits.available)
            summary["deployed_capital"] = float(limits.margin_used)
            summary["used_margin"] = float(limits.margin_used)
            summary["broker_cash"] = float(limits.cash)
            summary["broker_collateral"] = float(limits.collateral)
            snaps = [await _engine_app.orchestrator.risk.ensure_daily_state()]
            summary["today_pnl"] = float(sum(s.realized_pnl for s in snaps))
            summary["trade_count"] = sum(s.trade_count for s in snaps)
            summary["consecutive_losses"] = max((s.consecutive_losses for s in snaps), default=0)
            summary["kill_switch"] = any(s.kill_switch for s in snaps)
            summary["entries_blocked"] = any(s.entries_blocked for s in snaps)
            block_reasons = [s.block_reason for s in snaps if s.block_reason]
            summary["block_reason"] = block_reasons[0] if block_reasons else None
            summary["auto_trade_enabled"] = not summary["entries_blocked"]
        else:
            if uses_pooled_capital(_engine_app.config):
                snaps = [await _engine_app.orchestrator.risk.ensure_daily_state()]
            else:
                snaps = []
                for row in list_underlyings(_engine_app.config):
                    sym = str(row.get("symbol", "")).upper()
                    if sym:
                        snaps.append(await _engine_app.orchestrator.risk.ensure_daily_state(sym))
                if not snaps:
                    snaps = [await _engine_app.orchestrator.risk.ensure_daily_state()]

            summary["starting_capital"] = float(sum(s.starting_capital for s in snaps))
            summary["available_capital"] = float(sum(s.available_capital for s in snaps))
            summary["deployed_capital"] = float(sum(s.deployed_capital for s in snaps))
            summary["used_margin"] = summary["deployed_capital"]
            summary["today_pnl"] = float(sum(s.realized_pnl for s in snaps))
            summary["trade_count"] = sum(s.trade_count for s in snaps)
            summary["consecutive_losses"] = max((s.consecutive_losses for s in snaps), default=0)
            summary["kill_switch"] = any(s.kill_switch for s in snaps)
            summary["entries_blocked"] = any(s.entries_blocked for s in snaps)
            block_reasons = [s.block_reason for s in snaps if s.block_reason]
            summary["block_reason"] = block_reasons[0] if block_reasons else None
            summary["auto_trade_enabled"] = not summary["entries_blocked"]

        summary["scan_interval_seconds"] = int(
            _engine_app.config.runtime.get("scan_interval_seconds", 10)
        )
        risk_cfg = _engine_app.config.risk
        sizing = risk_cfg.get("confidence_lot_sizing") or {}
        summary["max_daily_loss"] = float(risk_cfg.get("max_daily_loss", 0))
        summary["max_deployed_pct_of_equity"] = float(
            risk_cfg.get("max_deployed_pct_of_equity", 85)
        )
        summary["trading_limits"] = {
            "max_trades_per_day": int(risk_cfg.get("max_trades_per_day", 0)),
            "max_trades_per_day_label": "unlimited"
            if int(risk_cfg.get("max_trades_per_day", 0)) <= 0
            else str(risk_cfg.get("max_trades_per_day")),
            "cooldown_after_exit_minutes": int(
                risk_cfg.get("cooldown_after_exit_minutes", 3)
            ),
            "max_concurrent_positions_per_index": int(
                risk_cfg.get("max_concurrent_positions_per_index", 0)
            ),
            "max_daily_loss_inr": float(risk_cfg.get("max_daily_loss", 0)),
            "max_consecutive_losses": int(risk_cfg.get("max_consecutive_losses", 0)),
            "min_router_confidence": int(
                _engine_app.config.strategy.get("router", {}).get("min_confidence", 80)
            ),
            "use_pooled_capital": bool(risk_cfg.get("use_pooled_capital", True)),
            "account_capital_inr": float(risk_cfg.get("account_capital_inr", 50000)),
            "confidence_lot_sizing": {
                "enabled": bool(sizing.get("enabled", False)),
                "mode": str(sizing.get("mode", "dynamic")),
                "min_capital_pct": float(sizing.get("min_capital_pct", 30)),
                "max_capital_pct": float(sizing.get("max_capital_pct", 90)),
                "aggressive_deploy_min_confidence": int(
                    sizing.get("aggressive_deploy_min_confidence", 90)
                ),
                "max_lots": int(sizing.get("max_lots", 0)),
            },
        }
        summary["has_open_position"] = _engine_app.orchestrator.positions.has_open_position
        summary["open_position_count"] = _engine_app.orchestrator.positions.open_count
        unrealized = _engine_app.orchestrator.positions.unrealized_pnl(
            _engine_app.option_data
        )
        summary["unrealized_pnl"] = float(unrealized)
        if is_live_execution():
            summary["equity"] = float(
                summary.get("starting_capital", 0)
                + summary.get("broker_collateral", 0)
                + summary.get("today_pnl", 0)
                + unrealized
            )
        else:
            summary["equity"] = float(summary["starting_capital"] + summary["today_pnl"] + unrealized)
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
