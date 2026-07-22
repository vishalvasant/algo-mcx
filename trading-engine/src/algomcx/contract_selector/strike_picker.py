"""Select CE/PE among ATM ± N using Black-Scholes Greeks + liquidity."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from algomcx.config import AppConfig
from algomcx.contract_selector.selector import ContractUniverse
from algomcx.models.events import Instrument, OptionState
from algomcx.option_data.greeks import compute_greeks


@dataclass
class StrikePick:
  instrument: Instrument
  option_state: OptionState
  score: float
  delta: float | None
  gamma: float | None
  iv: float | None
  reason: str
  candidates_considered: int


def atm_band_instruments(
  universe: ContractUniverse,
  side: str,
  *,
  band_steps: int = 1,
  step: Decimal = Decimal("50"),
) -> list[Instrument]:
  """Return CE or PE contracts at ATM, ATM±1, … ATM±band_steps."""
  atm = universe.atm_strike
  if atm is None:
    return []
  wanted = {atm + step * Decimal(i) for i in range(-band_steps, band_steps + 1)}
  instruments = getattr(universe, "instruments", None) or []
  out = [
    i
    for i in instruments
    if i.option_type == side and i.strike in wanted
  ]
  out.sort(key=lambda i: abs(i.strike - atm))
  return out


def select_option_contract(
  *,
  config: AppConfig,
  universe: ContractUniverse,
  side: str,
  spot: Decimal,
  option_states: dict[str, OptionState | None],
  expiry: date | None = None,
  now: datetime | None = None,
) -> StrikePick | None:
  """Pick best ATM±N contract for ``side`` using delta/gamma/spread.

  Falls back to nearest ATM with a valid LTP when Greeks cannot be solved.
  """
  cfg = config.strategy.get("strike_selection") or {}
  if not cfg.get("enabled", True):
    inst = universe.atm_ce if side == "CE" else universe.atm_pe
    if inst is None:
      return None
    state = option_states.get(inst.token)
    if state is None or state.ltp is None:
      return None
    return StrikePick(
      instrument=inst,
      option_state=state,
      score=0.0,
      delta=None,
      gamma=None,
      iv=None,
      reason="atm_only",
      candidates_considered=1,
    )

  step = Decimal(str((getattr(config, "symbols", None) or {}).get("strike_step", 50)))
  band = int(cfg.get("atm_band_steps", 1))
  target_delta = float(cfg.get("target_delta", 0.45))
  delta_min = float(cfg.get("delta_min", 0.30))
  delta_max = float(cfg.get("delta_max", 0.60))
  prefer_gamma = bool(cfg.get("prefer_higher_gamma", True))
  max_spread = Decimal(str(cfg.get("max_spread_pct", 8)))

  candidates = atm_band_instruments(universe, side, band_steps=band, step=step)
  if not candidates:
    inst = universe.atm_ce if side == "CE" else universe.atm_pe
    candidates = [inst] if inst else []

  exp = expiry
  if exp is None:
    for c in candidates:
      if c.expiry_date is not None:
        exp = c.expiry_date.date()
        break

  scored: list[StrikePick] = []
  for inst in candidates:
    state = option_states.get(inst.token)
    if state is None or state.ltp is None or state.ltp <= 0:
      continue
    if state.spread_pct is not None and state.spread_pct > max_spread:
      continue

    delta = gamma = iv = None
    score = 0.0
    reason = "ltp_fallback"
    if exp is not None and spot > 0:
      g = compute_greeks(
        spot=float(spot),
        strike=float(inst.strike),
        premium=float(state.ltp),
        option_type=inst.option_type,
        expiry=exp,
        now=now,
      )
      delta = g.delta
      gamma = g.gamma
      iv = g.iv
      if delta is not None:
        abs_d = abs(delta)
        # Prefer |delta| near target (ATM-ish directional).
        if delta_min <= abs_d <= delta_max:
          score += 40.0 - abs(abs_d - target_delta) * 80.0
          reason = "delta_in_band"
        else:
          score += 10.0 - abs(abs_d - target_delta) * 40.0
          reason = "delta_out_of_band"
      if prefer_gamma and gamma is not None:
        score += min(gamma * 5000.0, 25.0)  # weekly gamma is small
      # Prefer closer to ATM slightly when scores tie.
      atm = universe.atm_strike or inst.strike
      steps_away = abs(float(inst.strike - atm)) / float(step)
      score += max(0.0, 8.0 - steps_away * 3.0)
    if state.spread_pct is not None:
      score += max(0.0, 10.0 - float(state.spread_pct))
    if state.volume:
      score += min(state.volume / 50_000.0, 8.0)

    scored.append(
      StrikePick(
        instrument=inst,
        option_state=state,
        score=score,
        delta=delta,
        gamma=gamma,
        iv=iv,
        reason=reason,
        candidates_considered=len(candidates),
      )
    )

  if not scored:
    return None
  scored.sort(key=lambda p: p.score, reverse=True)
  best = scored[0]
  best.candidates_considered = len(scored)
  return best


def pick_meta(pick: StrikePick) -> dict[str, Any]:
  return {
    "strike": float(pick.instrument.strike),
    "tsym": pick.instrument.tsym,
    "token": pick.instrument.token,
    "score": round(pick.score, 2),
    "delta": round(pick.delta, 4) if pick.delta is not None else None,
    "gamma": round(pick.gamma, 6) if pick.gamma is not None else None,
    "iv": round(pick.iv * 100, 2) if pick.iv is not None else None,
    "pick_reason": pick.reason,
    "candidates_considered": pick.candidates_considered,
  }
