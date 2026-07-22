#!/usr/bin/env python3
"""Fully automated Flattrade daily token — no browser required.

Requires in .env:
  FLATTRADE_USER_ID
  FLATTRADE_API_KEY
  FLATTRADE_API_SECRET
  FLATTRADE_PASSWORD
  FLATTRADE_TOTP_SECRET   (from Flattrade web → Profile → Security → TOTP secret)

Flattrade API v2 also requires your current public IP to be registered in
Flattrade Wall → Pi → API Key → Primary IP.

Usage:
  python scripts/flattrade_auto_login.py
  python scripts/flattrade_auto_login.py --force   # ignore cached token
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))
os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from algomcx.broker.auth import FlattradeSessionStore, login_and_save
from algomcx.config import EnvSettings


def _validate_env(env: EnvSettings) -> None:
    missing = []
    if not env.flattrade_user_id:
        missing.append("FLATTRADE_USER_ID")
    if not env.flattrade_api_key:
        missing.append("FLATTRADE_API_KEY")
    if not env.flattrade_api_secret:
        missing.append("FLATTRADE_API_SECRET")
    if not env.flattrade_password:
        missing.append("FLATTRADE_PASSWORD")
    if not env.flattrade_totp_secret:
        missing.append("FLATTRADE_TOTP_SECRET")
    if missing:
        raise SystemExit(f"Missing in .env: {', '.join(missing)}")


async def _run(force: bool) -> int:
    env = EnvSettings()
    _validate_env(env)

    store = FlattradeSessionStore(Path(env.flattrade_token_file))
    if force and Path(env.flattrade_token_file).exists():
        Path(env.flattrade_token_file).unlink()

    cached = store.load()
    if cached and cached.is_valid and not force:
        print(f"Using cached token for {cached.user_id}")
        print(f"  Expires: {cached.expires_at.isoformat()}")
        print(f"  File:    {env.flattrade_token_file}")
        return 0

    print("Running headless Flattrade login (password + TOTP)...")
    try:
        session = await login_and_save(env)
    except ConnectionError as exc:
        msg = str(exc)
        if "INVALID_IP" in msg.upper():
            print("\nERROR: Flattrade rejected token exchange — INVALID_IP", file=sys.stderr)
            print(
                "Register your current public IP in Flattrade Wall → Pi → API Key → Primary IP.",
                file=sys.stderr,
            )
            print(
                "API v2 requires requests from that registered IP (Mac home IP or server VPS IP).",
                file=sys.stderr,
            )
        raise SystemExit(1) from exc

    print(f"OK: Token saved for {session.user_id}")
    print(f"  Expires: {session.expires_at.isoformat()}")
    print(f"  File:    {env.flattrade_token_file}")
    print(f"  Source:  {session.source}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Automated Flattrade token (no browser)")
    parser.add_argument("--force", action="store_true", help="Refresh token even if cache valid")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args.force)))


if __name__ == "__main__":
    main()
