from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from algomcx.contract_selector.expiry import (
  format_expiry_tag,
  include_expiry_day,
  nearest_weekly_expiry_tag,
  next_weekly_expiry_dates,
  weekly_expiry_candidates,
)

IST = ZoneInfo("Asia/Kolkata")


def test_next_weekly_after_tuesday_expiry():
  # 14 Jul 2026 is Tuesday (NIFTY weekly expiry day).
  dates = next_weekly_expiry_dates(
    underlying="NIFTY",
    as_of=date(2026, 7, 14),
    count=2,
    include_today_if_expiry=True,
  )
  assert dates[0] == date(2026, 7, 14)
  assert dates[1] == date(2026, 7, 21)

  rolled = next_weekly_expiry_dates(
    underlying="NIFTY",
    as_of=date(2026, 7, 14),
    count=2,
    include_today_if_expiry=False,
  )
  assert rolled[0] == date(2026, 7, 21)


def test_tomorrow_picks_next_tuesday():
  tag = nearest_weekly_expiry_tag([], underlying="NIFTY", as_of=date(2026, 7, 15))
  assert tag == "21JUL26"


def test_after_close_on_expiry_day_skips_today():
  now = datetime(2026, 7, 14, 16, 0, tzinfo=IST)
  assert include_expiry_day(as_of=date(2026, 7, 14), now=now) is False
  tags = weekly_expiry_candidates([], underlying="NIFTY", as_of=date(2026, 7, 14), include_today=False)
  assert tags[0] == "21JUL26"


def test_during_session_keeps_expiry_day():
  now = datetime(2026, 7, 14, 11, 0, tzinfo=IST)
  assert include_expiry_day(as_of=date(2026, 7, 14), now=now) is True
  assert format_expiry_tag(date(2026, 7, 21)) == "21JUL26"


def test_search_rows_prefer_nearest():
  rows = [
    {"tsym": "NIFTY14JUL26C24000", "instname": "OPTIDX"},
    {"tsym": "NIFTY21JUL26C24000", "instname": "OPTIDX"},
    {"tsym": "NIFTY28JUL26C24000", "instname": "OPTIDX"},
  ]
  assert nearest_weekly_expiry_tag(rows, underlying="NIFTY", as_of=date(2026, 7, 15)) == "21JUL26"
  assert nearest_weekly_expiry_tag(
    rows, underlying="NIFTY", as_of=date(2026, 7, 14), include_today=False
  ) == "21JUL26"
