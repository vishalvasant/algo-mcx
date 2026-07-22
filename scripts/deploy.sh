#!/usr/bin/env bash
# Deploy Algo-MCX on a server with Docker Compose.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE="${1:-prod}"

if [[ ! -f .env ]]; then
  echo "Missing .env — copy .env.example and fill in Flattrade credentials."
  exit 1
fi

COMPOSE=(docker compose)
if [[ "$MODE" == "prod" ]]; then
  COMPOSE+=(-f docker-compose.yml -f docker-compose.prod.yml)
fi

echo "==> Building images..."
"${COMPOSE[@]}" build

echo "==> Starting stack ($MODE)..."
"${COMPOSE[@]}" up -d

echo "==> Waiting for web health..."
for _ in $(seq 1 30); do
  if curl -sf "http://127.0.0.1:${WEB_PORT:-8080}/health" >/dev/null 2>&1; then
    echo "OK — dashboard: http://127.0.0.1:${WEB_PORT:-8080}"
    exit 0
  fi
  sleep 2
done

echo "Stack started but web health check timed out. Run: ${COMPOSE[*]} logs -f web-app"
exit 1
