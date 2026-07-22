#!/usr/bin/env bash
# Local dev — single UI entry point: http://localhost:8080
# FastAPI serves the React build + /api routes. Frontend auto-rebuilds on change.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WEB_PORT="${WEB_PORT:-8080}"
ENGINE_PORT="${ENGINE_PORT:-8001}"
VENV="${ROOT}/trading-engine/.venv/bin/python"

if [[ ! -x "$VENV" ]]; then
  echo "Missing venv at trading-engine/.venv — run:"
  echo "  cd trading-engine && python3.12 -m venv .venv && source .venv/bin/activate && pip install '.[dev]'"
  exit 1
fi

if [[ ! -f .env ]]; then
  echo "Missing .env — copy .env.example and fill in credentials."
  exit 1
fi

echo "==> Building frontend..."
(cd web-app/frontend && npm install --silent && npm run build)

echo "==> Starting frontend watch (rebuilds into web-app/frontend/dist)..."
(cd web-app/frontend && npm run watch) &
WATCH_PID=$!
trap 'kill $WATCH_PID 2>/dev/null || true' EXIT

echo "==> Starting trading engine on :${ENGINE_PORT}..."
CONFIG_DIR=./config DATABASE_URL="${DATABASE_URL:-postgresql://algomcx:algomcx@localhost:5432/algomcx}" \
  "$VENV" -m algomcx.main &
ENGINE_PID=$!
trap 'kill $ENGINE_PID $WATCH_PID 2>/dev/null || true' EXIT

sleep 2

echo "==> Starting web app on :${WEB_PORT} (UI + API)..."
echo "    Open http://127.0.0.1:${WEB_PORT}"
cd web-app
DATABASE_URL="${DATABASE_URL:-postgresql://algomcx:algomcx@localhost:5432/algomcx}" \
  TRADING_ENGINE_URL="http://127.0.0.1:${ENGINE_PORT}" \
  PYTHONPATH=src \
  "$VENV" -m uvicorn algomcx_web.main:app --host 0.0.0.0 --port "$WEB_PORT"
