"""Unit tests for reclaim / structure / bias PE-CE fairness."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from algomcx.features.engine import (
  FeatureEngine,
  _detect_reclaim,
  _structure_5m,
)
from algomcx.models.events import Bias, Candle, CandleInterval


def _bar(close: str, high: str | None = None, low: str | None = None) -> Candle:
  c = Decimal(close)
  return Candle(
    instrument_token="26000",
    ts=datetime.now(tz=timezone.utc),
    open=c,
    high=Decimal(high) if high else c,
    low=Decimal(low) if low else c,
    close=c,
    volume=100,
    interval=CandleInterval.M3,
  )


def test_reclaim_bear_when_close_below_vwap():
  vwap = Decimal("24100")
  bars = [_bar("24150"), _bar("24120"), _bar("24080")]
  assert _detect_reclaim(bars, vwap, 5) == "vwap_reclaim_bear"


def test_reclaim_bull_when_close_above_vwap():
  vwap = Decimal("24100")
  bars = [_bar("24050"), _bar("24080"), _bar("24120")]
  assert _detect_reclaim(bars, vwap, 5) == "vwap_reclaim_bull"


def test_reclaim_exact_vwap_uses_prior_side_for_bear():
  """Equal close must not default to bull and block PE."""
  vwap = Decimal("24100")
  bars = [_bar("24150"), _bar("24100")]
  assert _detect_reclaim(bars, vwap, 5) == "vwap_reclaim_bear"


def test_structure_tie_is_mixed_not_hhhl():
  # Both hhhl and lllh conditions can hold on flat extremes
  bars = [
    _bar("100", high="110", low="90"),
    _bar("100", high="110", low="90"),
    _bar("100", high="110", low="90"),
  ]
  assert _structure_5m(bars, 3) == "mixed"


def test_bias_uses_last_m1_close_not_live_ltp_flip():
  """Live LTP above VWAP while last 1m closed below → bearish (PE-eligible)."""
  md = MagicMock()
  md.spot_ltp = Decimal("24120")  # live flipped above
  vwap_bars = [
    _bar("24080"),
    _bar("24090"),
    _bar("24095"),  # last close still below VWAP
  ]
  for b in vwap_bars:
    object.__setattr__(b, "interval", CandleInterval.M1)
  md.candles = MagicMock(
    side_effect=lambda interval: {
      CandleInterval.M1: vwap_bars,
      CandleInterval.M3: vwap_bars,
      CandleInterval.M5: vwap_bars,
    }[interval]
  )

  cfg = MagicMock()
  cfg.strategy = {
    "vwap_reclaim": {
      "setup_lookback_bars": 5,
      "max_distance_to_vwap_points": 80,
      "trigger_lookback_bars": 3,
    },
    "vwap_pullback": {},
    "vwap_trend": {},
  }
  import algomcx.features.engine as fe_mod

  original = fe_mod.session_vwap
  fe_mod.session_vwap = lambda _bars: Decimal("24100")
  try:
    fe = FeatureEngine(cfg, md)
    snap = fe.compute()
    assert snap.bias_5m == Bias.BEARISH
    assert snap.nifty_spot == Decimal("24120")
  finally:
    fe_mod.session_vwap = original
