"""Technical indicators for institutional rulebook features."""
from __future__ import annotations

from datetime import datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from algomcx.models.events import Candle, CandleInterval

IST = ZoneInfo("Asia/Kolkata")


def ema(values: list[Decimal], period: int) -> Decimal | None:
  if len(values) < period or period < 1:
    return None
  k = Decimal("2") / (Decimal(period) + Decimal("1"))
  e = sum(values[:period], Decimal("0")) / Decimal(period)
  for v in values[period:]:
    e = v * k + e * (Decimal("1") - k)
  return e


def ema_series(values: list[Decimal], period: int) -> list[Decimal | None]:
  out: list[Decimal | None] = [None] * len(values)
  if len(values) < period:
    return out
  k = Decimal("2") / (Decimal(period) + Decimal("1"))
  e = sum(values[:period], Decimal("0")) / Decimal(period)
  out[period - 1] = e
  for i in range(period, len(values)):
    e = values[i] * k + e * (Decimal("1") - k)
    out[i] = e
  return out


def aggregate_from_m5(m5: list[Candle], minutes: int = 15) -> list[Candle]:
  """Build higher-TF bars from 5m (15m = 3×5m)."""
  if not m5 or minutes < 5 or minutes % 5 != 0:
    return []
  n = minutes // 5
  out: list[Candle] = []
  for i in range(0, len(m5) - n + 1, n):
    chunk = m5[i : i + n]
    if len(chunk) < n:
      break
    out.append(
      Candle(
        instrument_token=chunk[0].instrument_token,
        ts=chunk[0].ts,
        open=chunk[0].open,
        high=max(c.high for c in chunk),
        low=min(c.low for c in chunk),
        close=chunk[-1].close,
        volume=sum((c.volume or 0) for c in chunk) or None,
        interval=CandleInterval.M5,  # logical 15m; reuse enum
      )
    )
  return out


def cpr_levels(prev_high: Decimal, prev_low: Decimal, prev_close: Decimal) -> dict[str, Decimal]:
  pivot = (prev_high + prev_low + prev_close) / Decimal("3")
  bc = (prev_high + prev_low) / Decimal("2")
  tc = pivot * Decimal("2") - bc
  # Order TC above BC conventionally when bullish prior day
  top = max(tc, bc)
  bot = min(tc, bc)
  return {
    "pivot": pivot,
    "bc": bot,
    "tc": top,
    "r1": pivot + (pivot - prev_low),
    "s1": pivot - (prev_high - pivot),
  }


def opening_range(
  m1: list[Candle],
  *,
  minutes: int = 15,
  session_open: time = time(9, 15),
) -> dict[str, Decimal | datetime] | None:
  if not m1:
    return None
  first = m1[0].ts.astimezone(IST)
  start = first.replace(
    hour=session_open.hour, minute=session_open.minute, second=0, microsecond=0
  )
  end = start + timedelta(minutes=minutes)
  bars = [c for c in m1 if start <= c.ts.astimezone(IST) < end]
  if not bars:
    return None
  return {
    "or_high": max(c.high for c in bars),
    "or_low": min(c.low for c in bars),
    "or_open": bars[0].open,
    "or_end_ts": end,
  }


def session_option_vwap(
  samples: list[tuple[Decimal, int]],
) -> Decimal | None:
  """VWAP from (price, volume) samples. Equal-weight if volume is 0."""
  if not samples:
    return None
  num = Decimal("0")
  den = Decimal("0")
  for px, vol in samples:
    w = Decimal(vol) if vol and vol > 0 else Decimal("1")
    num += px * w
    den += w
  if den <= 0:
    return None
  return num / den
