"""Smoke tests for institutional library, chain intel, learner."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from algomcx.features.chain_intel import build_chain_snapshot
from algomcx.features.indicators import cpr_levels, ema
from algomcx.journal.analytics import StrategyLearner
from algomcx.models.events import Instrument, OptionState
from algomcx.scanner.library import STRATEGY_NAMES, build_strategy_scanners


def test_ema_and_cpr():
  vals = [Decimal(str(x)) for x in range(100, 130)]
  assert ema(vals, 9) is not None
  cpr = cpr_levels(Decimal("24100"), Decimal("23900"), Decimal("24050"))
  assert cpr["pivot"] > cpr["bc"]
  assert cpr["tc"] >= cpr["bc"]


def test_build_scanners_all_names():
  cfg = SimpleNamespace(
    strategy={
      "router": {"enabled_strategies": list(STRATEGY_NAMES)},
      "strategy_version": "t",
      "strike_selection": {"enabled": False},
    },
    symbols={"strike_step": 50},
  )
  scanners = build_strategy_scanners(cfg)  # type: ignore[arg-type]
  assert len(scanners) == len(STRATEGY_NAMES)


def test_chain_pcr_and_max_pain():
  instruments = []
  states = {}
  for k in (24000, 24050, 24100):
    for side in ("CE", "PE"):
      tok = f"{side}{k}"
      instruments.append(
        Instrument(
          exchange="NFO",
          token=tok,
          tsym=tok,
          underlying="NIFTY",
          strike=Decimal(k),
          option_type=side,
          lot_size=65,
        )
      )
      states[tok] = OptionState(
        instrument_token=tok,
        tsym=tok,
        ltp=Decimal("100"),
        oi=10_000 if side == "PE" else 8_000,
        volume=50_000,
      )
  uni = SimpleNamespace(instruments=instruments, atm_strike=Decimal("24050"))
  snap = build_chain_snapshot(uni, states)  # type: ignore[arg-type]
  assert snap["pcr_oi"] is not None and snap["pcr_oi"] > 1
  assert snap["max_pain"] is not None


def test_learner_demotes_loss_streak():
  learner = StrategyLearner(path=None, demote_after_losses=3)
  for _ in range(3):
    learner.record_trade("vwap_reclaim", -100)
  assert learner.priority_multiplier("vwap_reclaim") < 1.0
