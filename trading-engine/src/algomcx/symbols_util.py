"""MCX commodity symbol helpers (Gold phase 1)."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from algomcx.config import AppConfig
from algomcx.contract_selector.expiry import include_expiry_day, parse_expiry_tag

logger = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_MCX_FUT_TSYM = re.compile(r"^([A-Z]+)(\d{2})([A-Z]{3})(\d{2})$")


def fut_expiry_label_from_tsym(tsym: str) -> str | None:
    """Human label for top-bar FUT card, e.g. GOLD05DEC26 → 05 DEC."""
    m = _MCX_FUT_TSYM.match(str(tsym).upper())
    if not m:
        return None
    tag = f"{m.group(2)}{m.group(3)}{m.group(4)}"
    parsed = parse_expiry_tag(tag)
    if parsed is None:
        return tag
    return parsed.strftime("%d %b").upper()


def list_underlyings(config: AppConfig) -> list[dict[str, Any]]:
    rows = config.symbols.get("underlyings")
    if isinstance(rows, list) and rows:
        return rows
    return [
        {
            "symbol": config.symbols.get("underlying", "GOLD"),
            "display_name": config.symbols.get("underlying", "GOLD"),
            "exchange_spot": config.symbols.get("exchange_spot", "MCX"),
            "exchange_options": config.symbols.get("exchange_options", "MCX"),
            "spot_token": config.symbols.get("spot_token", ""),
            "strike_step": config.symbols.get("strike_step", 500),
            "strike_band_points": config.symbols.get("strike_band_points", 2500),
            "atm_strike_steps": config.symbols.get("atm_strike_steps", 5),
            "fallback_spot": 145500,
        }
    ]


def list_index_tickers(config: AppConfig) -> list[dict[str, Any]]:
    rows = config.symbols.get("index_tickers")
    if isinstance(rows, list):
        return [row for row in rows if row.get("symbol")]
    return []


def is_ticker_only(config: AppConfig, symbol: str) -> bool:
    sym = symbol.upper()
    return any(str(row.get("symbol", "")).upper() == sym for row in list_index_tickers(config))


def uses_futures_price(sym_cfg: dict[str, Any]) -> bool:
    return str(sym_cfg.get("price_source", "")).lower() == "futures"


def price_exchange_for(sym_cfg: dict[str, Any]) -> str:
    if uses_futures_price(sym_cfg):
        return str(
            sym_cfg.get("exchange_futures")
            or sym_cfg.get("exchange_options")
            or sym_cfg.get("exchange_spot", "MCX")
        )
    return str(sym_cfg.get("exchange_spot", "MCX"))


def price_context(sym_cfg: dict[str, Any]) -> tuple[str, str]:
    return (
        price_exchange_for(sym_cfg),
        str(sym_cfg.get("spot_token", "")),
    )


def symbols_for(config: AppConfig, symbol: str) -> dict[str, Any]:
    sym = symbol.upper()
    base = dict(config.symbols)
    for row in list_underlyings(config):
        if str(row.get("symbol", "")).upper() == sym:
            merged = {**base, **row, "underlying": sym}
            return merged
    for row in list_index_tickers(config):
        if str(row.get("symbol", "")).upper() == sym:
            merged = {**base, **row, "underlying": sym}
            return merged
    base["underlying"] = sym
    return base


def strike_band_points(sym_cfg: dict[str, Any]) -> float:
    steps = int(sym_cfg.get("atm_strike_steps", sym_cfg.get("atm_band_steps", 5)))
    step = float(sym_cfg.get("strike_step", 500))
    explicit = sym_cfg.get("strike_band_points")
    if explicit is not None:
        return float(explicit)
    return steps * step


def apply_active_underlying(config: AppConfig, symbol: str) -> dict[str, Any]:
    merged = symbols_for(config, symbol)
    config.symbols.update(merged)
    return merged


POOL_UNDERLYING = "POOL"

# MCX Gold futures/options lot size (1 kg contract).
GOLD_LOT_SIZE = 1
NIFTY_LOT_SIZE = GOLD_LOT_SIZE  # compat alias for shared engine code

# MCX FUTCOM lot sizes by segment key (fallback when broker metadata is missing).
MCX_LOT_BY_SEGMENT: dict[str, int] = {
    "GOLD": 1,
    "GOLDM": 100,
    "GOLDMINI": 1,
    "SILVER": 30,
    "SILVERM": 5,
    "SILVERMIC": 1,
    "CRUDEOIL": 100,
    "CRUDEOILM": 10,
    "NATURALGAS": 1250,
    "NATURALGASM": 250,
    "NATGASMINI": 250,
    "COPPER": 2500,
    "ZINC": 5000,
    "ALUMINI": 1000,
    "LEAD": 5000,
    "NICKEL": 1500,
}

GOLD_SPOT_MIN = Decimal("40000")
GOLD_SPOT_MAX = Decimal("350000")


def is_sane_gold_spot(spot: Decimal | float | None) -> bool:
    if spot is None:
        return False
    try:
        px = Decimal(str(spot))
    except Exception:
        return False
    return GOLD_SPOT_MIN <= px <= GOLD_SPOT_MAX


# Shared market_data engine imports this name from flat fork.
is_sane_nifty_spot = is_sane_gold_spot


def is_sane_spot(spot: Decimal | float | None, *, symbol: str = "GOLD") -> bool:
    """Sanity check — strict band for gold; any positive price for other MCX contracts."""
    if spot is None:
        return False
    sym = symbol.upper().removesuffix("_FUT")
    if sym in ("GOLD",):
        return is_sane_gold_spot(spot)
    try:
        return Decimal(str(spot)) > 0
    except Exception:
        return False


def _segment_key_from_tsym(tsym: str) -> str | None:
    upper = (tsym or "").upper()
    for key in sorted(MCX_LOT_BY_SEGMENT, key=len, reverse=True):
        if upper.startswith(key):
            return key
    return None


def lot_size_for_tsym(
    tsym: str,
    *,
    metadata_lot_size: int | None = None,
    segment_key: str | None = None,
) -> int:
    if metadata_lot_size is not None and metadata_lot_size > 0:
        return int(metadata_lot_size)
    seg = (segment_key or _segment_key_from_tsym(tsym) or "").upper()
    if seg in MCX_LOT_BY_SEGMENT:
        return MCX_LOT_BY_SEGMENT[seg]
    upper = (tsym or "").upper()
    if upper.startswith("GOLD"):
        return GOLD_LOT_SIZE
    return 1


def uses_pooled_capital(config: AppConfig) -> bool:
    return bool(config.risk.get("use_pooled_capital", True))


def total_account_capital(config: AppConfig) -> Decimal:
    return Decimal(str(config.risk.get("account_capital_inr", 50000)))


def risk_underlying_key(config: AppConfig, symbol: str | None = None) -> str:
    if uses_pooled_capital(config):
        return POOL_UNDERLYING
    sym = (symbol or config.symbols.get("underlying", "GOLD")).upper()
    return sym


def capital_for(config: AppConfig, symbol: str) -> Decimal:
    if uses_pooled_capital(config):
        return total_account_capital(config)
    sym = symbol.upper()
    alloc = config.risk.get("capital_allocations") or {}
    if sym in alloc:
        return Decimal(str(alloc[sym]))
    rows = list_underlyings(config)
    if len(rows) > 1:
        total = total_account_capital(config)
        return total / Decimal(len(rows))
    return total_account_capital(config)


def fallback_spot(config: AppConfig, symbol: str) -> Decimal:
    merged = symbols_for(config, symbol)
    raw = merged.get("fallback_spot", 145500)
    return Decimal(str(raw))


def _expiry_from_search_row(row: dict[str, Any]) -> date | None:
    for key in ("exd", "expiry", "Expiry"):
        raw = row.get(key)
        if not raw:
            continue
        text = str(raw).strip()
        for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt).date()
            except ValueError:
                continue
        parsed = parse_expiry_tag(text.upper())
        if parsed is not None:
            return parsed
    tsym = str(row.get("tsym", "")).upper()
    m = re.search(r"(\d{2}[A-Z]{3}\d{2})", tsym)
    if m:
        return parse_expiry_tag(m.group(1))
    return None


def _is_futcom_row(row: dict[str, Any], symbol: str) -> bool:
    inst = str(row.get("instname", row.get("Instrument", ""))).upper()
    if inst and inst != "FUTCOM":
        return False
    tsym = str(row.get("tsym", row.get("Tradingsymbol", ""))).upper()
    if not tsym:
        return False
    ul = symbol.upper()
    prefix_re = re.compile(rf"^{re.escape(ul)}(\d|$)")
    return bool(prefix_re.match(tsym) and _MCX_FUT_TSYM.match(tsym))


async def resolve_mcx_future_token(
    broker: Any,
    symbol: str,
    exchange: str = "MCX",
) -> tuple[str, str] | None:
    """Nearest MCX FUTCOM token for candles and trading price."""
    ul = symbol.upper()
    spot_query = ul
    try:
        rows = await broker.search_scrip(exchange, spot_query)
    except Exception:
        logger.exception("mcx_futures_token_search_failed", symbol=ul, exchange=exchange)
        return None

    fut_rows = [r for r in rows or [] if _is_futcom_row(r, ul)]
    if not fut_rows:
        from algomcx.contract_selector.scripmaster import nearest_future_from_scripmaster

        fallback = nearest_future_from_scripmaster(underlying=ul, exchange=exchange)
        if fallback is None:
            return None
        token, tsym = fallback
        return token, tsym

    today = datetime.now(IST).date()
    include_today = include_expiry_day(today)

    def _sort_key(row: dict[str, Any]) -> tuple[int, str]:
        exp = _expiry_from_search_row(row)
        if exp is not None:
            return (0, exp.isoformat())
        return (1, str(row.get("tsym", "")))

    fut_rows.sort(key=_sort_key)
    for row in fut_rows:
        exp = _expiry_from_search_row(row)
        if exp is not None:
            if exp > today:
                pass
            elif exp < today:
                continue
            elif not include_today:
                continue
        token = str(row.get("token", row.get("Token", ""))).strip()
        tsym = str(row.get("tsym", row.get("Tradingsymbol", ""))).strip()
        if token:
            return token, tsym
    return None


async def resolve_index_future_token(
    broker: Any,
    symbol: str,
    exchange: str,
) -> tuple[str, str] | None:
    """MCX wrapper — flat main.py calls this for futures price source."""
    if str(exchange).upper() == "MCX":
        return await resolve_mcx_future_token(broker, symbol.upper(), exchange)
    return None


async def resolve_spot_token(broker: Any, symbol: str, exchange: str = "MCX") -> str | None:
    result = await resolve_mcx_future_token(broker, symbol, exchange)
    return result[0] if result else None


async def resolve_all_spot_tokens(config: AppConfig, broker: Any) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for row in list_underlyings(config):
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue

        if uses_futures_price(row):
            exchange = price_exchange_for(row)
            row["exchange_spot"] = exchange
            spot_query = str(row.get("spot_search_text") or sym).upper()
            result = await resolve_mcx_future_token(broker, spot_query, exchange)
            if result:
                token, tsym = result
                row["spot_token"] = token
                row["fut_tsym"] = tsym
                resolved[sym] = token
                logger.info(
                    "futures_token_resolved",
                    symbol=sym,
                    exchange=exchange,
                    token=token,
                    tsym=tsym,
                )
            continue

        existing = str(row.get("spot_token", "")).strip()
        if existing:
            resolved[sym] = existing

    if resolved:
        primary = str(config.symbols.get("underlying", "GOLD")).upper()
        if primary in resolved:
            merged = apply_active_underlying(config, primary)
            merged["spot_token"] = resolved[primary]
    return resolved


async def resolve_index_ticker_token(
    broker: Any,
    symbol: str,
    exchange: str,
) -> tuple[str, str] | None:
    ul = symbol.upper()
    try:
        rows = await broker.search_scrip(exchange, ul)
    except Exception:
        logger.exception("index_ticker_search_failed", symbol=ul, exchange=exchange)
        return None
    exact: tuple[str, str] | None = None
    prefix: tuple[str, str] | None = None
    for row in rows or []:
        tsym = str(row.get("tsym", row.get("Tradingsymbol", ""))).upper()
        token = str(row.get("token", row.get("Token", ""))).strip()
        if not token:
            continue
        if tsym == ul:
            exact = (token, tsym)
            break
        if prefix is None and tsym.startswith(ul):
            prefix = (token, tsym)
    return exact or prefix


async def resolve_index_ticker_tokens(config: AppConfig, broker: Any) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for row in list_index_tickers(config):
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        existing = str(row.get("spot_token", "")).strip()
        if existing:
            resolved[sym] = existing
            continue
        exchange = str(row.get("exchange", "MCX"))
        result = await resolve_index_ticker_token(broker, sym, exchange)
        if result:
            token, tsym = result
            row["spot_token"] = token
            resolved[sym] = token
            logger.info(
                "index_ticker_token_resolved",
                symbol=sym,
                exchange=exchange,
                token=token,
                tsym=tsym,
            )
    return resolved
