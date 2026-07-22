from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import asyncpg
import httpx
from fastapi import Depends, FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic_settings import BaseSettings, SettingsConfigDict

from algomcx_web.auth import (
    LoginRequest,
    TokenResponse,
    create_access_token,
    require_user,
    verify_credentials,
)
from algomcx_web.paper_account import IST_TRADE_DATE, ensure_paper_account

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"

# Alerts page: user-facing events only (trades + critical system), not ops noise.
IMPORTANT_ALERT_SQL = """
(
  type IN ('trade', 'kill_switch')
  OR title IN (
    'Trade entry',
    'Trade exit',
    'Paper entry filled',
    'Paper exit',
    'Entry blocked by risk',
    'Auto trading ON',
    'Auto trading OFF',
    'Kill switch updated',
    'Flattrade login required',
    'Flattrade API key missing',
    'Paper account reset',
    'Weekly expiry rolled'
  )
)
"""


def _resolve_env_file() -> str | None:
    root = Path(__file__).resolve().parents[3]
    for path in (Path.cwd() / ".env", root / ".env"):
        if path.is_file():
            return str(path)
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=_resolve_env_file(), extra="ignore")
    database_url: str = "postgresql://algoflat:algoflat@localhost:5432/algoflat"
    trading_engine_url: str = "http://127.0.0.1:8001"


settings = Settings()
app = FastAPI(title="Algo-MCX Web", version="0.3.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=True,
)
_pool: asyncpg.Pool | None = None
User = Annotated[str, Depends(require_user)]


@app.on_event("startup")
async def startup() -> None:
    global _pool
    _pool = await asyncpg.create_pool(dsn=settings.database_url, min_size=1, max_size=5)
    await ensure_paper_account(_pool)


@app.on_event("shutdown")
async def shutdown() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


def pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("DB pool not ready")
    return _pool


@app.post("/api/auth/login", response_model=TokenResponse)
async def login(body: LoginRequest, response: Response) -> TokenResponse:
    if not verify_credentials(body.username, body.password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token, expires = create_access_token(body.username)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24,
    )
    return TokenResponse(
        access_token=token,
        username=body.username,
        expires_at=expires.isoformat(),
    )


@app.post("/api/auth/logout")
async def logout(response: Response, _user: User) -> dict[str, bool]:
    response.delete_cookie("access_token")
    return {"ok": True}


@app.get("/api/auth/me")
async def me(user: User) -> dict[str, str]:
    return {"username": user}


@app.get("/health")
async def liveness() -> dict[str, str]:
    """Public liveness probe for Docker / load balancers (no auth)."""
    return {"status": "ok"}


@app.get("/api/health")
async def proxy_health(_user: User) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{settings.trading_engine_url}/health")
            data = resp.json()
        except Exception as exc:
            return {"status": "unreachable", "error": str(exc), "db_ok": False}

    try:
        row = await pool().fetchrow(
            """
            SELECT kill_switch FROM daily_risk_state
            WHERE trade_date = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
            """
        )
        data["kill_switch"] = bool(row["kill_switch"]) if row else False
    except Exception:
        data["kill_switch"] = False
    return data


