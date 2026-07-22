from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# India overnight / risk-free proxy for display Greeks
DEFAULT_RATE = 0.065


@dataclass(frozen=True)
class OptionGreeks:
  iv: float | None  # decimal, e.g. 0.18 = 18%
  delta: float | None
  gamma: float | None
  theta: float | None  # per calendar day
  vega: float | None  # per 1% vol


def years_to_expiry(expiry: date, *, now: datetime | None = None) -> float:
  """Fractional years to 15:30 IST expiry (weekly). Floor to avoid zero."""
  now_ist = (now or datetime.now(tz=IST)).astimezone(IST)
  expiry_dt = datetime.combine(expiry, datetime.min.time()).replace(
    hour=15, minute=30, tzinfo=IST
  )
  seconds = (expiry_dt - now_ist).total_seconds()
  if seconds <= 0:
    return 1.0 / (365.0 * 24.0 * 60.0)  # ~1 minute
  return max(seconds / (365.0 * 24.0 * 3600.0), 1e-6)


def _norm_cdf(x: float) -> float:
  return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
  return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _bs_price(
  spot: float,
  strike: float,
  t: float,
  rate: float,
  vol: float,
  option_type: str,
) -> float:
  if vol <= 0 or t <= 0 or spot <= 0 or strike <= 0:
    intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
    return max(intrinsic, 0.0)
  sqrt_t = math.sqrt(t)
  d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * sqrt_t)
  d2 = d1 - vol * sqrt_t
  if option_type == "CE":
    return spot * _norm_cdf(d1) - strike * math.exp(-rate * t) * _norm_cdf(d2)
  return strike * math.exp(-rate * t) * _norm_cdf(-d2) - spot * _norm_cdf(-d1)


def implied_volatility(
  spot: float,
  strike: float,
  t: float,
  rate: float,
  premium: float,
  option_type: str,
  *,
  max_iter: int = 40,
) -> float | None:
  """Newton-Raphson IV; returns None if not solvable."""
  if spot <= 0 or strike <= 0 or premium <= 0 or t <= 0:
    return None

  intrinsic = max(spot - strike, 0.0) if option_type == "CE" else max(strike - spot, 0.0)
  # Premium below intrinsic (stale quote) — still allow slight cushion
  if premium < intrinsic * 0.5:
    return None

  vol = 0.25
  for _ in range(max_iter):
    price = _bs_price(spot, strike, t, rate, vol, option_type)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(spot / strike) + (rate + 0.5 * vol * vol) * t) / (vol * sqrt_t)
    vega = spot * _norm_pdf(d1) * sqrt_t
    if vega < 1e-12:
      break
    diff = price - premium
    if abs(diff) < 1e-4:
      return max(0.01, min(vol, 5.0))
    vol -= diff / vega
    if vol <= 0.001:
      vol = 0.001
    if vol > 5.0:
      vol = 5.0
  # Accept if close enough
  if abs(_bs_price(spot, strike, t, rate, vol, option_type) - premium) < 0.5:
    return max(0.01, min(vol, 5.0))
  return None


def compute_greeks(
  *,
  spot: float | Decimal,
  strike: float | Decimal,
  premium: float | Decimal | None,
  option_type: str,
  expiry: date | None,
  rate: float = DEFAULT_RATE,
  now: datetime | None = None,
) -> OptionGreeks:
  """Derive IV + Greeks from LTP via Black-Scholes (broker does not stream Greeks)."""
  empty = OptionGreeks(None, None, None, None, None)
  if premium is None or expiry is None:
    return empty

  s = float(spot)
  k = float(strike)
  p = float(premium)
  ot = option_type.upper()
  if ot not in ("CE", "PE") or s <= 0 or k <= 0 or p <= 0:
    return empty

  t = years_to_expiry(expiry, now=now)
  iv = implied_volatility(s, k, t, rate, p, ot)
  if iv is None:
    return empty

  sqrt_t = math.sqrt(t)
  d1 = (math.log(s / k) + (rate + 0.5 * iv * iv) * t) / (iv * sqrt_t)
  d2 = d1 - iv * sqrt_t
  pdf = _norm_pdf(d1)

  if ot == "CE":
    delta = _norm_cdf(d1)
    theta = (
      -(s * pdf * iv) / (2.0 * sqrt_t)
      - rate * k * math.exp(-rate * t) * _norm_cdf(d2)
    ) / 365.0
  else:
    delta = _norm_cdf(d1) - 1.0
    theta = (
      -(s * pdf * iv) / (2.0 * sqrt_t)
      + rate * k * math.exp(-rate * t) * _norm_cdf(-d2)
    ) / 365.0

  gamma = pdf / (s * iv * sqrt_t)
  vega = s * pdf * sqrt_t / 100.0  # per 1 vol point

  return OptionGreeks(
    iv=iv,
    delta=delta,
    gamma=gamma,
    theta=theta,
    vega=vega,
  )
