#!/usr/bin/env python3
"""Repair server paper account: remove exact duplicate closed trades + rebuild capital chain.

Default is dry-run (prints what would change). Pass --apply to write.

On the server (Docker, from project directory)::

  docker compose -f docker-compose.yml -f docker-compose.prod.yml run --rm \\
    -v \"$PWD/scripts/repair_paper_account.py:/app/repair_paper_account.py:ro\" \\
    --entrypoint python trading-engine /app/repair_paper_account.py

  # then with --apply

Local (venv)::

  CONFIG_DIR=./config PYTHONPATH=trading-engine/src \\
    trading-engine/.venv/bin/python scripts/repair_paper_account.py --apply

Duplicate definition (matches the 17-Jul live bug):
  same tsym, quantity, entry_price, exit_price, pnl, and entry/exit timestamps
  within the same second — keep the earliest closed_trades.id, remove the rest,
  then rebuild the capital chain from closed_trades.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from decimal import Decimal
from pathlib import Path

# Resolve imports for: repo checkout, or Docker image (/app/src already on PYTHONPATH).
_SCRIPT = Path(__file__).resolve()
_CANDIDATES = [
    _SCRIPT.parent.parent / "trading-engine" / "src",  # repo: scripts/repair_*.py
    Path("/app/src"),  # trading-engine container
    _SCRIPT.parent / "src",
]
for _src in _CANDIDATES:
    if (_src / "algomcx").is_dir():
        sys.path.insert(0, str(_src))
        break

# CONFIG_DIR: prefer env (Docker sets /app/config); else repo ./config
if not os.environ.get("CONFIG_DIR"):
    for _cfg in (
        Path("/app/config"),
        _SCRIPT.parent.parent / "config",
        Path.cwd() / "config",
    ):
        if _cfg.is_dir():
            os.environ["CONFIG_DIR"] = str(_cfg)
            break

from algomcx.config import get_config  # noqa: E402
from algomcx.db.connection import close_pool, init_pool  # noqa: E402


FIND_DUPES = """
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
        (ct.exit_ts AT TIME ZONE 'Asia/Kolkata')::date AS trade_date,
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
SELECT *
FROM ranked
WHERE rn > 1
ORDER BY exit_ts, tsym
"""


async def rebuild_capital_chain(conn, base_capital: Decimal, *, apply: bool) -> list[dict]:
    days = await conn.fetch(
        "SELECT trade_date, starting_capital, available_capital, deployed_capital, "
        "realized_pnl, trade_count FROM daily_risk_state ORDER BY trade_date"
    )
    plan: list[dict] = []
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
                SELECT COUNT(*)
                FROM closed_trades ct
                WHERE (ct.exit_ts AT TIME ZONE 'Asia/Kolkata')::date = $1
                """,
                day,
            )
            or 0
        )
        starting = base_capital if prev_end is None else prev_end
        deployed = Decimal(str(row["deployed_capital"] or 0))
        available = starting + realized - deployed
        plan.append(
            {
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
        )
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
    return plan


async def main() -> None:
    parser = argparse.ArgumentParser(description="Repair paper capital + duplicate trades")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write changes (default: dry-run)",
    )
    args = parser.parse_args()
    apply = args.apply

    config = get_config()
    base = Decimal(str(config.risk.get("account_capital_inr", 50000)))
    await init_pool()
    pool = await init_pool()

    try:
        async with pool.acquire() as conn:
            dupes = await conn.fetch(FIND_DUPES)
            print(f"Duplicate closed trades found: {len(dupes)}")
            for d in dupes:
                print(
                    f"  - {d['tsym']} qty={d['quantity']} "
                    f"entry={d['entry_price']} exit={d['exit_price']} "
                    f"pnl={d['pnl']} entry_ts={d['entry_ts']} "
                    f"exit_ts={d['exit_ts']} closed_id={d['closed_id']}"
                )

            if apply and dupes:
                async with conn.transaction():
                    for d in dupes:
                        closed_id = d["closed_id"]
                        position_id = d["position_id"]
                        order_id = d["order_id"]
                        await conn.execute(
                            "DELETE FROM closed_trades WHERE id = $1", closed_id
                        )
                        await conn.execute(
                            "DELETE FROM positions WHERE id = $1", position_id
                        )
                        await conn.execute(
                            "DELETE FROM orders WHERE id = $1", order_id
                        )
                        print(f"  deleted closed={closed_id} position={position_id}")

            print("\nCapital chain rebuild:")
            plan = await rebuild_capital_chain(conn, base, apply=apply)
            for p in plan:
                print(
                    f"  {p['trade_date']}: start "
                    f"{p['old_starting']:.2f}→{p['new_starting']:.2f}  "
                    f"realized {p['old_realized']:.2f}→{p['new_realized']:.2f}  "
                    f"avail {p['old_available']:.2f}→{p['new_available']:.2f}  "
                    f"trades {p['old_trade_count']}→{p['new_trade_count']}  "
                    f"end_equity={p['ending_equity']:.2f}"
                )

            if not apply:
                print("\nDry-run only. Re-run with --apply to write changes.")
            else:
                print("\nApplied.")
    finally:
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
