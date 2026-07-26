from __future__ import annotations

import asyncio
from typing import Literal

import structlog

from algomcx.db.connection import get_pool

logger = structlog.get_logger(__name__)

ExecutionMode = Literal["paper", "live"]
_MODE: ExecutionMode = "paper"
_lock = asyncio.Lock()


def init_execution_mode(default: str) -> ExecutionMode:
    global _MODE
    mode = (default or "paper").lower()
    _MODE = "live" if mode == "live" else "paper"
    return _MODE


def get_execution_mode() -> ExecutionMode:
    return _MODE


def is_live_execution() -> bool:
    return _MODE == "live"


def is_paper_execution() -> bool:
    return _MODE == "paper"


async def load_execution_mode_from_db(default: str) -> ExecutionMode:
    global _MODE
    init_execution_mode(default)
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT value FROM engine_settings WHERE key = 'execution_mode'"
            )
        if row and row["value"] in ("paper", "live"):
            _MODE = row["value"]
            logger.info("execution_mode_loaded", mode=_MODE)
    except Exception:
        logger.exception("execution_mode_load_failed")
    return _MODE


async def persist_execution_mode(mode: ExecutionMode) -> None:
    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO engine_settings (key, value, updated_at)
            VALUES ('execution_mode', $1, now())
            ON CONFLICT (key) DO UPDATE SET
                value = EXCLUDED.value,
                updated_at = now()
            """,
            mode,
        )


async def set_execution_mode(mode: str) -> ExecutionMode:
    global _MODE
    normalized: ExecutionMode = "live" if str(mode).lower() == "live" else "paper"
    async with _lock:
        _MODE = normalized
        await persist_execution_mode(normalized)
    logger.info("execution_mode_changed", mode=normalized)
    return normalized
