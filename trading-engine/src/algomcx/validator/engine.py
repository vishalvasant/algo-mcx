from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog

from algomcx.config import AppConfig
from algomcx.models.events import CandidateSignal, OptionState, ValidationResult
from algomcx.validator.expiry_cautious import expiry_cautious_rejection_reasons
from algomcx.validator.time_of_day_cautious import time_of_day_cautious_rejection_reasons
from algomcx.validator.trap_avoidance import trap_rejection_reasons

logger = structlog.get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class RuleValidator:
  def __init__(self, config: AppConfig) -> None:
    self._config = config
    self._validator = config.validator
    self._version = self._validator.get("validator_version", "validator_v1.0.0")

  def validate(
    self,
    signal: CandidateSignal,
    option: OptionState | None,
    *,
    has_open_for_token: bool,
    in_cooldown: bool,
    kill_switch: bool,
    is_expiry_day: bool = False,
    now: datetime | None = None,
  ) -> ValidationResult:
    reasons: list[str] = []
    ist_now = now.astimezone(IST) if now is not None else datetime.now(IST)
    clock = ist_now.time()

    start = time.fromisoformat(self._validator["entry_start_time"])
    end = time.fromisoformat(self._validator["entry_end_time"])
    if not (start <= clock <= end):
      reasons.append("outside_entry_window")
    else:
      tod = self._validator.get("time_of_day_cautious") or {}
      if tod.get("enabled", False):
        reasons.extend(
          time_of_day_cautious_rejection_reasons(signal, tod, now=ist_now)
        )

    if kill_switch:
      reasons.append("kill_switch_active")

    if is_expiry_day and bool(self._validator.get("expiry_day_block_entries", False)):
      reasons.append("expiry_day_block")
    elif is_expiry_day:
      cautious = self._validator.get("expiry_day_cautious") or {}
      if cautious.get("enabled", False):
        reasons.extend(expiry_cautious_rejection_reasons(signal, cautious))
        if bool(cautious.get("apply_trap_avoidance", False)):
          trap_cfg = dict(self._validator.get("trap_avoidance") or {})
          buffer = cautious.get("spot_vwap_buffer_points")
          if buffer is not None:
            trap_cfg["spot_vwap_buffer_points"] = buffer
          reasons.extend(trap_rejection_reasons(signal, trap_cfg))
      else:
        reasons.extend(
          trap_rejection_reasons(signal, self._validator.get("trap_avoidance") or {})
        )
    else:
      reasons.extend(
        trap_rejection_reasons(signal, self._validator.get("trap_avoidance") or {})
      )

    if has_open_for_token:
      reasons.append("position_already_open_for_symbol")

    if in_cooldown:
      reasons.append("cooldown_active")

    if option is None:
      reasons.append("option_state_missing")
    else:
      relaxed = self._validator.get("paper_relaxed_liquidity", False)
      # Paper/pre-market often lacks bid/ask depth — LTP is enough for our ATM entries.
      soft_fields = {"oi", "volume", "bid", "ask"} if relaxed else {"oi", "volume"}
      for field in self._validator.get("required_fields", []):
        val = getattr(option, field, None)
        if val is None:
          if field in soft_fields:
            continue
          reasons.append(f"missing_{field}")

      if option.spread_pct is not None:
        max_spread = Decimal(str(self._validator.get("max_spread_pct", 2.0)))
        if option.spread_pct > max_spread:
          reasons.append("spread_too_wide")

      if not relaxed:
        min_vol = int(self._validator.get("min_option_volume", 0))
        min_oi = int(self._validator.get("min_option_oi", 0))
        if option.volume is not None and option.volume < min_vol:
          reasons.append("low_volume")
        if option.oi is not None and option.oi < min_oi:
          reasons.append("low_oi")

    passed = len(reasons) == 0
    result = ValidationResult(
      candidate_signal_id=signal.id,
      ts=now if now is not None else datetime.now(tz=timezone.utc),
      passed=passed,
      rejection_reasons=reasons,
      validator_version=self._version,
    )
    if not passed:
      logger.info("candidate_rejected", reasons=reasons, tsym=signal.tsym)
    return result