@app.get("/api/market-summary")
async def market_summary(_user: User) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{settings.trading_engine_url}/market-summary")
            data = resp.json()
        except Exception:
            data = {
                "underlying": "GOLD",
                "spot_ltp": None,
                "session_vwap": None,
                "spot_vs_vwap": None,
                "bias_5m": "NEUTRAL",
                "market_session": "CLOSED",
                "market_open": False,
            }

    row = await pool().fetchrow(
        f"""
        SELECT
            COALESCE(starting_capital, 50000) AS starting_capital,
            COALESCE(available_capital, 50000) AS available_capital,
            COALESCE(deployed_capital, 0) AS deployed_capital,
            COALESCE(realized_pnl, 0) AS today_pnl,
            COALESCE(trade_count, 0) AS trade_count
        FROM daily_risk_state
        WHERE trade_date = {IST_TRADE_DATE}
        """
    )
    if row:
        data["starting_capital"] = float(row["starting_capital"])
        data["available_capital"] = float(row["available_capital"])
        data["deployed_capital"] = float(row["deployed_capital"])
        data["used_margin"] = float(row["deployed_capital"])
        data["today_pnl"] = float(row["today_pnl"])
        data["trade_count"] = int(row["trade_count"])
    else:
        data.setdefault("starting_capital", 50000.0)
        data.setdefault("available_capital", 50000.0)
        data.setdefault("deployed_capital", 0.0)
        data.setdefault("used_margin", 0.0)
        data["today_pnl"] = 0.0
        data["trade_count"] = 0

    # Equity = carried-forward starting + today realized + live unrealized.
    unrealized = float(data.get("unrealized_pnl") or 0)
    data["equity"] = float(data["starting_capital"]) + float(data["today_pnl"]) + unrealized

    stats = await pool().fetchrow(
        f"""
        SELECT
            (SELECT COUNT(*) FROM candidate_signals
             WHERE (ts AT TIME ZONE 'Asia/Kolkata')::date = {IST_TRADE_DATE}) AS candidate_count,
            (
                SELECT COUNT(*) FROM validation_results vr
                JOIN candidate_signals cs ON cs.id = vr.candidate_signal_id
                WHERE (cs.ts AT TIME ZONE 'Asia/Kolkata')::date = {IST_TRADE_DATE} AND NOT vr.passed
            ) AS rejection_count
        """
    )
    if stats:
        data["candidate_count"] = int(stats["candidate_count"] or 0)
        data["rejection_count"] = int(stats["rejection_count"] or 0)

    rejections = await pool().fetch(
        f"""
        SELECT vr.rejection_reasons, cs.tsym
        FROM validation_results vr
        JOIN candidate_signals cs ON cs.id = vr.candidate_signal_id
        WHERE (cs.ts AT TIME ZONE 'Asia/Kolkata')::date = {IST_TRADE_DATE} AND NOT vr.passed
        ORDER BY vr.ts DESC
        LIMIT 5
        """
    )
    data["recent_rejections"] = [
        {"tsym": r["tsym"], "reasons": list(r["rejection_reasons"] or [])}
        for r in rejections
    ]

    unread = await pool().fetchval(
        f"SELECT COUNT(*) FROM notifications WHERE read = FALSE AND {IMPORTANT_ALERT_SQL}"
    )
    data["unread_notifications"] = int(unread or 0)
    return data


@app.get("/api/notifications")
async def list_notifications(
    _user: User,
    limit: int = 50,
    unread_only: bool = False,
) -> list[dict[str, Any]]:
    """Important user alerts only — trade entry/exit and critical system events."""
    clauses = [IMPORTANT_ALERT_SQL]
    if unread_only:
        clauses.append("read = FALSE")
    where = " AND ".join(clauses)
    rows = await pool().fetch(
        f"""
        SELECT id, ts, type, severity, title, message, read
        FROM notifications
        WHERE {where}
        ORDER BY ts DESC
        LIMIT $1
        """,
        max(1, min(limit, 200)),
    )
    return [
        {
            "id": str(r["id"]),
            "ts": r["ts"].isoformat(),
            "type": r["type"],
            "severity": r["severity"],
            "title": r["title"],
            "message": r["message"],
            "read": r["read"],
        }
        for r in rows
    ]


@app.post("/api/notifications/{notification_id}/read")
async def mark_read(notification_id: UUID, _user: User) -> dict[str, bool]:
    await pool().execute(
        "UPDATE notifications SET read = TRUE WHERE id = $1",
        notification_id,
    )
    return {"read": True}


@app.get("/api/trades/today")
async def trades_today(_user: User, limit: int = 100) -> list[dict[str, Any]]:
    """Closed trades for today (session P&L)."""
    return await _fetch_closed_trades(limit=limit, today_only=True)


