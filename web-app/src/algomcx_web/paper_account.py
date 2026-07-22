from __future__ import annotations

from decimal import Decimal

import asyncpg

IST_TRADE_DATE = "(CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date"
DEFAULT_CAPITAL = 50000

_ENSURE_COLUMNS = """
ALTER TABLE daily_risk_state
    ADD COLUMN IF NOT EXISTS starting_capital NUMERIC(12, 4) DEFAULT 50000,
    ADD COLUMN IF NOT EXISTS available_capital NUMERIC(12, 4) DEFAULT 50000,
    ADD COLUMN IF NOT EXISTS deployed_capital NUMERIC(12, 4) DEFAULT 0;
"""

_PREV_EQUITY = f"""
SELECT
    COALESCE(starting_capital, 0) + COALESCE(realized_pnl, 0) AS equity,
    COALESCE(available_capital, 0) + COALESCE(deployed_capital, 0) AS cash_plus_deployed
FROM daily_risk_state
WHERE trade_date < {IST_TRADE_DATE}
ORDER BY trade_date DESC
LIMIT 1
"""

_BOOTSTRAP_ROW = f"""
INSERT INTO daily_risk_state (
    trade_date, starting_capital, available_capital,
    deployed_capital, realized_pnl, trade_count, consecutive_losses
)
VALUES ({IST_TRADE_DATE}, $1, $1, 0, 0, 0, 0)
ON CONFLICT (trade_date) DO UPDATE SET
    starting_capital = COALESCE(daily_risk_state.starting_capital, EXCLUDED.starting_capital),
    available_capital = COALESCE(daily_risk_state.available_capital, EXCLUDED.available_capital),
    deployed_capital = COALESCE(daily_risk_state.deployed_capital, 0),
    updated_at = now()
"""

_FIX_NULL_CAPITAL = f"""
UPDATE daily_risk_state SET
    starting_capital = $1,
    available_capital = COALESCE(available_capital, $1),
    deployed_capital = COALESCE(deployed_capital, 0)
WHERE trade_date = {IST_TRADE_DATE}
  AND (starting_capital IS NULL OR available_capital IS NULL)
"""


async def previous_day_ending_equity(
    conn: asyncpg.Connection,
    fallback: float | Decimal,
) -> Decimal:
    fb = Decimal(str(fallback))
    row = await conn.fetchrow(_PREV_EQUITY)
    if not row:
        return fb
    cash_plus = Decimal(str(row["cash_plus_deployed"] or 0))
    if cash_plus > 0:
        return cash_plus
    equity = Decimal(str(row["equity"] or 0))
    return equity if equity > 0 else fb


async def ensure_paper_account(pool: asyncpg.Pool, capital_inr: float = DEFAULT_CAPITAL) -> None:
    """Seed today's paper row from prior-day ending equity (carry-forward)."""
    async with pool.acquire() as conn:
        await conn.execute(_ENSURE_COLUMNS)
        seed = await previous_day_ending_equity(conn, capital_inr)
        await conn.execute(_BOOTSTRAP_ROW, float(seed))
        await conn.execute(_FIX_NULL_CAPITAL, float(seed))
