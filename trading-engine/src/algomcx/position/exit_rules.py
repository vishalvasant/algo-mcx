from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from algomcx.market_data.engine import MarketDataEngine


@dataclass
class ExitDecision:
  should_exit: bool
  reason: str | None = None


def _adverse_stop_pct(
  exit_cfg: dict,
  *,
  confidence: int | None,
  high_vol: bool,
) -> Decimal:
  """Max loss % from entry — up to 4% at high confidence, tighter when lower."""
  key = "high_vol_adverse_move_pct_from_entry" if high_vol else "adverse_move_pct_from_entry"
  max_pct = Decimal(str(exit_cfg.get(key, exit_cfg.get("adverse_move_pct_from_entry", 4)))) / Decimal(
    "100"
  )
  if not exit_cfg.get("confidence_adverse_scaling", True) or confidence is None:
    return max_pct

  min_pct = Decimal(str(exit_cfg.get("adverse_move_min_pct", 3))) / Decimal("100")
  base_conf = int(exit_cfg.get("adverse_base_confidence", 76))
  top_conf = int(exit_cfg.get("adverse_max_confidence", 100))
  conf = max(base_conf, min(int(confidence), top_conf))
  if top_conf <= base_conf:
    return max_pct
  ratio = Decimal(conf - base_conf) / Decimal(top_conf - base_conf)
  return min_pct + (max_pct - min_pct) * ratio


def _momentum_trail_floor(
  entry_price: Decimal,
  mfe_points: Decimal,
  exit_cfg: dict,
) -> Decimal | None:
  """
  Breakeven once green; above +4% MFE lock (peak% - 2%) from entry.
  Example entry 100, peak +10% → floor 108.
  """
  if mfe_points <= 0 or entry_price <= 0:
    return None

  mfe_pct = mfe_points / entry_price
  step_arm = Decimal(str(exit_cfg.get("trail_step_arm_mfe_pct", 4))) / Decimal("100")
  buffer_pct = Decimal(str(exit_cfg.get("trail_step_buffer_pct", 2))) / Decimal("100")

  if mfe_pct <= step_arm:
    return entry_price

  locked_pct = mfe_pct - buffer_pct
  if locked_pct <= 0:
    return entry_price
  return entry_price * (Decimal("1") + locked_pct)


def _trend_reversal_should_exit(
  *,
  option_side: str,
  entry_price: Decimal,
  current_ltp: Decimal,
  mfe_points: Decimal,
  held: float,
  market_data: MarketDataEngine,
  exit_cfg: dict,
) -> bool:
  """VWAP bias flip — only after min hold and when not a small winning scalp."""
  if not exit_cfg.get("bias_flip_exit", True):
    return False

  reversal_min_hold = int(exit_cfg.get("trend_reversal_min_hold_seconds", 180))
  if held < reversal_min_hold:
    return False

  in_profit = current_ltp > entry_price
  if bool(exit_cfg.get("trend_reversal_skip_when_profitable", True)) and in_profit:
    return False

  min_mfe_pct = Decimal(str(exit_cfg.get("trend_reversal_min_mfe_pct", 3))) / Decimal("100")
  if bool(exit_cfg.get("trend_reversal_defer_if_had_mfe", True)):
    if mfe_points >= entry_price * min_mfe_pct:
      return False

  only_underwater = bool(exit_cfg.get("trend_reversal_only_if_underwater", True))
  if only_underwater and in_profit:
    return False

  spot = market_data.spot_ltp
  vwap = market_data.session_vwap_value
  if spot is None or vwap is None:
    return False

  buffer = Decimal(str(exit_cfg.get("bias_flip_buffer_points", 0)))
  if option_side == "CE" and spot < (vwap - buffer):
    return True
  if option_side == "PE" and spot > (vwap + buffer):
    return True
  return False


def evaluate_momentum_exit(
  *,
  option_side: str,
  entry_price: Decimal,
  entry_ts: datetime,
  current_ltp: Decimal,
  mfe_points: Decimal,
  market_data: MarketDataEngine,
  cfg: dict,
  force_exit: bool,
  regime_primary: str | None = None,
  quantity: int = 1,
  is_expiry_day: bool = False,
  confidence: int | None = None,
  setup_type: str | None = None,
  now: datetime | None = None,
) -> ExitDecision:
  if force_exit:
    return ExitDecision(True, "force_exit")

  now = now or datetime.now(tz=timezone.utc)
  if entry_ts.tzinfo is None and now.tzinfo is not None:
    entry_ts = entry_ts.replace(tzinfo=timezone.utc)
  held = (now - entry_ts).total_seconds()

  exit_cfg = dict(cfg)
  if is_expiry_day:
    expiry_overrides = cfg.get("expiry_day_exit") or {}
    exit_cfg.update({k: v for k, v in expiry_overrides.items() if v is not None})

  min_hold = int(exit_cfg.get("min_hold_seconds", 20))
  if held < min_hold:
    return ExitDecision(False)

  if entry_price <= 0 or current_ltp <= 0:
    return ExitDecision(False)

  in_profit = current_ltp > entry_price
  high_vol = regime_primary == "high_volatility"
  adverse_pct = _adverse_stop_pct(exit_cfg, confidence=confidence, high_vol=high_vol)

  max_hold = int(exit_cfg.get("max_hold_minutes", 0))
  skip_time_in_profit = bool(exit_cfg.get("skip_time_stop_when_profitable", True))
  if max_hold > 0 and held > max_hold * 60:
    if not (skip_time_in_profit and in_profit):
      return ExitDecision(True, "time_stop")

  qty = max(quantity, 1)
  max_loss_inr = Decimal(str(exit_cfg.get("max_loss_inr_per_trade", 0)))
  if max_loss_inr > 0:
    loss_inr = (entry_price - current_ltp) * qty
    if loss_inr >= max_loss_inr:
      return ExitDecision(True, "max_loss_inr")

  # Early invalidation: never went meaningfully green → cut at tight SL.
  exempt = set(exit_cfg.get("early_invalidation_exempt_setups") or [])
  if exit_cfg.get("early_invalidation_enabled", True) and setup_type not in exempt:
    early_min_hold = int(exit_cfg.get("early_invalidation_min_hold_seconds", 45))
    mfe_arm_pct = Decimal(str(exit_cfg.get("early_invalidation_max_mfe_pct", 2))) / Decimal("100")
    early_loss_pct = Decimal(str(exit_cfg.get("early_invalidation_loss_pct", 4))) / Decimal("100")
    if held >= early_min_hold:
      never_armed = mfe_points < entry_price * mfe_arm_pct
      underwater = current_ltp <= entry_price * (Decimal("1") - early_loss_pct)
      if never_armed and underwater:
        return ExitDecision(True, "early_invalidation")

  if current_ltp <= entry_price * (Decimal("1") - adverse_pct):
    return ExitDecision(True, "adverse_momentum")

  trail_floor = _momentum_trail_floor(entry_price, mfe_points, exit_cfg)
  if trail_floor is not None and current_ltp <= trail_floor:
    return ExitDecision(True, "momentum_trail")

  # VWAP flip — last resort after hard stops; avoid 30s noise exits.
  if _trend_reversal_should_exit(
    option_side=option_side,
    entry_price=entry_price,
    current_ltp=current_ltp,
    mfe_points=mfe_points,
    held=held,
    market_data=market_data,
    exit_cfg=exit_cfg,
  ):
    return ExitDecision(True, "trend_reversal")

  return ExitDecision(False)
