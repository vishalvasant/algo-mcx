"""Shared helper: resolve CE/PE contract via Greek ATM±N picker."""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from algomcx.config import AppConfig
from algomcx.contract_selector.selector import ContractUniverse
from algomcx.contract_selector.strike_picker import pick_meta, select_option_contract
from algomcx.models.events import Instrument, OptionState


def resolve_side_contract(
  *,
  config: AppConfig,
  universe: ContractUniverse,
  side: str,
  spot: Decimal | None,
  option_states: dict[str, OptionState | None],
  expiry: date | None = None,
  now: datetime | None = None,
) -> tuple[Instrument, OptionState, dict] | None:
  if spot is None or spot <= 0:
    # Fall back to ATM only when spot missing.
    inst = universe.atm_ce if side == "CE" else universe.atm_pe
    if inst is None:
      return None
    state = option_states.get(inst.token)
    if state is None or state.ltp is None:
      return None
    return inst, state, {"pick_reason": "atm_no_spot", "strike": float(inst.strike)}

  pick = select_option_contract(
    config=config,
    universe=universe,
    side=side,
    spot=spot,
    option_states=option_states,
    expiry=expiry,
    now=now,
  )
  if pick is None:
    return None
  return pick.instrument, pick.option_state, pick_meta(pick)
