#!/usr/bin/env python3
"""Phase 0: Flattrade connectivity spike.

Usage (from repo root):
  cp .env.example .env   # add FLATTRADE_USER_ID + FLATTRADE_ACCESS_TOKEN
  cd trading-engine && pip install -e . && python ../scripts/phase0_spike.py
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))

from algomcx.broker.flattrade import FlattradeAdapter
from algomcx.config import AppConfig, EnvSettings
from algomcx.models.events import CandleInterval


async def main() -> None:
    os.chdir(ROOT)
    os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

    env = EnvSettings()
    if not env.flattrade_api_key or not env.flattrade_api_secret:
        print("ERROR: Set FLATTRADE_API_KEY and FLATTRADE_API_SECRET in .env")
        sys.exit(1)

    from algomcx.broker.auth import ensure_session

    config = AppConfig()
    broker = FlattradeAdapter(config)

    print("==> Resolving Flattrade session (env or .flattrade/session.json)...")
    try:
        await ensure_session(env)
    except RuntimeError as exc:
        print(f"ERROR: {exc}")
        sys.exit(1)

    print("==> Connecting Flattrade session...")
    await broker.connect()
    print("OK: session established")

    exchange = config.symbols["exchange_spot"]
    token = config.symbols["spot_token"]
    end = datetime.now(tz=timezone.utc)
    start = end - timedelta(hours=3)

    print("==> Fetching NIFTY 1m/3m/5m candles...")
    for interval in (CandleInterval.M1, CandleInterval.M3, CandleInterval.M5):
        candles = await broker.get_candles(exchange, token, interval, start, end)
        print(f"  {interval.value}: {len(candles)} bars")
        if candles:
            last = candles[-1]
            print(f"    last close={last.close} @ {last.ts.isoformat()}")

    print("==> Fetching NIFTY quote snapshot...")
    quote = await broker.get_quotes(exchange, token)
    print(f"  stat={quote.get('stat')} ltp={quote.get('lp')}")

    spot = float(quote.get("lp") or 24500)
    print("==> Fetching option chain around ATM...")
    chain = await broker.get_option_chain(
        exchange=config.symbols["exchange_options"],
        tradingsymbol=config.symbols["underlying"],
        strikeprice=spot,
        count=int(config.symbols["strike_band_points"] / config.symbols["strike_step"]),
    )
    print(f"  contracts returned: {len(chain)}")
    if chain:
        print(f"  sample: {chain[0].get('tsym')} token={chain[0].get('token')}")

    print("==> WebSocket smoke test (10 seconds)...")
    count = 0

    def on_quote(quote) -> None:
        nonlocal count
        count += 1
        if count <= 3:
            print(f"  tick #{count}: token={quote.instrument_token} ltp={quote.ltp}")

    keys = [broker.format_instrument(exchange, token)]
    if chain:
        sample = chain[0]
        keys.append(broker.format_instrument(sample["exch"], str(sample["token"])))

    ws_task = asyncio.create_task(broker.subscribe(keys, on_quote))
    await asyncio.sleep(10)
    ws_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ws_task

    print(f"OK: received {count} quote updates in 10s")
    print("\nPhase 0 spike complete.")


if __name__ == "__main__":
    asyncio.run(main())
