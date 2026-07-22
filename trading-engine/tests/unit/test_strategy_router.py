from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

from algomcx.features.engine import (
  _detect_pullback,
  _detect_reclaim,
  _detect_reclaim_trigger,
)
from algomcx.models.events import (
  Bias,
  Candle,
  CandleInterval,
  CandidateSignal,
  FeatureSnapshot,
  Instrument,
  MarketRegime,
  OptionState,
)
from algomcx.quality.gate import QualityGate
from algomcx.regime.classifier import _normalize_100
from algomcx.scanner.vwap_pullback import VwapPullbackScanner
from algomcx.scanner.vwap_reclaim import VwapReclaimScanner
from algomcx.strategy.router import StrategyRouter


def _candle(close: float, high: float | None = None, low: float | None = None) -> Candle:
  c = Decimal(str(close))
  h = Decimal(str(high if high is not None else close + 2))
  lo = Decimal(str(low if low is not None else close - 2))
  return Candle(
    instrument_token="26000",
    ts=datetime.now(tz=timezone.utc),
    open=c,
    high=h,
    low=lo,
    close=c,
    volume=1000,
    interval=CandleInterval.M1,
  )


def _minimal_config(**strategy_overrides):
  base_strategy = {
    "strategy_version": "test",
    "router": {"min_confidence": 70, "enabled_strategies": ["vwap_reclaim", "vwap_pullback"]},
    "regime": {
      "block_sideways": True,
      "max_risk_score_to_trade": 90,
      "block_high_volatility": False,
    },
    "vwap_reclaim": {
      "setup_lookback_bars": 5,
      "trigger_lookback_bars": 3,
      "max_distance_to_vwap_points": 20,
    },
    "vwap_pullback": {
      "setup_lookback_bars": 8,
      "min_extension_points": 8,
      "max_distance_to_vwap_points": 12,
      "trigger_lookback_bars": 3,
    },
  }
  base_strategy.update(strategy_overrides)
  return SimpleNamespace(
    strategy=base_strategy,
    symbols={"strike_step": 50},
    validator={},
    risk={},
    runtime={},
  )


def test_normalize_100():
  probs = _normalize_100({"a": 1, "b": 1, "c": 2})
  assert abs(sum(probs.values()) - 100.0) < 0.01


def test_reclaim_lookback_detects_prior_below():
  vwap = Decimal("100")
  # prior below, current above — not only adjacent if lookback includes older bars
  bars = [
    _candle(95),
    _candle(96),
    _candle(97),
    _candle(101),
  ]
  assert _detect_reclaim(bars, vwap, lookback=5) == "vwap_reclaim_bull"
  assert _detect_reclaim_trigger(bars, vwap, lookback=3) == "vwap_reclaim_cross_up"


def test_pullback_after_extension():
  vwap = Decimal("100")
  bars = [
    _candle(112),  # extended
    _candle(110),
    _candle(105),  # pulled back near VWAP
  ]
  assert (
    _detect_pullback(
      bars,
      vwap,
      Bias.BULLISH,
      lookback=8,
      min_extension=Decimal("8"),
      max_distance=Decimal("12"),
    )
    == "vwap_pullback_bull"
  )


def test_reclaim_scanner_requires_trigger_alignment():
  config = _minimal_config()
  scanner = VwapReclaimScanner(config)  # type: ignore[arg-type]
  features = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("101"),
    session_vwap=Decimal("100"),
    bias_5m=Bias.BULLISH,
    setup_3m="vwap_reclaim_bull",
    trigger_1m="vwap_reclaim_cross_down",  # wrong direction
  )
  universe = SimpleNamespace(
    atm_ce=Instrument(
      exchange="NFO",
      token="1",
      tsym="NIFTYCE",
      underlying="NIFTY",
      strike=Decimal("24500"),
      option_type="CE",
      lot_size=65,
    ),
    atm_pe=None,
    atm_strike=Decimal("24500"),
  )
  option = OptionState(instrument_token="1", tsym="NIFTYCE", ltp=Decimal("120"))
  assert scanner.scan(features, universe, option) is None  # type: ignore[arg-type]

  features.trigger_1m = "vwap_reclaim_cross_up"
  assert scanner.scan(features, universe, option) is not None  # type: ignore[arg-type]


def test_router_no_trade_when_regime_blocks():
  config = _minimal_config()
  quality = QualityGate(config)  # type: ignore[arg-type]
  router = StrategyRouter(
    config,  # type: ignore[arg-type]
    [VwapReclaimScanner(config), VwapPullbackScanner(config)],  # type: ignore[arg-type]
    quality,
  )
  features = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("100"),
    session_vwap=Decimal("100"),
    bias_5m=Bias.NEUTRAL,
  )
  regime = MarketRegime(
    ts=datetime.now(tz=timezone.utc),
    primary="sideways",
    probabilities={"sideways": 100},
    trade_allowed=False,
    risk_score=80,
    reasons=["primary_sideways_blocks_trade"],
  )
  universe = SimpleNamespace(atm_ce=None, atm_pe=None, atm_strike=Decimal("0"))
  decision, signal = router.route(features, regime, universe, {})  # type: ignore[arg-type]
  assert signal is None
  assert decision.selected_strategy == "NO_TRADE"


def test_quality_gate_scores_aligned_reclaim():
  config = _minimal_config()
  gate = QualityGate(config)  # type: ignore[arg-type]
  features = FeatureSnapshot(
    ts=datetime.now(tz=timezone.utc),
    nifty_spot=Decimal("101"),
    session_vwap=Decimal("100"),
    bias_5m=Bias.BULLISH,
    setup_3m="vwap_reclaim_bull",
    trigger_1m="vwap_reclaim_cross_up",
    extra={
      "abs_distance_to_vwap_points": 3,
      "max_distance_to_vwap_points": 15,
      "structure_5m": "hhhl",
      "ema9": 101.5,
      "ema21": 100.2,
      "chain": {"oi_confirms_ce": True, "long_build_up": True},
    },
  )
  signal = CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type="vwap_reclaim",
    side="CE",
    instrument_token="1",
    tsym="X",
    strategy_version="t",
    feature_snapshot=features,
  )
  regime = MarketRegime(
    ts=datetime.now(tz=timezone.utc),
    primary="trending_up",
    probabilities={"trending_up": 60},
    trade_allowed=True,
    risk_score=30,
  )
  conf, logs = gate.score(
    signal,
    features,
    regime,
    context={
      "ltp": 120,
      "option_vwap": 115,
      "volume": 200000,
      "oi": 600000,
      "delta": 0.52,
      "gamma": 0.0015,
      "iv": 0.14,
      "spread_pct": 1.0,
    },
  )
  assert conf >= 75
  assert gate.passes(conf)
  assert logs
  assert "confidence_components" in signal.scanner_metadata
