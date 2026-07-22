"""MCX multi-underlying symbol helpers."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import re
import structlog

from algomcx.config import AppConfig

logger = structlog.get_logger(__name__)

_FUT_SUFFIX = "FUT"


def list_underlyings(config: AppConfig) -> list[dict[str, Any]]:
    rows = config.symbols.get("underlyings")
    if isinstance(rows, list) and rows:
        return rows
    return [
        {
            "symbol": config.symbols.get("underlying", "GOLD"),
            "display_name": config.symbols.get("underlying", "GOLD"),
            "spot_token": config.symbols.get("spot_token", ""),
            "strike_step": config.symbols.get("strike_step", 100),
            "strike_band_points": config.symbols.get("strike_band_points", 500),
            "tick_size": 1,
            "fallback_spot": 75000,
        }
    ]


def symbols_for(config: AppConfig, symbol: str) -> dict[str, Any]:
    sym = symbol.upper()
    base = dict(config.symbols)
    for row in list_underlyings(config):
        if str(row.get("symbol", "")).upper() == sym:
            merged = {**base, **row, "underlying": sym}
            return merged
    base["underlying"] = sym
    return base


def strike_band_points(sym_cfg: dict[str, Any]) -> float:
    steps = int(sym_cfg.get("atm_strike_steps", sym_cfg.get("atm_band_steps", 10)))
    step = float(sym_cfg.get("strike_step", 100))
    explicit = sym_cfg.get("strike_band_points")
    if explicit is not None:
        return float(explicit)
    return steps * step


def apply_active_underlying(config: AppConfig, symbol: str) -> dict[str, Any]:
    merged = symbols_for(config, symbol)
    config.symbols.update(merged)
    return merged


async def resolve_spot_token(broker: Any, symbol: str, exchange: str = "MCX") -> str | None:
    """Pick nearest MCX futures contract token for spot/candle tracking."""
    try:
        rows = await broker.search_scrip(exchange, symbol)
    except Exception:
        logger.exception("spot_token_search_failed", symbol=symbol)
        return None
    if not rows:
        return None

    ul = symbol.upper()
    # Exact prefix: SILVER matches SILVER04DEC26 but not SILVERM…; GOLDM matches GOLDM… only.
    prefix_re = re.compile(rf"^{re.escape(ul)}(\d|$)")
    fut_tsym = re.compile(rf"^{re.escape(ul)}(\d{{2}})([A-Z]{{3}})(\d{{2}})$")
    fut_rows: list[dict[str, Any]] = []
    for row in rows:
        tsym = str(row.get("tsym", "")).upper()
        inst = str(row.get("instname", "")).upper()
        if inst != "FUTCOM":
            continue
        if not prefix_re.match(tsym):
            continue
        if not fut_tsym.match(tsym):
            continue
        fut_rows.append(row)

    if not fut_rows:
        return None

    def _expiry_key(row: dict[str, Any]) -> tuple[int, str]:
        tsym = str(row.get("tsym", "")).upper()
        m = re.search(r"(\d{2}[A-Z]{3}\d{2})", tsym)
        if m:
            from algomcx.contract_selector.expiry import parse_expiry_tag

            exp = parse_expiry_tag(m.group(1))
            if exp is not None:
                return (0, exp.isoformat())
        return (1, tsym)

    fut_rows.sort(key=_expiry_key)
    token = str(fut_rows[0].get("token", "")).strip()
    return token or None


async def resolve_all_spot_tokens(config: AppConfig, broker: Any) -> dict[str, str]:
    exchange = str(config.symbols.get("exchange_spot", "MCX"))
    resolved: dict[str, str] = {}
    for row in list_underlyings(config):
        sym = str(row.get("symbol", "")).upper()
        existing = str(row.get("spot_token", "")).strip()
        if existing:
            resolved[sym] = existing
            continue
        spot_query = str(row.get("spot_search_text") or sym).upper()
        token = await resolve_spot_token(broker, spot_query, exchange)
        if token:
            row["spot_token"] = token
            resolved[sym] = token
            logger.info("spot_token_resolved", symbol=sym, token=token)
    if resolved:
        primary = str(config.symbols.get("underlying", "GOLD")).upper()
        if primary in resolved:
            config.symbols["spot_token"] = resolved[primary]
    return resolved


def fallback_spot(config: AppConfig, symbol: str) -> Decimal:
    merged = symbols_for(config, symbol)
    raw = merged.get("fallback_spot", 75000)
    return Decimal(str(raw))
