from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from algomcx.option_data.greeks import compute_greeks, implied_volatility, years_to_expiry

IST = ZoneInfo("Asia/Kolkata")


def test_years_to_expiry_positive():
  expiry = date(2026, 7, 21)
  now = datetime(2026, 7, 15, 10, 0, tzinfo=IST)
  t = years_to_expiry(expiry, now=now)
  assert 0.01 < t < 0.05


def test_atm_call_greeks_reasonable():
  g = compute_greeks(
    spot=24050,
    strike=24050,
    premium=150,
    option_type="CE",
    expiry=date(2026, 7, 21),
    rate=0.065,
    now=datetime(2026, 7, 15, 10, 0, tzinfo=IST),
  )
  assert g.iv is not None and 0.05 < g.iv < 1.5
  assert g.delta is not None and 0.35 < g.delta < 0.65
  assert g.theta is not None and g.theta < 0
  assert g.vega is not None and g.vega > 0
  assert g.gamma is not None and g.gamma > 0


def test_atm_put_delta_negative():
  g = compute_greeks(
    spot=24050,
    strike=24050,
    premium=160,
    option_type="PE",
    expiry=date(2026, 7, 21),
    rate=0.065,
    now=datetime(2026, 7, 15, 10, 0, tzinfo=IST),
  )
  assert g.delta is not None and -0.65 < g.delta < -0.35


def test_iv_rejects_zero_premium():
  assert (
    implied_volatility(24050, 24050, 0.02, 0.065, 0.0, "CE") is None
  )


def test_trend_continuation_bear():
  from decimal import Decimal

  from algomcx.features.engine import _detect_trend_continuation
  from algomcx.models.events import Bias, Candle, CandleInterval

  def c(close: float) -> Candle:
    x = Decimal(str(close))
    return Candle(
      instrument_token="26000",
      ts=datetime.now(tz=timezone.utc),
      open=x,
      high=x + 2,
      low=x - 2,
      close=x,
      volume=1000,
      interval=CandleInterval.M3,
    )

  vwap = Decimal("100")
  m3 = [c(90), c(88), c(87)]
  m1 = [c(88), c(86)]
  assert (
    _detect_trend_continuation(
      m3,
      m1,
      vwap,
      Bias.BEARISH,
      min_bars=3,
      min_distance=Decimal("3"),
      max_distance=Decimal("50"),
      require_momentum=True,
    )
    == "vwap_trend_bear"
  )


def test_trend_continuation_rejects_mixed_side():
  from decimal import Decimal

  from algomcx.features.engine import _detect_trend_continuation
  from algomcx.models.events import Bias, Candle, CandleInterval

  def c(close: float) -> Candle:
    x = Decimal(str(close))
    return Candle(
      instrument_token="26000",
      ts=datetime.now(tz=timezone.utc),
      open=x,
      high=x + 2,
      low=x - 2,
      close=x,
      volume=1000,
      interval=CandleInterval.M3,
    )

  vwap = Decimal("100")
  m3 = [c(90), c(101), c(87)]
  m1 = [c(88), c(86)]
  assert (
    _detect_trend_continuation(
      m3,
      m1,
      vwap,
      Bias.BEARISH,
      min_bars=3,
      min_distance=Decimal("3"),
      max_distance=Decimal("50"),
      require_momentum=True,
    )
    is None
  )
