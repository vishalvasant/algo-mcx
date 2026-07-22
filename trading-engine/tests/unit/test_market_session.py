"""NSE session clock — weekends must stay closed."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from algomcx.market_session import is_market_open, is_nse_weekday, session_label

IST = ZoneInfo("Asia/Kolkata")
MS = {"market_open": "09:15", "market_close": "15:30"}


def test_weekend_not_weekday() -> None:
    sat = datetime(2026, 7, 18, 12, 0, tzinfo=IST)  # Saturday
    sun = datetime(2026, 7, 19, 12, 0, tzinfo=IST)
    assert not is_nse_weekday(sat.date())
    assert not is_nse_weekday(sun.date())
    assert not is_market_open(MS, now=sat)
    assert not is_market_open(MS, now=sun)
    assert session_label(MS, now=sat) == "CLOSED"
    assert session_label(MS, now=sun) == "CLOSED"


def test_weekday_open_hours() -> None:
    fri_open = datetime(2026, 7, 17, 10, 0, tzinfo=IST)
    fri_pre = datetime(2026, 7, 17, 9, 0, tzinfo=IST)
    fri_after = datetime(2026, 7, 17, 16, 0, tzinfo=IST)
    assert is_nse_weekday(fri_open.date())
    assert is_market_open(MS, now=fri_open)
    assert not is_market_open(MS, now=fri_pre)
    assert not is_market_open(MS, now=fri_after)
    assert session_label(MS, now=fri_open) == "OPEN"
    assert session_label(MS, now=fri_pre) == "PRE_MARKET"
    assert session_label(MS, now=fri_after) == "CLOSED"
