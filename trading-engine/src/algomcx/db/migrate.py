from __future__ import annotations

import os
from pathlib import Path

import structlog

from algomcx.db.connection import get_pool

logger = structlog.get_logger(__name__)


def _migrations_dir() -> Path:
    env_path = os.environ.get("MIGRATIONS_DIR")
    if env_path:
        return Path(env_path)
    candidates = [
        Path(__file__).resolve().parents[4] / "db" / "migrations",
        Path("/app/db/migrations"),
    ]
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0]


async def _seed_preapplied_migrations(conn) -> None:
    """Mark migrations already applied by postgres initdb (same files mounted in compose)."""
    has_capital = await conn.fetchval(
        """
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name = 'daily_risk_state'
          AND column_name = 'starting_capital'
        """
    )
    if has_capital:
        await conn.execute(
            """
            INSERT INTO schema_migrations (filename)
            VALUES ('002_paper_account.sql')
            ON CONFLICT (filename) DO NOTHING
            """
        )

    has_underlying_key = await conn.fetchval(
        """
        SELECT 1 FROM pg_constraint c
        JOIN pg_class t ON c.conrelid = t.oid
        WHERE t.relname = 'daily_risk_state'
          AND c.conname = 'daily_risk_state_trade_date_underlying_key'
        """
    )
    if has_underlying_key:
        await conn.execute(
            """
            INSERT INTO schema_migrations (filename)
            VALUES ('003_per_underlying_risk.sql')
            ON CONFLICT (filename) DO NOTHING
            """
        )


async def apply_migrations() -> None:
    migrations_dir = _migrations_dir()
    if not migrations_dir.is_dir():
        logger.warning("migrations_dir_missing", path=str(migrations_dir))
        return

    pool = get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ DEFAULT now()
            )
            """
        )
        has_core = await conn.fetchval(
            """
            SELECT 1 FROM information_schema.tables
            WHERE table_schema = 'public' AND table_name = 'instruments'
            """
        )
        if has_core:
            await conn.execute(
                """
                INSERT INTO schema_migrations (filename)
                VALUES ('001_initial_schema.sql')
                ON CONFLICT (filename) DO NOTHING
                """
            )

        await _seed_preapplied_migrations(conn)

        for path in sorted(migrations_dir.glob("*.sql")):
            applied = await conn.fetchval(
                "SELECT 1 FROM schema_migrations WHERE filename = $1",
                path.name,
            )
            if applied:
                continue
            sql = path.read_text(encoding="utf-8")
            await conn.execute(sql)
            await conn.execute(
                "INSERT INTO schema_migrations (filename) VALUES ($1)",
                path.name,
            )
            logger.info("migration_applied", file=path.name)
