"""MCX option expiry + instrument parsing from Flattrade search results."""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from algomcx.contract_selector.expiry import parse_expiry_tag
from algomcx.models.events import Instrument

_OPTION_TSYM = re.compile(
    r"^(?P<ul>[A-Z0-9]+)(?P<tag>\d{2}[A-Z]{3}\d{2})(?P<side>[CP])(?P<strike>\d+(?:\.\d+)?)$"
)


def parse_option_tsym(tsym: str) -> dict[str, Any] | None:
    m = _OPTION_TSYM.match((tsym or "").upper().strip())
    if not m:
        return None
    return {
        "underlying": m.group("ul"),
        "expiry_tag": m.group("tag").upper(),
        "option_type": "CE" if m.group("side") == "C" else "PE",
        "strike": Decimal(m.group("strike")),
    }


def option_rows_from_search(
    rows: list[dict[str, Any]],
    *,
    option_prefix: str,
) -> list[dict[str, Any]]:
    prefix = option_prefix.upper()
    out: list[dict[str, Any]] = []
    for row in rows:
        inst = str(row.get("instname", "")).upper()
        if inst and inst != "OPTFUT":
            continue
        tsym = str(row.get("tsym", "")).upper()
        if not tsym.startswith(prefix):
            continue
        if parse_option_tsym(tsym) is None:
            continue
        out.append(row)
    return out


def option_expiry_candidates(
    rows: list[dict[str, Any]],
    *,
    option_prefix: str,
    as_of: date | None = None,
    limit: int = 4,
) -> list[str]:
    today = as_of or date.today()
    expiries: dict[str, date] = {}
    for row in option_rows_from_search(rows, option_prefix=option_prefix):
        parsed = parse_option_tsym(str(row.get("tsym", "")))
        if parsed is None:
            continue
        tag = parsed["expiry_tag"]
        exp = parse_expiry_tag(tag)
        if exp is None or exp < today:
            continue
        expiries.setdefault(tag, exp)
    return [t for t, _ in sorted(expiries.items(), key=lambda x: x[1])[:limit]]


def instruments_from_search(
    rows: list[dict[str, Any]],
    *,
    option_prefix: str,
    expiry_tag: str,
    atm: Decimal,
    band_points: Decimal,
    step: Decimal,
    exchange: str,
    underlying: str,
) -> list[Instrument]:
    expiry_date = parse_expiry_tag(expiry_tag)
    if expiry_date is None:
        return []

    instruments: list[Instrument] = []
    tag = expiry_tag.upper()
    for row in option_rows_from_search(rows, option_prefix=option_prefix):
        tsym = str(row.get("tsym", "")).upper()
        parsed = parse_option_tsym(tsym)
        if parsed is None or parsed["expiry_tag"] != tag:
            continue
        strike = parsed["strike"]
        if abs(strike - atm) > band_points:
            continue
        diff = abs(strike - atm)
        if step > 0 and (diff % step) != 0:
            continue
        option_type = parsed["option_type"]
        lot = int(float(row.get("ls") or row.get("lot_size") or 1))
        tick_raw = row.get("ti") or row.get("tick_size")
        tick = Decimal(str(tick_raw)) if tick_raw not in (None, "") else None
        token = str(row.get("token", "")).strip()
        if not token:
            continue
        instruments.append(
            Instrument(
                exchange=exchange,
                token=token,
                tsym=tsym,
                underlying=underlying,
                expiry_date=datetime.combine(expiry_date, datetime.min.time()),
                strike=strike,
                option_type=option_type,
                lot_size=lot,
                tick_size=tick,
                is_atm=strike == atm,
                in_band=True,
            )
        )

    instruments.sort(key=lambda i: (i.strike, 0 if i.option_type == "CE" else 1))
    return instruments


def chain_anchor_for(
    option_prefix: str,
    expiry_tag: str,
    atm_strike: Decimal | int,
    side: str = "CE",
) -> str:
    letter = "C" if side.upper().startswith("C") else "P"
    return f"{option_prefix.upper()}{expiry_tag.upper()}{letter}{int(atm_strike)}"
