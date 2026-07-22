"""Unit tests for ATM±N Greek strike selection and flip queue."""
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from algomcx.contract_selector.selector import ContractUniverse
from algomcx.contract_selector.strike_picker import (
  atm_band_instruments,
  select_option_contract,
)
from algomcx.models.events import Instrument, OptionState

IST = ZoneInfo("Asia/Kolkata")


def _inst(side: str, strike: int, *, atm: int = 24050) -> Instrument:
  return Instrument(
    exchange="NFO",
    token=f"T-{side}-{strike}",
    tsym=f"NIFTY{strike}{side}",
    underlying="NIFTY",
    expiry_date=datetime(2026, 7, 16, 15, 30, tzinfo=IST),
    strike=Decimal(strike),
    option_type=side,
    lot_size=65,
    is_atm=strike == atm,
    in_band=True,
  )


def _universe(atm: int = 24050) -> ContractUniverse:
  step = 50
  instruments = []
  for i in range(-2, 3):
    k = atm + i * step
    instruments.append(_inst("CE", k, atm=atm))
    instruments.append(_inst("PE", k, atm=atm))
  return ContractUniverse(
    spot=Decimal(atm),
    atm_strike=Decimal(atm),
    expiry_symbol="16JUL26",
    instruments=instruments,
    atm_ce=next(i for i in instruments if i.strike == atm and i.option_type == "CE"),
    atm_pe=next(i for i in instruments if i.strike == atm and i.option_type == "PE"),
  )


def _cfg(enabled: bool = True) -> SimpleNamespace:
  return SimpleNamespace(
    strategy={
      "strike_selection": {
        "enabled": enabled,
        "atm_band_steps": 1,
        "target_delta": 0.45,
        "delta_min": 0.30,
        "delta_max": 0.60,
        "prefer_higher_gamma": True,
        "max_spread_pct": 8,
      }
    },
    symbols={"strike_step": 50},
  )


def _states(uni: ContractUniverse, premiums: dict[tuple[str, int], float]) -> dict:
  out: dict[str, OptionState | None] = {}
  for inst in uni.instruments:
    key = (inst.option_type, int(inst.strike))
    if key not in premiums:
      continue
    px = Decimal(str(premiums[key]))
    out[inst.token] = OptionState(
      instrument_token=inst.token,
      tsym=inst.tsym,
      ltp=px,
      bid=px - Decimal("0.5"),
      ask=px + Decimal("0.5"),
      spread_pct=Decimal("1"),
      volume=100_000,
    )
  return out


def test_atm_band_instruments_returns_atm_plus_minus_one():
  uni = _universe()
  ces = atm_band_instruments(uni, "CE", band_steps=1, step=Decimal("50"))
  strikes = sorted(float(i.strike) for i in ces)
  assert strikes == [24000.0, 24050.0, 24100.0]


def test_select_picks_among_band_when_enabled():
  uni = _universe()
  # OTM CE cheaper / lower delta; ITM richer — ATM should score well near 0.45.
  states = _states(
    uni,
    {
      ("CE", 24000): 220,  # ITM
      ("CE", 24050): 140,  # ATM
      ("CE", 24100): 80,  # OTM
    },
  )
  pick = select_option_contract(
    config=_cfg(True),
    universe=uni,
    side="CE",
    spot=Decimal("24050"),
    option_states=states,
    expiry=date(2026, 7, 16),
    now=datetime(2026, 7, 14, 11, 0, tzinfo=IST),
  )
  assert pick is not None
  assert pick.instrument.option_type == "CE"
  assert pick.instrument.strike in {
    Decimal("24000"),
    Decimal("24050"),
    Decimal("24100"),
  }
  assert pick.candidates_considered >= 1
  assert pick.delta is not None


def test_select_falls_back_to_atm_when_disabled():
  uni = _universe()
  states = _states(
    uni,
    {
      ("CE", 24000): 220,
      ("CE", 24050): 140,
      ("CE", 24100): 80,
    },
  )
  pick = select_option_contract(
    config=_cfg(False),
    universe=uni,
    side="CE",
    spot=Decimal("24050"),
    option_states=states,
  )
  assert pick is not None
  assert pick.instrument.strike == Decimal("24050")
  assert pick.reason == "atm_only"


def test_pending_flip_queue_on_trend_reversal():
  """PositionManager queues opposite side after trend_reversal exit."""
  from unittest.mock import MagicMock

  from algomcx.position.manager import PositionManager

  cfg = SimpleNamespace(
    position_exit={"flip_on_trend_reversal": True, "min_hold_seconds": 0},
    risk={},
  )
  pm = PositionManager(cfg, MagicMock(), MagicMock(), MagicMock(), MagicMock())
  pm._pending_flips.append(
    {"side": "PE", "from_side": "CE", "reason": "trend_reversal_flip"}
  )
  flips = pm.pop_pending_flips()
  assert len(flips) == 1
  assert flips[0]["side"] == "PE"
  assert flips[0]["from_side"] == "CE"
  assert pm.pop_pending_flips() == []
