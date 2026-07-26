"""MCX bullion futures watchlist — GOLD / GOLDM / GOLD MINI and SILVER variants."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from algomcx.contract_selector.expiry import include_expiry_day, parse_expiry_tag
from algomcx.symbols_util import lot_size_for_tsym

logger = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")
_FUT_TSYM = re.compile(r"^(?P<prefix>[A-Z0-9]+)(?P<tag>\d{2}[A-Z]{3}\d{2})$")


@dataclass(frozen=True)
class FuturesWatchSpec:
    group: str
    segment: str
    search: str
    display: str
    match_mode: str  # exact | prefix
    exclude_prefixes: tuple[str, ...] = ()
    sort_order: int = 0
    group_order: int = 99
    alt_searches: tuple[str, ...] = ()


DEFAULT_WATCHLIST: tuple[FuturesWatchSpec, ...] = (
    FuturesWatchSpec("Gold", "GOLD", "GOLD", "GOLD FUT", "exact", ("GOLDM", "GOLDMINI", "GOLDPETAL"), 1),
    FuturesWatchSpec("Gold", "GOLDM", "GOLDM", "GOLDM", "prefix", (), 2),
    FuturesWatchSpec("Gold", "GOLDMINI", "GOLDMIN", "GOLD MINI", "prefix", (), 3),
    FuturesWatchSpec("Silver", "SILVERMIC", "SILVERMIC", "SILVER MICRO", "prefix", (), 1),
    FuturesWatchSpec("Silver", "SILVERM", "SILVERM", "SILVER MINI", "prefix", ("SILVERMIC",), 2),
    FuturesWatchSpec(
        "Silver",
        "SILVER",
        "SILVER",
        "SILVER FUT",
        "exact",
        ("SILVERM", "SILVERMIC", "SILVERMINI"),
        3,
    ),
)


def _expiry_from_row(row: dict[str, Any]) -> date | None:
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
    m = _FUT_TSYM.match(tsym)
    if m:
        return parse_expiry_tag(m.group("tag"))
    return None


def _tsym_matches(spec: FuturesWatchSpec, tsym: str) -> bool:
    upper = tsym.upper()
    m = _FUT_TSYM.match(upper)
    if not m:
        return False
    prefix = m.group("prefix")
    for ex in spec.exclude_prefixes:
        if upper.startswith(ex):
            return False
    if spec.match_mode == "exact":
        return prefix == spec.search.upper()
    return prefix.startswith(spec.search.upper())


def _pick_nearest_future(
    rows: list[dict[str, Any]],
    spec: FuturesWatchSpec,
    *,
    as_of: date | None = None,
) -> dict[str, Any] | None:
    today = as_of or datetime.now(IST).date()
    include_today = include_expiry_day(today)
    candidates: list[tuple[date, dict[str, Any]]] = []
    for row in rows:
        inst = str(row.get("instname", row.get("Instrument", ""))).upper()
        if inst and inst != "FUTCOM":
            continue
        tsym = str(row.get("tsym", row.get("Tradingsymbol", ""))).upper()
        if not tsym or not _tsym_matches(spec, tsym):
            continue
        token = str(row.get("token", row.get("Token", ""))).strip()
        if not token:
            continue
        exp = _expiry_from_row(row)
        if exp is not None:
            if exp < today:
                continue
            if exp == today and not include_today:
                continue
        candidates.append((exp or date.max, row))
    if not candidates:
        return None
    candidates.sort(key=lambda x: (x[0], str(x[1].get("tsym", ""))))
    return candidates[0][1]


def _expiry_label(tsym: str) -> str | None:
    m = _FUT_TSYM.match(tsym.upper())
    if not m:
        return None
    parsed = parse_expiry_tag(m.group("tag"))
    if parsed is None:
        return m.group("tag")
    return parsed.strftime("%d %b %y").upper()


def watchlist_specs_from_config(config: dict[str, Any]) -> tuple[FuturesWatchSpec, ...]:
    raw = config.get("futures_watchlist")
    if not isinstance(raw, list) or not raw:
        return DEFAULT_WATCHLIST
    specs: list[FuturesWatchSpec] = []
    for i, row in enumerate(raw):
        if not isinstance(row, dict):
            continue
        specs.append(
            FuturesWatchSpec(
                group=str(row.get("group", "")),
                segment=str(row.get("segment", row.get("search", ""))),
                search=str(row.get("search", "")),
                display=str(row.get("display_name", row.get("display", ""))),
                match_mode=str(row.get("match_mode", "prefix")),
                exclude_prefixes=tuple(row.get("exclude_prefixes") or ()),
                sort_order=int(row.get("sort_order", i + 1)),
                group_order=int(row.get("group_order", 99)),
                alt_searches=tuple(row.get("alt_searches") or ()),
            )
        )
    return tuple(specs) if specs else DEFAULT_WATCHLIST


async def resolve_mcx_futures_watchlist(
    broker: Any,
    *,
    exchange: str = "MCX",
    config: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Resolve current-month (nearest live) FUTCOM row per bullion segment."""
    cfg = config or {}
    specs = watchlist_specs_from_config(cfg)
    cache: dict[str, list[dict[str, Any]]] = {}
    items: list[dict[str, Any]] = []

    for spec in specs:
        search = spec.search.upper()
        if search not in cache:
            try:
                cache[search] = list(await broker.search_scrip(exchange, search) or [])
            except Exception:
                logger.exception("futures_watchlist_search_failed", search=search)
                cache[search] = []

        row = _pick_nearest_future(cache[search], spec)
        if row is None:
            for alt in spec.alt_searches:
                alt_key = alt.upper()
                if alt_key not in cache:
                    try:
                        cache[alt_key] = list(await broker.search_scrip(exchange, alt_key) or [])
                    except Exception:
                        logger.exception("futures_watchlist_search_failed", search=alt_key)
                        cache[alt_key] = []
                alt_spec = FuturesWatchSpec(
                    spec.group,
                    spec.segment,
                    alt_key,
                    spec.display,
                    "prefix",
                    spec.exclude_prefixes,
                    spec.sort_order,
                    spec.group_order,
                    (),
                )
                row = _pick_nearest_future(cache[alt_key], alt_spec)
                if row:
                    break
        if row is None:
            # GOLD MINI naming varies on MCX (GOLDMIN / GOLDPETAL / GOLDMINI).
            if spec.segment in ("GOLDMINI", "GOLDMIN"):
                for alt in ("GOLDMIN", "GOLDMINI", "GOLDPETAL", "GOLDGUINEA"):
                    if alt not in cache:
                        try:
                            cache[alt] = list(await broker.search_scrip(exchange, alt) or [])
                        except Exception:
                            cache[alt] = []
                    alt_spec = FuturesWatchSpec(
                        spec.group,
                        spec.segment,
                        alt,
                        spec.display,
                        "prefix",
                        spec.exclude_prefixes,
                        spec.sort_order,
                        spec.group_order,
                        (),
                    )
                    row = _pick_nearest_future(cache[alt], alt_spec)
                    if row:
                        break
            if row is None:
                logger.warning("futures_watchlist_missing", segment=spec.segment, search=search)
                continue

        tsym = str(row.get("tsym", "")).upper()
        token = str(row.get("token", "")).strip()
        lot_raw = row.get("ls") or row.get("lot_size") or row.get("Lotsize")
        meta_lot: int | None = None
        if lot_raw is not None:
            try:
                meta_lot = int(float(lot_raw))
            except (TypeError, ValueError):
                meta_lot = None
        lot_size = lot_size_for_tsym(
            tsym,
            metadata_lot_size=meta_lot,
            segment_key=spec.segment,
        )

        items.append(
            {
                "token": token,
                "tsym": tsym,
                "strike": float(spec.sort_order),
                "option_type": "FUT",
                "is_atm": False,
                "tradable": True,
                "lot_size": lot_size,
                "ltp": None,
                "bid": None,
                "ask": None,
                "volume": None,
                "oi": None,
                "segment_group": spec.group,
                "segment_label": spec.display,
                "segment_key": spec.segment,
                "group_order": spec.group_order,
                "expiry_label": _expiry_label(tsym),
                "last_update_ts": None,
            }
        )
        logger.info(
            "futures_watchlist_resolved",
            segment=spec.segment,
            tsym=tsym,
            token=token,
        )

    items.sort(
        key=lambda r: (
            int(r.get("group_order", 99)),
            str(r.get("segment_group", "")),
            float(r.get("strike", 0)),
        )
    )
    return items
