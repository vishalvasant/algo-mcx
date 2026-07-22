"""NSE cash-session clock helpers (IST)."""
from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def is_nse_weekday(d: date | None = None) -> bool:
    """True Mon–Fri (NSE does not trade Sat/Sun)."""
    day = d or datetime.now(IST).date()
    return day.weekday() < 5


def is_market_open(
    market_session: dict,
    *,
    now: datetime | None = None,
) -> bool:
    """True only on weekdays between market_open and market_close IST."""
    ist = now.astimezone(IST) if now is not None else datetime.now(IST)
    if not is_nse_weekday(ist.date()):
        return False
    open_h, open_m = map(int, str(market_session["market_open"]).split(":"))
    close_h, close_m = map(int, str(market_session["market_close"]).split(":"))
    open_ts = ist.replace(hour=open_h, minute=open_m, second=0, microsecond=0)
    close_ts = ist.replace(hour=close_h, minute=close_m, second=0, microsecond=0)
    return open_ts <= ist <= close_ts


def session_label(
    market_session: dict,
    *,
    now: datetime | None = None,
) -> str:
    """PRE_MARKET / OPEN / CLOSED — weekends always CLOSED."""
    ist = now.astimezone(IST) if now is not None else datetime.now(IST)
    if not is_nse_weekday(ist.date()):
        return "CLOSED"
    open_h, open_m = map(int, str(market_session["market_open"]).split(":"))
    close_h, close_m = map(int, str(market_session["market_close"]).split(":"))
    open_t = time(open_h, open_m)
    close_t = time(close_h, close_m)
    t = ist.timetz().replace(tzinfo=None)
    if t < open_t:
        return "PRE_MARKET"
    if t <= close_t:
        return "OPEN"
    return "CLOSED"
