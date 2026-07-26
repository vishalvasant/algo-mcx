#!/usr/bin/env bash
# Replay today's (or given) IST session with live-parity router, validator, exits, and sizing.
# Run from repo root on the VPS (Flattrade API IP must match your API key).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
DATE="${1:-$(TZ=Asia/Kolkata date +%F)}"
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" && -x trading-engine/.venv/bin/python ]]; then
  PYTHON=trading-engine/.venv/bin/python
elif [[ -z "$PYTHON" ]]; then
  PYTHON=python3
fi
export CONFIG_DIR="${CONFIG_DIR:-$ROOT/config}"
export PYTHONPATH="${PYTHONPATH:-$ROOT/trading-engine/src}"
SCAN="${SCAN_INTERVAL:-10}"
echo "==> Day backtest $DATE (NIFTY, scan=${SCAN}s, current config)"
exec "$PYTHON" scripts/day_backtest.py \
  --date "$DATE" \
  --underlying NIFTY \
  --scan-interval "$SCAN" \
  "${@:2}"