@app.get("/api/trades")
async def trades_all(
    _user: User,
    limit: int = 500,
    today_only: bool = False,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    """Closed trades with optional IST date filter (from_date / to_date = YYYY-MM-DD)."""
    return await _fetch_closed_trades(
        limit=limit,
        today_only=today_only,
        from_date=from_date,
        to_date=to_date,
    )


@app.get("/api/trades/report")
async def trades_report(
    _user: User,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 2000,
) -> dict[str, Any]:
    """P&L report payload: filtered trades + aggregate stats for Order Book export."""
    trades = await _fetch_closed_trades(
        limit=limit,
        today_only=False,
        from_date=from_date,
        to_date=to_date,
    )
    pnls = [float(t["pnl"]) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    by_reason: dict[str, dict[str, float | int]] = {}
    by_setup: dict[str, dict[str, float | int]] = {}
    by_day: dict[str, dict[str, float | int]] = {}
    for t in trades:
        reason = str(t.get("exit_reason") or "unknown")
        setup = str(t.get("setup_type") or "unknown")
        day = (t["exit_ts"] or "")[:10]
        for bucket, key in ((by_reason, reason), (by_setup, setup)):
            row = bucket.setdefault(key, {"count": 0, "pnl": 0.0})
            row["count"] = int(row["count"]) + 1
            row["pnl"] = float(row["pnl"]) + float(t["pnl"])
        if day:
            # Convert UTC ISO to IST date label via exit_ts already being ISO;
            # use exit date in IST by parsing.
            try:
                from zoneinfo import ZoneInfo

                exit_dt = datetime.fromisoformat(t["exit_ts"].replace("Z", "+00:00"))
                day_ist = exit_dt.astimezone(ZoneInfo("Asia/Kolkata")).date().isoformat()
            except Exception:
                day_ist = day
            row = by_day.setdefault(day_ist, {"count": 0, "pnl": 0.0})
            row["count"] = int(row["count"]) + 1
            row["pnl"] = float(row["pnl"]) + float(t["pnl"])

    total = sum(pnls)
    return {
        "from_date": from_date,
        "to_date": to_date,
        "generated_at": datetime.now(tz=timezone.utc).isoformat(),
        "summary": {
            "trades": len(trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate_pct": (len(wins) / len(pnls) * 100.0) if pnls else 0.0,
            "total_pnl": total,
            "avg_pnl": (total / len(pnls)) if pnls else 0.0,
            "best_trade": max(pnls) if pnls else 0.0,
            "worst_trade": min(pnls) if pnls else 0.0,
            "gross_profit": sum(wins) if wins else 0.0,
            "gross_loss": sum(losses) if losses else 0.0,
        },
        "by_exit_reason": by_reason,
        "by_setup": by_setup,
        "by_day": dict(sorted(by_day.items())),
        "trades": trades,
    }


@app.get("/api/trades/dates")
async def trades_dates(_user: User) -> list[str]:
    """Distinct IST exit dates that have closed trades (newest first)."""
    rows = await pool().fetch(
        """
        SELECT DISTINCT (ct.exit_ts AT TIME ZONE 'Asia/Kolkata')::date AS d
        FROM closed_trades ct
        ORDER BY d DESC
        LIMIT 365
        """
    )
    return [r["d"].isoformat() for r in rows]


async def _fetch_closed_trades(
    *,
    limit: int,
    today_only: bool,
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict[str, Any]]:
    from datetime import date as date_cls

    clauses: list[str] = []
    args: list[Any] = []
    if today_only:
        clauses.append(
            f"(ct.exit_ts AT TIME ZONE 'Asia/Kolkata')::date = {IST_TRADE_DATE}"
        )
    else:
        # Pass real date objects — asyncpg + `$N::date` string casts 500 on some setups.
        if from_date:
            args.append(date_cls.fromisoformat(from_date))
            clauses.append(
                f"(ct.exit_ts AT TIME ZONE 'Asia/Kolkata')::date >= ${len(args)}"
            )
        if to_date:
            args.append(date_cls.fromisoformat(to_date))
            clauses.append(
                f"(ct.exit_ts AT TIME ZONE 'Asia/Kolkata')::date <= ${len(args)}"
            )

    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    args.append(max(1, min(limit, 2000)))
    limit_ph = f"${len(args)}"

    rows = await pool().fetch(
        f"""
        SELECT
            ct.id,
            ct.entry_ts,
            ct.exit_ts,
            ct.entry_price,
            ct.exit_price,
            ct.quantity,
            ct.pnl,
            ct.pnl_pct,
            ct.mfe,
            ct.mae,
            ct.exit_reason,
            ct.setup_type,
            ct.hold_seconds,
            ct.mode,
            p.tsym,
            p.side AS position_side,
            p.instrument_token
        FROM closed_trades ct
        JOIN positions p ON p.id = ct.position_id
        {where}
        ORDER BY ct.exit_ts DESC
        LIMIT {limit_ph}
        """,
        *args,
    )
    lot_default = 65
    out: list[dict[str, Any]] = []
    for r in rows:
        qty = int(r["quantity"])
        lots = qty // lot_default if qty else 0
        out.append(
            {
                "id": str(r["id"]),
                "tsym": r["tsym"],
                "side": r["position_side"],
                "instrument_token": r["instrument_token"],
                "entry_ts": r["entry_ts"].isoformat(),
                "exit_ts": r["exit_ts"].isoformat(),
                "entry_price": float(r["entry_price"]),
                "exit_price": float(r["exit_price"]),
                "quantity": qty,
                "lot_size": lot_default,
                "lots": lots,
                "pnl": float(r["pnl"]),
                "pnl_pct": float(r["pnl_pct"]) if r["pnl_pct"] is not None else None,
                "mfe": float(r["mfe"]) if r["mfe"] is not None else None,
                "mae": float(r["mae"]) if r["mae"] is not None else None,
                "exit_reason": r["exit_reason"],
                "setup_type": r["setup_type"],
                "hold_seconds": r["hold_seconds"],
                "mode": r["mode"],
            }
        )
    return out


@app.get("/api/events/stream")
async def events_stream(_user: User):
    async def generate():
        import asyncio

        while True:
            rows = await pool().fetch(
                f"""
                SELECT type, severity, title, message, ts
                FROM notifications
                WHERE {IMPORTANT_ALERT_SQL}
                ORDER BY ts DESC
                LIMIT 5
                """
            )
            payload = {
                "ts": datetime.now(tz=timezone.utc).isoformat(),
                "notifications": [
                    {**dict(r), "ts": r["ts"].isoformat()} for r in rows
                ],
            }
            yield f"data: {json.dumps(payload, default=str)}\n\n"
            await asyncio.sleep(5)

    return StreamingResponse(generate(), media_type="text/event-stream")


@app.post("/api/control/kill-switch")
async def kill_switch(_user: User, enabled: bool = True) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{settings.trading_engine_url}/control/kill-switch",
            params={"enabled": str(enabled).lower()},
        )
        return resp.json()


@app.post("/api/control/reauth")
async def reauth(_user: User, force: bool = True) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{settings.trading_engine_url}/control/reauth",
            params={"force": str(force).lower()},
        )
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise HTTPException(status_code=resp.status_code, detail=detail)
        return resp.json()


@app.post("/api/control/refresh-universe")
async def refresh_universe(_user: User) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(f"{settings.trading_engine_url}/control/refresh-universe")
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise HTTPException(status_code=resp.status_code, detail=detail)
        return resp.json()


@app.post("/api/control/auto-trade")
async def auto_trade(_user: User, enabled: bool = True) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{settings.trading_engine_url}/control/auto-trade",
            params={"enabled": str(enabled).lower()},
        )
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise HTTPException(status_code=resp.status_code, detail=detail)
        return resp.json()


