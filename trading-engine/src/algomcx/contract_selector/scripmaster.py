from __future__ import annotations

import csv
import io
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from urllib.request import urlopen

import structlog

from algomcx.contract_selector.expiry import (
  _OPTION_TSYM,
  parse_expiry_tag,
)
from algomcx.models.events import Instrument

logger = structlog.get_logger(__name__)

# Public Flattrade contract master (available after hours when REST search/chain are empty).
FLATTRADE_NFO_INDEX_CSV = (
  "https://flattrade.s3.ap-south-1.amazonaws.com/scripmaster/Nfo_Index_Derivatives.csv"
)


def _parse_master_expiry(raw: str) -> date | None:
  raw = (raw or "").strip()
  if not raw:
    return None
  for fmt in ("%d-%b-%Y", "%d-%b-%y", "%Y-%m-%d"):
    try:
      return datetime.strptime(raw, fmt).date()
    except ValueError:
      continue
  try:
    return datetime.strptime(raw.upper(), "%d-%b-%Y").date()
  except ValueError:
    return parse_expiry_tag(raw.upper())


def download_nfo_index_rows(url: str = FLATTRADE_NFO_INDEX_CSV) -> list[dict[str, str]]:
  with urlopen(url, timeout=60) as resp:  # noqa: S310 — fixed Flattrade CDN URL
    payload = resp.read().decode("utf-8", errors="replace")
  reader = csv.DictReader(io.StringIO(payload))
  return [dict(row) for row in reader]


def instruments_from_scripmaster(
  rows: list[dict[str, str]],
  *,
  underlying: str,
  expiry_tag: str,
  atm: Decimal,
  band_points: Decimal,
  exchange: str = "NFO",
) -> list[Instrument]:
  expiry_date = parse_expiry_tag(expiry_tag)
  if expiry_date is None:
    return []

  instruments: list[Instrument] = []
  ul = underlying.upper()
  for row in rows:
    symbol = (row.get("Symbol") or row.get("symbol") or "").upper()
    if symbol != ul:
      continue
    instrument = (row.get("Instrument") or row.get("instrument") or "").upper()
    allowed = {"OPTIDX", "OPTFUT", "OPTCOM", "OPTSTK"}
    if instrument and instrument not in allowed:
      continue

    tsym = row.get("Tradingsymbol") or row.get("TradingSymbol") or row.get("tsym") or ""
    token = str(row.get("Token") or row.get("token") or "")
    if not tsym or not token:
      continue

    exp = _parse_master_expiry(row.get("Expiry") or row.get("expiry") or "")
    if exp is None:
      m = _OPTION_TSYM.match(tsym)
      if not m:
        continue
      exp = parse_expiry_tag(m.group("tag"))
    if exp != expiry_date:
      continue

    try:
      strike = Decimal(str(row.get("Strike") or row.get("strike") or "0"))
    except Exception:
      continue
    if abs(strike - atm) > band_points:
      continue

    opt = (row.get("Optiontype") or row.get("OptionType") or row.get("optt") or "").upper()
    if opt in ("CE", "C", "CALL"):
      option_type = "CE"
    elif opt in ("PE", "P", "PUT"):
      option_type = "PE"
    else:
      continue

    lot = int(float(row.get("Lotsize") or row.get("ls") or 65))
    instruments.append(
      Instrument(
        exchange=exchange,
        token=token,
        tsym=tsym,
        underlying=ul,
        expiry_date=datetime.combine(exp, datetime.min.time()),
        strike=strike,
        option_type=option_type,
        lot_size=lot,
        tick_size=Decimal("0.05"),
        is_atm=strike == atm,
        in_band=True,
      )
    )

  instruments.sort(key=lambda i: (i.strike, 0 if i.option_type == "CE" else 1))
  return instruments


def load_weekly_band_from_scripmaster(
  *,
  underlying: str,
  expiry_tag: str,
  atm: Decimal,
  band_points: Decimal,
  exchange: str = "NFO",
) -> list[Instrument]:
  rows = download_nfo_index_rows()
  instruments = instruments_from_scripmaster(
    rows,
    underlying=underlying,
    expiry_tag=expiry_tag,
    atm=atm,
    band_points=band_points,
    exchange=exchange,
  )
  logger.info(
    "scripmaster_loaded",
    underlying=underlying,
    expiry=expiry_tag,
    atm=str(atm),
    count=len(instruments),
  )
  return instruments
