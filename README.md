# Algo-MCX

MCX commodity options auto-trader (Gold, Silver, Natural Gas) via **Flattrade**, forked from Algo-Flat.

## Quick start

```bash
cp .env.example .env   # fill Flattrade credentials
make up                # Docker: UI http://localhost:8081
```

**Login:** `admin` / `algomcx` (see `.env`)

**Ports (default):** Web `8081`, Engine `8002`, Postgres `5433`, Redis `6380`

## Commodities

| Symbol | Exchange | Session |
|--------|----------|---------|
| GOLD | MCX | 09:00–23:30 IST |
| SILVER | MCX | 09:00–23:30 IST |
| NATURALGAS | MCX | 09:00–23:30 IST |

Scanner rotates across all three each cycle. Dashboard has Gold/Silver/Gas tabs on the options chain.

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
