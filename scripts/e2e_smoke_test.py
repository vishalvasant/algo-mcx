#!/usr/bin/env python3
"""End-to-end smoke test for Algo-MCX stack."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
ENGINE_PORT = 8001
WEB_PORT = 8080


def _fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


def _ok(msg: str) -> None:
    print(f"OK:   {msg}")


async def _wait_health(url: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    last_err = ""
    async with httpx.AsyncClient(timeout=5.0) as client:
        while time.time() < deadline:
            try:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return resp.json()
            except Exception as exc:
                last_err = str(exc)
            await asyncio.sleep(1)
    _fail(f"Health not ready at {url} ({last_err})")


async def main() -> None:
    os.chdir(ROOT)
    os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

    venv_python = ROOT / "trading-engine" / ".venv" / "bin" / "python"
    if not venv_python.exists():
        _fail("trading-engine venv missing — run: cd trading-engine && python3.12 -m venv .venv && pip install .")

    # 1) Unit tests
    print("\n==> Unit tests")
    r = subprocess.run(
        [str(venv_python), "-m", "pytest", "tests/unit", "-q"],
        cwd=ROOT / "trading-engine",
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        _fail("unit tests failed")
    _ok("unit tests passed")

    # 2) Auto login / token cache
    print("\n==> Flattrade token")
    r = subprocess.run(
        [str(venv_python), str(ROOT / "scripts" / "flattrade_auto_login.py")],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        print(r.stdout, r.stderr)
        _fail("flattrade_auto_login failed (VPN on?)")
    _ok("token available")

    # 3) Postgres check
    print("\n==> PostgreSQL")
    try:
        import asyncpg

        pool = await asyncpg.create_pool(os.environ.get(
            "DATABASE_URL", "postgresql://algoflat:algoflat@localhost:5432/algoflat"
        ), min_size=1, max_size=1)
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        await pool.close()
        _ok("postgres connected")
    except Exception as exc:
        _fail(f"postgres not available — start Docker: docker compose up -d postgres ({exc})")

    # 4) Start trading engine
    print("\n==> Trading engine")
    engine = subprocess.Popen(
        [str(venv_python), "-m", "algoflat.main"],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONUNBUFFERED": "1",
            "CONFIG_DIR": str(ROOT / "config"),
            "DATABASE_URL": os.environ.get(
                "DATABASE_URL", "postgresql://algoflat:algoflat@localhost:5432/algoflat"
            ),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        health = await _wait_health(f"http://127.0.0.1:{ENGINE_PORT}/health", timeout=45)
        print(json.dumps(health, indent=2))
        if not health.get("db_ok"):
            _fail("engine db_ok=false")
        _ok(f"engine status={health.get('status')}, broker={health.get('broker_connected')}")
    finally:
        engine.terminate()
        try:
            engine.wait(timeout=5)
        except subprocess.TimeoutExpired:
            engine.kill()

    # 5) Web app
    print("\n==> Web app")
    web = subprocess.Popen(
        [str(venv_python), "-m", "uvicorn", "algomcx_web.main:app", "--host", "127.0.0.1", "--port", str(WEB_PORT)],
        cwd=ROOT / "web-app",
        env={**os.environ, "PYTHONPATH": str(ROOT / "web-app" / "src")},
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        await _wait_health(f"http://127.0.0.1:{WEB_PORT}/api/health", timeout=30)
        async with httpx.AsyncClient(timeout=5.0) as client:
            dash = await client.get(f"http://127.0.0.1:{WEB_PORT}/")
            notes = await client.get(f"http://127.0.0.1:{WEB_PORT}/api/notifications?limit=5")
        if dash.status_code != 200:
            _fail(f"dashboard status {dash.status_code}")
        if notes.status_code != 200:
            _fail(f"notifications status {notes.status_code}")
        _ok("web dashboard + notifications API")
    finally:
        web.terminate()
        try:
            web.wait(timeout=5)
        except subprocess.TimeoutExpired:
            web.kill()

    print("\n==> E2E smoke test PASSED")


if __name__ == "__main__":
    asyncio.run(main())
