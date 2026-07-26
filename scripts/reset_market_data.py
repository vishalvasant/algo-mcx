#!/usr/bin/env python3
"""Reset stale market candles/instruments so spot and option chain rebuild cleanly."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Clear stale candles and option instruments")
    parser.add_argument(
        "--keep-tokens",
        nargs="*",
        default=[],
        help="Instrument tokens to keep (default: wipe all candle rows)",
    )
    args = parser.parse_args()

    from algomcx.db.connection import close_pool, get_pool, init_pool

    await init_pool()
    pool = get_pool()
    async with pool.acquire() as conn:
        if args.keep_tokens:
            for table in ("candles_1m", "candles_3m", "candles_5m"):
                await conn.execute(
                    f"DELETE FROM {table} WHERE instrument_token <> ALL($1::text[])",
                    args.keep_tokens,
                )
        else:
            await conn.execute("DELETE FROM candles_1m")
            await conn.execute("DELETE FROM candles_3m")
            await conn.execute("DELETE FROM candles_5m")
        await conn.execute("DELETE FROM option_quotes")
        await conn.execute("DELETE FROM option_snapshots")
        await conn.execute("DELETE FROM instruments")
    await close_pool()
    print("OK: market candles and instruments cleared — restart engine to rebuild universe.")


if __name__ == "__main__":
    asyncio.run(main())
