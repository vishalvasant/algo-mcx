# Algo-MCX

MCX Gold futures + options auto-trader via **Flattrade**, forked from Algo-Flat (same layout, process, and features).

## Quick start

```bash
cp .env.example .env   # fill Flattrade credentials
make up                # Docker: UI http://localhost:8081
```

**Login:** `admin` / `algomcx` (see `.env`)

**Ports (default):** Web `8081`, Engine `8002`, Postgres `5433`, Redis `6380`

## Phase 1 — Gold only

| Symbol | Exchange | Session |
|--------|----------|---------|
| GOLD | MCX | 09:00–23:30 IST |

Futures price drives spot/candles; options chain is GOLD CE/PE (OPTFUT).

Trading-engine logic tweaks happen **after** backtesting — this repo mirrors algo-flat structure first.

## Local dev (no Docker)

```bash
cd trading-engine && python3.12 -m venv .venv && source .venv/bin/activate && pip install '.[dev]'
cd ../web-app && pip install -e .
# Start Postgres on :5433 first, then:
make dev
```

## Flattrade auth

```bash
python scripts/flattrade_login.py
# or use Re-authenticate in the dashboard
```

## Deploy

```bash
git pull origin main
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

After schema changes, migrations apply on engine startup. Use **Re-Authenticate** daily before the MCX session.
