#!/usr/bin/env python3
"""Flattrade OAuth login — generates daily access token.

Modes:
  1. Auto (password + TOTP in .env): python scripts/flattrade_login.py
  2. Browser OAuth: same command without TOTP — completes in browser
  3. Manual code paste: python scripts/flattrade_login.py --code 'REQUEST_CODE'
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sys
import webbrowser
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "trading-engine" / "src"))

os.chdir(ROOT)
os.environ.setdefault("CONFIG_DIR", str(ROOT / "config"))

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
import uvicorn

from algomcx.broker.auth import (
    FlattradeSessionStore,
    build_authorization_url,
    exchange_request_code,
    login_and_save,
    parse_redirect_url,
)
from algomcx.config import EnvSettings

app = FastAPI(title="Flattrade OAuth", docs_url=None)
_login_result: dict | None = None


SUCCESS_HTML = """
<!DOCTYPE html>
<html><head><title>Algo-MCX — Flattrade Login</title>
<style>
  body {{ font-family: system-ui; max-width: 520px; margin: 3rem auto; padding: 1rem; }}
  .ok {{ color: #166534; }} .err {{ color: #991b1b; }}
</style></head><body>
  <h2 class="{css}">{title}</h2>
  <p>{message}</p>
  <p>You can close this tab and return to the terminal.</p>
</body></html>
"""


@app.get("/callback")
@app.get("/")
async def oauth_callback(request: Request) -> HTMLResponse:
    global _login_result
    env = EnvSettings()
    request_code = request.query_params.get("code")
    if not request_code:
        html = SUCCESS_HTML.format(
            css="err", title="Login failed", message="Missing ?code= in redirect URL."
        )
        _login_result = {"ok": False, "error": "missing_code"}
        return HTMLResponse(html, status_code=400)

    if not env.flattrade_api_key or not env.flattrade_api_secret:
        html = SUCCESS_HTML.format(
            css="err",
            title="Configuration error",
            message="Set FLATTRADE_API_KEY and FLATTRADE_API_SECRET in .env",
        )
        _login_result = {"ok": False, "error": "missing_api_credentials"}
        return HTMLResponse(html, status_code=500)

    try:
        session = await exchange_request_code(
            env.flattrade_api_key,
            request_code,
            env.flattrade_api_secret,
        )
        if env.flattrade_user_id and session.user_id and session.user_id != env.flattrade_user_id:
            print(
                f"WARNING: OAuth client id {session.user_id} differs from "
                f"FLATTRADE_USER_ID={env.flattrade_user_id}"
            )
        if not session.user_id and env.flattrade_user_id:
            session = replace(session, user_id=env.flattrade_user_id)

        token_path = Path(env.flattrade_token_file)
        FlattradeSessionStore(token_path).save(session)
        _login_result = {"ok": True, "session": session.to_dict()}
        html = SUCCESS_HTML.format(
            css="ok",
            title="Login successful",
            message=f"Token saved for {session.user_id}. Expires {session.expires_at.strftime('%Y-%m-%d %H:%M %Z')}.",
        )
        return HTMLResponse(html)
    except Exception as exc:
        _login_result = {"ok": False, "error": str(exc)}
        html = SUCCESS_HTML.format(css="err", title="Token exchange failed", message=str(exc))
        return HTMLResponse(html, status_code=500)


async def run_login_server() -> dict:
    env = EnvSettings()
    if not env.flattrade_api_key or not env.flattrade_api_secret:
        print("ERROR: Set FLATTRADE_API_KEY and FLATTRADE_API_SECRET in .env")
        sys.exit(1)

    _, port, path = parse_redirect_url(env.flattrade_redirect_url)
    if path != "/callback":
        print(f"WARNING: Redirect path is {path}; this server only handles /callback")

    auth_url = build_authorization_url(env.flattrade_api_key)
    print("\n" + "=" * 60)
    print("Flattrade OAuth — open this URL in your browser:")
    print(auth_url)
    print("=" * 60 + "\n")
    print(f"Waiting for redirect on {env.flattrade_redirect_url} ...")

    webbrowser.open(auth_url)

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(config)

    async def wait_for_login() -> None:
        global _login_result
        while _login_result is None:
            await asyncio.sleep(0.3)
        server.should_exit = True

    await asyncio.gather(server.serve(), wait_for_login())
    return _login_result or {"ok": False, "error": "unknown"}


async def exchange_and_save(request_code: str) -> dict:
    env = EnvSettings()
    if not env.flattrade_api_key or not env.flattrade_api_secret:
        raise ValueError("Set FLATTRADE_API_KEY and FLATTRADE_API_SECRET in .env")

    session = await exchange_request_code(
        env.flattrade_api_key,
        request_code,
        env.flattrade_api_secret,
    )
    if env.flattrade_user_id and session.user_id and session.user_id != env.flattrade_user_id:
        print(
            f"WARNING: OAuth client id {session.user_id} differs from "
            f"FLATTRADE_USER_ID={env.flattrade_user_id}"
        )
    if not session.user_id and env.flattrade_user_id:
        session = replace(session, user_id=env.flattrade_user_id)

    token_path = Path(env.flattrade_token_file)
    FlattradeSessionStore(token_path).save(session)
    return {"ok": True, "session": session.to_dict()}


def main() -> None:
    parser = argparse.ArgumentParser(description="Flattrade daily token login")
    parser.add_argument(
        "--code",
        help="Request code from browser redirect URL (?code=...)",
    )
    args = parser.parse_args()

    if args.code:
        try:
            result = asyncio.run(exchange_and_save(args.code.strip()))
            session = result["session"]
            print(f"OK: Token saved for user {session['user_id']}")
            print(f"    Expires: {session['expires_at']}")
            print(f"    File:    {EnvSettings().flattrade_token_file}")
            return
        except Exception as exc:
            print(f"Token exchange failed: {exc}")
            sys.exit(1)

    env = EnvSettings()
    if env.flattrade_password and env.flattrade_totp_secret:
        print("Using automated login (password + TOTP)...")
        try:
            session = asyncio.run(login_and_save(env))
            print(f"\nOK: Token saved for user {session.user_id}")
            print(f"    Expires: {session.expires_at.isoformat()}")
            print(f"    File:    {env.flattrade_token_file}")
            print("\nNext: python scripts/phase0_spike.py")
            return
        except Exception as exc:
            print(f"\nAuto login failed: {exc}")
            sys.exit(1)

    if not env.flattrade_api_key or not env.flattrade_api_secret:
        print("ERROR: Set FLATTRADE_API_KEY and FLATTRADE_API_SECRET in .env")
        sys.exit(1)

    print("No TOTP secret — starting browser OAuth fallback...")
    result = asyncio.run(run_login_server())
    if result.get("ok"):
        session = result["session"]
        print(f"\nOK: Token saved for user {session['user_id']}")
        print(f"    Expires: {session['expires_at']}")
        print(f"    File:    {EnvSettings().flattrade_token_file}")
        print("\nNext: python scripts/phase0_spike.py")
    else:
        print(f"\nLogin failed: {result.get('error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