@app.post("/api/control/sync-missing")
async def sync_missing(_user: User) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(f"{settings.trading_engine_url}/control/sync-missing")
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise HTTPException(status_code=resp.status_code, detail=detail)
        return resp.json()


@app.get("/api/decision-logs")
async def decision_logs(
    _user: User, limit: int = 100, event_type: str | None = None
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": limit}
    if event_type:
        params["event_type"] = event_type
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(
            f"{settings.trading_engine_url}/decision-logs",
            params=params,
        )
        if resp.status_code >= 400:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp.json()


@app.get("/api/trade-blotter")
async def trade_blotter(_user: User, limit: int = 200) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(
                f"{settings.trading_engine_url}/trade-blotter",
                params={"limit": limit},
            )
            return resp.json()
        except Exception:
            return {"open_positions": [], "closed_trades": []}


@app.post("/api/control/repair-paper-account")
async def repair_paper_account(
    _user: User,
    apply: bool = False,
) -> dict[str, Any]:
    """Remove exact duplicate closed trades and rebuild capital carry-forward chain.

    Dry-run by default (apply=false). Pass apply=true to write changes.
    """
    from decimal import Decimal

    base = Decimal("50000")
    find_dupes = """
    WITH ranked AS (
        SELECT
            ct.id AS closed_id,
            ct.position_id,
            p.order_id,
            p.tsym,
            ct.quantity,
            ct.entry_price,
            ct.exit_price,
            ct.pnl,
            ct.entry_ts,
            ct.exit_ts,
            ROW_NUMBER() OVER (
                PARTITION BY
                    p.tsym,
                    ct.quantity,
                    ct.entry_price,
                    ct.exit_price,
                    ct.pnl,
                    date_trunc('second', ct.entry_ts),
                    date_trunc('second', ct.exit_ts)
                ORDER BY ct.id
            ) AS rn
        FROM closed_trades ct
        JOIN positions p ON p.id = ct.position_id
    )
    SELECT * FROM ranked WHERE rn > 1 ORDER BY exit_ts, tsym
    """
    async with pool().acquire() as conn:
        dupes = await conn.fetch(find_dupes)
        removed: list[dict[str, Any]] = []
        if apply and dupes:
            async with conn.transaction():
                for d in dupes:
                    await conn.execute(
                        "DELETE FROM closed_trades WHERE id = $1", d["closed_id"]
                    )
                    await conn.execute(
                        "DELETE FROM positions WHERE id = $1", d["position_id"]
                    )
                    await conn.execute(
                        "DELETE FROM orders WHERE id = $1", d["order_id"]
                    )
                    removed.append(
                        {
                            "tsym": d["tsym"],
                            "pnl": float(d["pnl"]),
                            "exit_ts": d["exit_ts"].isoformat(),
                            "closed_id": str(d["closed_id"]),
                        }
                    )

        days = await conn.fetch(
            "SELECT trade_date, starting_capital, available_capital, "
            "deployed_capital, realized_pnl, trade_count "
            "FROM daily_risk_state ORDER BY trade_date"
        )
        chain: list[dict[str, Any]] = []
        prev_end: Decimal | None = None
        for row in days:
            day = row["trade_date"]
            realized = Decimal(
                str(
                    await conn.fetchval(
                        """
                        SELECT COALESCE(SUM(ct.pnl), 0)
                        FROM closed_trades ct
                        WHERE (ct.exit_ts AT TIME ZONE 'Asia/Kolkata')::date = $1
                        """,
                        day,
                    )
                    or 0
                )
            )
            trade_count = int(
                await conn.fetchval(
                    """
                    SELECT COUNT(*) FROM closed_trades ct
                    WHERE (ct.exit_ts AT TIME ZONE 'Asia/Kolkata')::date = $1
                    """,
                    day,
                )
                or 0
            )
            starting = base if prev_end is None else prev_end
            deployed = Decimal(str(row["deployed_capital"] or 0))
            available = starting + realized - deployed
            entry = {
                "trade_date": str(day),
                "old_starting": float(row["starting_capital"] or 0),
                "new_starting": float(starting),
                "old_realized": float(row["realized_pnl"] or 0),
                "new_realized": float(realized),
                "old_available": float(row["available_capital"] or 0),
                "new_available": float(available),
                "old_trade_count": int(row["trade_count"] or 0),
                "new_trade_count": trade_count,
                "ending_equity": float(starting + realized),
            }
            chain.append(entry)
            if apply:
                await conn.execute(
                    """
                    UPDATE daily_risk_state SET
                        starting_capital = $2,
                        available_capital = $3,
                        realized_pnl = $4,
                        trade_count = $5,
                        updated_at = now()
                    WHERE trade_date = $1
                    """,
                    day,
                    starting,
                    available,
                    realized,
                    trade_count,
                )
            prev_end = starting + realized

    return {
        "ok": True,
        "applied": apply,
        "duplicates_found": len(dupes),
        "duplicates": [
            {
                "tsym": d["tsym"],
                "pnl": float(d["pnl"]),
                "exit_ts": d["exit_ts"].isoformat(),
                "closed_id": str(d["closed_id"]),
            }
            for d in dupes
        ],
        "duplicates_removed": removed,
        "capital_chain": chain,
    }


@app.post("/api/control/reset-paper-account")
async def reset_paper_account(_user: User) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=90.0) as client:
        resp = await client.post(
            f"{settings.trading_engine_url}/control/reset-paper-account"
        )
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise HTTPException(status_code=resp.status_code, detail=detail)
        return resp.json()


