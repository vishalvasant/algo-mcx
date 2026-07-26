from __future__ import annotations

import re
from datetime import date, datetime, timedelta, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
_OPTION_TSYM = re.compile(r"^(?P<ul>[A-Z0-9]+)(?P<tag>\d{2}[A-Z]{3}\d{2})[CP]\d+")

# NIFTY weekly options expire on Tuesday.
_DEFAULT_WEEKLY_WEEKDAY = {
  "GOLD": 1,        # Tuesday (MCX weekly series; refined after backtest)
  "BANKNIFTY": 1,   # Tuesday
  "FINNIFTY": 1,    # Tuesday
  "MIDCPNIFTY": 1,  # Tuesday
}


def parse_expiry_tag(tag: str) -> date | None:
  try:
    return datetime.strptime(tag, "%d%b%y").date()
  except ValueError:
    return None


def format_expiry_tag(d: date) -> str:
  # Force English month abbreviation (locale-independent).
  months = (
    "JAN", "FEB", "MAR", "APR", "MAY", "JUN",
    "JUL", "AUG", "SEP", "OCT", "NOV", "DEC",
  )
  return f"{d.day:02d}{months[d.month - 1]}{d.year % 100:02d}"


def weekly_weekday_for(underlying: str) -> int:
  return _DEFAULT_WEEKLY_WEEKDAY.get(underlying.upper(), 1)


def next_weekly_expiry_dates(
  *,
  underlying: str,
  as_of: date | None = None,
  count: int = 6,
  include_today_if_expiry: bool = True,
) -> list[date]:
  """Upcoming weekly expiry calendar dates (no broker call)."""
  today = as_of or datetime.now(IST).date()
  weekday = weekly_weekday_for(underlying)
  dates: list[date] = []

  # Start from today, walk forward until we have `count` matching weekdays.
  cursor = today
  # If today is expiry weekday but we should skip it (after close), advance one day.
  if not include_today_if_expiry and cursor.weekday() == weekday:
    cursor += timedelta(days=1)

  for _ in range(60):
    if cursor.weekday() == weekday:
      if cursor > today or (include_today_if_expiry and cursor == today):
        dates.append(cursor)
        if len(dates) >= count:
          break
    cursor += timedelta(days=1)
  return dates


def include_expiry_day(as_of: date | None = None, now: datetime | None = None) -> bool:
  """Keep today's expiry only while the cash session is still live."""
  now_ist = (now or datetime.now(IST)).astimezone(IST)
  day = as_of or now_ist.date()
  if now_ist.date() != day:
    return True
  # After 15:30 IST, contracts for today's weekly are done — roll next.
  return now_ist.time() < time(15, 30)


def collect_expiry_tags_from_search(
  rows: list[dict],
  *,
  underlying: str,
  as_of: date | None = None,
  include_today: bool | None = None,
) -> dict[str, date]:
  today = as_of or datetime.now(IST).date()
  if include_today is None:
    include_today = include_expiry_day(as_of=today)

  expiries: dict[str, date] = {}
  ul = underlying.upper()

  for row in rows:
    tsym = str(row.get("tsym", ""))
    if "NXT" in tsym:
      continue
    instname = row.get("instname")
    if instname and instname != "OPTIDX":
      continue
    match = _OPTION_TSYM.match(tsym)
    if match and match.group("ul") == ul:
      tag = match.group("tag").upper()
    else:
      continue
    exp = parse_expiry_tag(tag)
    if exp is None:
      continue
    if exp < today:
      continue
    if exp == today and not include_today:
      continue
    expiries.setdefault(tag, exp)
  return expiries


def nearest_weekly_expiry_tag(
  rows: list[dict],
  *,
  underlying: str,
  as_of: date | None = None,
  include_today: bool | None = None,
) -> str | None:
  expiries = collect_expiry_tags_from_search(
    rows, underlying=underlying, as_of=as_of, include_today=include_today
  )
  if expiries:
    return min(expiries.items(), key=lambda item: item[1])[0]

  # Broker search can return empty after hours — fall back to calendar Tuesdays.
  today = as_of or datetime.now(IST).date()
  if include_today is None:
    include_today = include_expiry_day(as_of=today)
  dates = next_weekly_expiry_dates(
    underlying=underlying,
    as_of=today,
    count=1,
    include_today_if_expiry=include_today,
  )
  return format_expiry_tag(dates[0]) if dates else None


def weekly_expiry_candidates(
  rows: list[dict],
  *,
  underlying: str,
  as_of: date | None = None,
  include_today: bool | None = None,
  limit: int = 4,
) -> list[str]:
  """Ordered list of weekly expiry tags to try (search first, then calendar)."""
  today = as_of or datetime.now(IST).date()
  if include_today is None:
    include_today = include_expiry_day(as_of=today)

  expiries = collect_expiry_tags_from_search(
    rows, underlying=underlying, as_of=today, include_today=include_today
  )
  tags = [t for t, _ in sorted(expiries.items(), key=lambda item: item[1])]

  for d in next_weekly_expiry_dates(
    underlying=underlying,
    as_of=today,
    count=limit,
    include_today_if_expiry=include_today,
  ):
    tag = format_expiry_tag(d)
    if tag not in tags:
      tags.append(tag)

  return tags[:limit]


def chain_anchor_symbol(underlying: str, expiry_tag: str, atm_strike: int, side: str = "CE") -> str:
  ul = underlying.upper()
  return f"{ul}{expiry_tag.upper()}{side[0]}{int(atm_strike)}"
