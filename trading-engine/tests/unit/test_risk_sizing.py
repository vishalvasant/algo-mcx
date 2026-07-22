"""Unit tests for confidence-based lot sizing."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from algomcx.models.events import Bias, CandidateSignal, FeatureSnapshot, OptionState
from algomcx.risk.engine import (
  DailyRiskSnapshot,
  RiskEngine,
  fit_lots_to_capital,
  lots_for_confidence,
)


def _signal(confidence: int) -> CandidateSignal:
  return CandidateSignal(
    ts=datetime.now(tz=timezone.utc),
    setup_type="vwap_trend",
    side="PE",
    instrument_token="1",
    tsym="NIFTYPE",
    strategy_version="t",
    confidence=confidence,
    scanner_metadata={"lot_size": 65},
    feature_snapshot=FeatureSnapshot(
      ts=datetime.now(tz=timezone.utc),
      bias_5m=Bias.BEARISH,
    ),
  )


def _risk_cfg(**overrides):
  cfg = {
    "default_lots": 1,
    "max_premium_pct_of_available": 65,
    "max_deployed_pct_of_equity": 85,
    "max_daily_loss": 10000,
    "max_trades_per_day": 0,
    "max_concurrent_positions": 0,
    "max_consecutive_losses": 5,
    "confidence_lot_sizing": {
      "enabled": True,
      "max_lots": 3,
      "tiers": [
        {"min_confidence": 70, "lots": 1},
        {"min_confidence": 80, "lots": 2},
        {"min_confidence": 90, "lots": 3},
      ],
    },
  }
  cfg.update(overrides)
  return cfg


def test_lots_for_confidence_tiers():
  cfg = _risk_cfg()
  assert lots_for_confidence(cfg, 69) == 1  # below first tier → default
  assert lots_for_confidence(cfg, 70) == 1
  assert lots_for_confidence(cfg, 79) == 1
  assert lots_for_confidence(cfg, 80) == 2
  assert lots_for_confidence(cfg, 89) == 2
  assert lots_for_confidence(cfg, 90) == 3
  assert lots_for_confidence(cfg, 100) == 3


def test_lots_for_confidence_disabled_uses_default():
  cfg = _risk_cfg()
  cfg["confidence_lot_sizing"]["enabled"] = False
  cfg["default_lots"] = 2
  assert lots_for_confidence(cfg, 95) == 2


def test_lots_capped_by_max_lots():
  cfg = _risk_cfg()
  cfg["confidence_lot_sizing"]["max_lots"] = 2
  assert lots_for_confidence(cfg, 95) == 2


@pytest.mark.asyncio
async def test_size_entry_uses_confidence_lots():
  config = SimpleNamespace(risk=_risk_cfg())
  risk = RiskEngine(config)  # type: ignore[arg-type]

  signal = _signal(92)
  # 3 × 65 × 80 = 15600 ≤ 35% of 50k (17500)
  option = OptionState(instrument_token="1", tsym="NIFTYPE", ltp=Decimal("80"))
  snap = DailyRiskSnapshot(
    trade_date=date.today(),
    starting_capital=Decimal("50000"),
    available_capital=Decimal("50000"),
    deployed_capital=Decimal("0"),
    realized_pnl=Decimal("0"),
    trade_count=0,
    consecutive_losses=0,
    kill_switch=False,
    entries_blocked=False,
  )

  sizing = await risk.size_entry(signal, option, snap)
  assert sizing.approved
  assert sizing.lots == 3
  assert sizing.quantity == 195  # 65 * 3
  assert sizing.confidence == 92
  assert sizing.premium_required == Decimal("15600")


@pytest.mark.asyncio
async def test_size_entry_steps_down_when_capital_tight():
  """3 lots would exceed 65% premium cap; step down until it fits."""
  config = SimpleNamespace(risk=_risk_cfg())
  risk = RiskEngine(config)  # type: ignore[arg-type]

  signal = _signal(95)
  # 3 lots * 65 * 200 = 39000 > 65% of 50000 (=32500) → must step down
  # 2 lots * 65 * 200 = 26000 <= 32500
  option = OptionState(instrument_token="1", tsym="NIFTYPE", ltp=Decimal("200"))
  snap = DailyRiskSnapshot(
    trade_date=date.today(),
    starting_capital=Decimal("50000"),
    available_capital=Decimal("50000"),
    deployed_capital=Decimal("0"),
    realized_pnl=Decimal("0"),
    trade_count=0,
    consecutive_losses=0,
    kill_switch=False,
    entries_blocked=False,
  )

  sizing = await risk.size_entry(signal, option, snap)
  assert sizing.approved
  assert sizing.lots == 2
  assert sizing.quantity == 130


def test_fit_lots_fills_up_toward_deploy_when_conf_high():
  """Tier say 2 lots at conf 87, but room allows 3 → fill to 3."""
  cfg = _risk_cfg()
  lots, prem = fit_lots_to_capital(
    cfg,
    confidence=87,
    entry_ltp=Decimal("80"),
    lot_size=65,
    available=Decimal("50000"),
    deployed=Decimal("0"),
    equity=Decimal("50000"),
  )
  assert lots == 3
  assert prem == Decimal("15600")


@pytest.mark.asyncio
async def test_loss_stops_disabled_when_zero():
  """max_consecutive_losses=0 / max_daily_loss=0 must not block entries."""
  config = SimpleNamespace(risk=_risk_cfg(max_consecutive_losses=0, max_daily_loss=0))
  risk = RiskEngine(config)  # type: ignore[arg-type]
  signal = _signal(80)
  option = OptionState(instrument_token="1", tsym="NIFTYPE", ltp=Decimal("80"))
  snap = DailyRiskSnapshot(
    trade_date=date.today(),
    starting_capital=Decimal("50000"),
    available_capital=Decimal("40000"),
    deployed_capital=Decimal("0"),
    realized_pnl=Decimal("-25000"),
    trade_count=12,
    consecutive_losses=20,
    kill_switch=False,
    entries_blocked=False,
  )
  sizing = await risk.size_entry(signal, option, snap)
  assert sizing.approved
  assert sizing.rejection_reason is None