@app.post("/api/control/exit-position")
async def exit_position(_user: User, position_id: str) -> dict[str, Any]:
    """Proxy manual square-off at current LTP."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{settings.trading_engine_url}/control/exit-position",
            params={"position_id": position_id},
        )
        if resp.status_code >= 400:
            detail = resp.text
            try:
                detail = resp.json().get("detail", detail)
            except Exception:
                pass
            raise HTTPException(status_code=resp.status_code, detail=detail)
        return resp.json()


@app.get("/api/watchlist")
async def watchlist(_user: User) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=5.0) as client:
        try:
            resp = await client.get(f"{settings.trading_engine_url}/watchlist")
            return resp.json()
        except Exception:
            return {
                "underlying": "GOLD",
                "spot_ltp": None,
                "atm_strike": None,
                "instrument_count": 0,
                "last_quote_ts": None,
                "items": [],
            }


@app.get("/api/quotes/stream")
async def quotes_stream(_user: User) -> StreamingResponse:
    """Proxy live option-chain ticks from the trading engine to the browser."""

    async def generate():
        async with httpx.AsyncClient(timeout=None) as client:
            while True:
                try:
                    async with client.stream(
                        "GET", f"{settings.trading_engine_url}/quotes/stream"
                    ) as resp:
                        async for line in resp.aiter_lines():
                            if line.startswith("data:"):
                                yield f"{line}\n\n"
                except Exception as exc:
                    payload = {
                        "items": [],
                        "feed_mode": "offline",
                        "error": str(exc),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
                    await asyncio.sleep(2)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# React SPA — static assets + client-side routing fallback
if FRONTEND_DIST.is_dir():
    assets_dir = FRONTEND_DIST / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/{full_path:path}")
    async def spa_fallback(full_path: str) -> FileResponse:
        if full_path.startswith("api"):
            raise HTTPException(status_code=404)
        file = FRONTEND_DIST / full_path
        if file.is_file():
            return FileResponse(file)
        return FileResponse(FRONTEND_DIST / "index.html")
