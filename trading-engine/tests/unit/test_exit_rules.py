"""Unit tests for early_invalidation + trail exits."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock

from algomcx.position.exit_rules import evaluate_momentum_exit


def _cfg(**overrides):
  cfg = {
    "min_hold_seconds": 20,
    "max_hold_minutes": 0,
    "bias_flip_exit": True,
    "bias_flip_buffer_points": 8,
    "adverse_move_pct_from_entry": 12,
    "min_profit_before_trail_pct": 12,
    "trail_giveback_pct": 35,
    "early_invalidation_enabled": True,
    "early_invalidation_min_hold_seconds": 45,
    "early_invalidation_max_mfe_pct": 3,
    "early_invalidation_loss_pct": 7,
  }
  cfg.update(overrides)
  return cfg


def _md_bull():
  md = MagicMock()
  md.spot_ltp = Decimal("24200")
  md.session_vwap_value = Decimal("24150")
  return md


def test_early_invalidation_cuts_never_green_loser():
  """No meaningful MFE + -7% → early_invalidation before full -12% adverse."""
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("92.5"),  # -7.5%
    mfe_points=Decimal("1"),  # < 3% of entry
    market_data=_md_bull(),
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "early_invalidation"


def test_early_invalidation_skipped_if_had_mfe():
  """If trade once went +5% MFE, early cut does not apply — wait for trail/adverse."""
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("92.5"),
    mfe_points=Decimal("5"),
    market_data=_md_bull(),
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.reason != "early_invalidation"


def test_adverse_still_fires_at_12pct():
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("87"),  # -13%
    mfe_points=Decimal("10"),  # had green — skip early, hit adverse
    market_data=_md_bull(),
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "adverse_momentum"


def test_trend_reversal_exits_before_trail_even_in_profit():
  md = MagicMock()
  md.spot_ltp = Decimal("24100")
  md.session_vwap_value = Decimal("24150")
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)

  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("108"),
    mfe_points=Decimal("10"),
    market_data=md,
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "trend_reversal"


def test_min_hold_blocks_immediate_reversal():
  md = MagicMock()
  md.spot_ltp = Decimal("24100")
  md.session_vwap_value = Decimal("24150")
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=5)

  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("99"),
    mfe_points=Decimal("0"),
    market_data=md,
    cfg=_cfg(),
    force_exit=False,
  )
  assert not decision.should_exit


def test_large_profit_trail_exits_on_giveback():
  md = _md_bull()
  entry_ts = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
  # MFE +25% (≥12%), giveback 35% → floor = 100 + 25*(1-0.35) = 116.25
  decision = evaluate_momentum_exit(
    option_side="CE",
    entry_price=Decimal("100"),
    entry_ts=entry_ts,
    current_ltp=Decimal("115"),
    mfe_points=Decimal("25"),
    market_data=md,
    cfg=_cfg(),
    force_exit=False,
  )
  assert decision.should_exit
  assert decision.reason == "momentum_trail"
