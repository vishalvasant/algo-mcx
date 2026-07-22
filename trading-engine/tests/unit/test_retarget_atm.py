"""ContractSelector ATM retarget tests."""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from algomcx.contract_selector.selector import ContractSelector, ContractUniverse
from algomcx.models.events import Instrument


def _inst(strike: int, side: str) -> Instrument:
  return Instrument(
    exchange="NFO",
    token=f"{strike}{side}",
    tsym=f"NIFTY{strike}{side}",
    underlying="NIFTY",
    strike=Decimal(strike),
    option_type=side,
    lot_size=65,
    is_atm=False,
    in_band=True,
  )


def test_retarget_atm_picks_nearest_strike():
  cfg = SimpleNamespace(symbols={"strike_step": 50, "strike_band_points": 300})
  sel = ContractSelector(cfg, broker=SimpleNamespace())  # type: ignore[arg-type]
  instruments = [
    _inst(24100, "CE"),
    _inst(24100, "PE"),
    _inst(24150, "CE"),
    _inst(24150, "PE"),
    _inst(24200, "CE"),
    _inst(24200, "PE"),
  ]
  uni = ContractUniverse(
    spot=Decimal("24200"),
    atm_strike=Decimal("24200"),
    expiry_symbol="21JUL26",
    instruments=instruments,
    atm_ce=instruments[4],
    atm_pe=instruments[5],
  )
  moved = sel.retarget_atm(uni, Decimal("24148"))
  assert moved.atm_strike == Decimal("24150")
  assert moved.atm_ce is not None and moved.atm_ce.strike == Decimal("24150")
  assert moved.atm_pe is not None and moved.atm_pe.strike == Decimal("24150")
