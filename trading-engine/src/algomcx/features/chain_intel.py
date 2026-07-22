"""Option-chain intelligence: PCR, Max Pain, OI build-up classification."""
from __future__ import annotations

from decimal import Decimal
from typing import Any

from algomcx.contract_selector.selector import ContractUniverse
from algomcx.models.events import OptionState


def build_chain_snapshot(
  universe: ContractUniverse,
  states: dict[str, OptionState | None],
  *,
  prior_oi: dict[str, int] | None = None,
) -> dict[str, Any]:
  """Compute PCR, max-pain proxy, and ATM-band OI build flags."""
  prior_oi = prior_oi or {}
  ce_oi = pe_oi = 0
  ce_vol = pe_vol = 0
  strikes: dict[Decimal, dict[str, Any]] = {}

  for inst in universe.instruments:
    st = states.get(inst.token)
    oi = int(st.oi or 0) if st else 0
    vol = int(st.volume or 0) if st else 0
    if inst.option_type == "CE":
      ce_oi += oi
      ce_vol += vol
    elif inst.option_type == "PE":
      pe_oi += oi
      pe_vol += vol
    bucket = strikes.setdefault(
      inst.strike, {"ce_oi": 0, "pe_oi": 0, "ce_token": None, "pe_token": None}
    )
    if inst.option_type == "CE":
      bucket["ce_oi"] = oi
      bucket["ce_token"] = inst.token
    else:
      bucket["pe_oi"] = oi
      bucket["pe_token"] = inst.token

  pcr_oi = (pe_oi / ce_oi) if ce_oi > 0 else None
  pcr_vol = (pe_vol / ce_vol) if ce_vol > 0 else None

  # Max pain ≈ strike minimizing total intrinsic * OI (call+put).
  max_pain = _max_pain(strikes)
  atm = universe.atm_strike
  atm_band = []
  if atm is not None:
    for k, v in strikes.items():
      if abs(k - atm) <= Decimal("100"):
        atm_band.append((k, v))

  long_build = short_build = unwind = False
  oi_delta_ce = oi_delta_pe = 0
  for inst in universe.instruments:
    if universe.atm_strike is None:
      break
    if abs(inst.strike - universe.atm_strike) > Decimal("50"):
      continue
    st = states.get(inst.token)
    oi = int(st.oi or 0) if st else 0
    prev = int(prior_oi.get(inst.token, oi))
    d = oi - prev
    if inst.option_type == "CE":
      oi_delta_ce += d
    else:
      oi_delta_pe += d

  # Heuristic: PE OI↑ + spot down → long put build; CE OI↑ + spot up → long call
  if oi_delta_pe > 0 and oi_delta_ce <= 0:
    long_build = True  # put buildup
  if oi_delta_ce > 0 and oi_delta_pe <= 0:
    long_build = True  # call buildup
  if oi_delta_ce < 0 and oi_delta_pe < 0:
    unwind = True
  if oi_delta_ce > 0 and oi_delta_pe > 0:
    short_build = True  # both sides writing / straddle build

  return {
    "pcr_oi": round(pcr_oi, 3) if pcr_oi is not None else None,
    "pcr_vol": round(pcr_vol, 3) if pcr_vol is not None else None,
    "max_pain": float(max_pain) if max_pain is not None else None,
    "ce_oi_total": ce_oi,
    "pe_oi_total": pe_oi,
    "oi_delta_ce_atm": oi_delta_ce,
    "oi_delta_pe_atm": oi_delta_pe,
    "long_build_up": long_build,
    "short_build_up": short_build,
    "oi_unwinding": unwind,
    "oi_confirms_ce": bool(pcr_oi is not None and pcr_oi < 0.9),
    "oi_confirms_pe": bool(pcr_oi is not None and pcr_oi > 1.1),
  }


def _max_pain(strikes: dict[Decimal, dict[str, Any]]) -> Decimal | None:
  if not strikes:
    return None
  keys = sorted(strikes.keys())
  best_k = keys[0]
  best_pain = None
  for trial in keys:
    pain = Decimal("0")
    for k, v in strikes.items():
      ce_oi = Decimal(v.get("ce_oi") or 0)
      pe_oi = Decimal(v.get("pe_oi") or 0)
      if trial > k:
        pain += (trial - k) * ce_oi
      if trial < k:
        pain += (k - trial) * pe_oi
    if best_pain is None or pain < best_pain:
      best_pain = pain
      best_k = trial
  return best_k
