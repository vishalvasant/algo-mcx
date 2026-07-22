from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog

from algomcx.config import AppConfig
from algomcx.contract_selector.selector import ContractUniverse
from algomcx.models.events import (
  Bias,
  CandidateSignal,
  FeatureSnapshot,
  MarketRegime,
  OptionState,
  StrategyDecision,
)
from algomcx.quality.gate import QualityGate
from algomcx.scanner.base import StrategyScanner

logger = structlog.get_logger(__name__)


def _score_context_from_option(
  signal: CandidateSignal,
  option: OptionState | dict[str, OptionState | None] | None,
) -> dict:
  """Build quality-gate context from option state / strike_pick metadata."""
  ctx: dict = {}
  pick = (signal.scanner_metadata or {}).get("strike_pick") or {}
  for k in ("delta", "gamma", "iv"):
    if pick.get(k) is not None:
      ctx[k] = pick[k]
  state: OptionState | None = None
  if isinstance(option, dict):
    state = option.get(signal.instrument_token)
  elif isinstance(option, OptionState):
    state = option
  if state is not None:
    if state.ltp is not None:
      ctx["ltp"] = float(state.ltp)
    if state.spread_pct is not None:
      ctx["spread_pct"] = float(state.spread_pct)
    if state.volume is not None:
      ctx["volume"] = state.volume
    if state.oi is not None:
      ctx["oi"] = state.oi
  meta = signal.scanner_metadata or {}
  if meta.get("option_ltp") is not None and "ltp" not in ctx:
    ctx["ltp"] = float(meta["option_ltp"])
  return ctx


def _diagnose_no_signal(
  name: str,
  features: FeatureSnapshot,
  option: OptionState | dict[str, OptionState | None] | None,
) -> str:
  """Explain why a scanner produced nothing — for Decision Logs."""
  if features.nifty_spot is None or features.session_vwap is None:
    return "missing_spot_or_vwap"
  if features.bias_5m == Bias.NEUTRAL:
    return "neutral_bias"
  has_ltp = False
  if isinstance(option, dict):
    has_ltp = any(s is not None and s.ltp is not None for s in option.values())
  elif option is not None:
    has_ltp = option.ltp is not None
  if not has_ltp:
    if option is None or (isinstance(option, dict) and not option):
      return "option_state_missing"
    return "option_ltp_missing"

  extra = features.extra or {}
  if name == "vwap_reclaim":
    if not features.setup_3m:
      return "no_reclaim_setup"
    if not features.trigger_1m:
      return "no_reclaim_trigger"
    if features.bias_5m == Bias.BULLISH and (
      features.setup_3m != "vwap_reclaim_bull"
      or features.trigger_1m != "vwap_reclaim_cross_up"
    ):
      return "reclaim_side_mismatch"
    if features.bias_5m == Bias.BEARISH and (
      features.setup_3m != "vwap_reclaim_bear"
      or features.trigger_1m != "vwap_reclaim_cross_down"
    ):
      return "reclaim_side_mismatch"
    return "reclaim_gate_failed"

  if name == "vwap_pullback":
    if not extra.get("setup_vwap_pullback"):
      return "no_pullback_setup"
    if not extra.get("trigger_vwap_pullback"):
      return "no_pullback_trigger"
    return "pullback_gate_failed"

  if name == "vwap_trend":
    if not extra.get("setup_vwap_trend"):
      return "no_trend_setup"
    return "trend_gate_failed"

  return "no_setup_or_trigger"


class StrategyRouter:
  """Pick exactly one coded strategy or NO_TRADE. Deterministic."""

  def __init__(
    self,
    config: AppConfig,
    scanners: list[StrategyScanner],
    quality: QualityGate,
  ) -> None:
    self._config = config
    router_cfg = config.strategy.get("router", {})
    enabled = set(router_cfg.get("enabled_strategies") or [s.name for s in scanners])
    self._scanners = [s for s in scanners if s.name in enabled]
    self._quality = quality
    self._min_confidence = quality.min_confidence
    self.last_decision: StrategyDecision | None = None

  def _score_context(
    self,
    signal: CandidateSignal,
    option: OptionState | dict[str, OptionState | None] | None,
  ) -> dict:
    return _score_context_from_option(signal, option)

  def route(
    self,
    features: FeatureSnapshot,
    regime: MarketRegime,
    universe: ContractUniverse,
    options_by_strategy: dict[str, OptionState | dict[str, OptionState | None] | None],
  ) -> tuple[StrategyDecision, CandidateSignal | None]:
    logs: list[str] = []
    warnings: list[str] = []
    scores: list[dict[str, Any]] = []
    now = datetime.now(tz=timezone.utc)
    feature_debug = {
      "bias": features.bias_5m.value,
      "spot": float(features.nifty_spot) if features.nifty_spot is not None else None,
      "vwap": float(features.session_vwap) if features.session_vwap is not None else None,
      "distance": (features.extra or {}).get("distance_to_vwap_points"),
      "setup_3m": features.setup_3m,
      "trigger_1m": features.trigger_1m,
      "setup_pullback": (features.extra or {}).get("setup_vwap_pullback"),
      "trigger_pullback": (features.extra or {}).get("trigger_vwap_pullback"),
      "setup_trend": (features.extra or {}).get("setup_vwap_trend"),
      "skip_reasons": (features.extra or {}).get("skip_reasons"),
      "candle_counts": (features.extra or {}).get("candle_counts"),
    }

    if not regime.trade_allowed:
      decision = StrategyDecision(
        ts=now,
        selected_strategy="NO_TRADE",
        confidence=0,
        trade_allowed=False,
        position_side="NONE",
        selected_reason="regime_blocks_trade: " + ", ".join(regime.reasons[:4]),
        regime=regime,
        strategy_scores=[],
        warnings=warnings,
        logs=["regime.trade_allowed=false", str(feature_debug)],
      )
      # Attach feature debug via warnings bucket for journal metadata consumers
      decision.warnings.append(f"features={feature_debug}")
      self.last_decision = decision
      return decision, None

    best_signal: CandidateSignal | None = None
    best_conf = -1
    best_name = "NO_TRADE"
    best_logs: list[str] = []

    for scanner in self._scanners:
      option = options_by_strategy.get(scanner.name)
      signal = scanner.scan(features, universe, option)
      if signal is None:
        reason = _diagnose_no_signal(scanner.name, features, option)
        scores.append(
          {
            "strategy": scanner.name,
            "compatible": False,
            "confidence": 0,
            "reason": reason,
          }
        )
        logs.append(f"{scanner.name}: {reason}")
        continue

      conf, conf_logs = self._quality.score(
        signal,
        features,
        regime,
        context=self._score_context(signal, option),
      )
      signal.confidence = conf
      signal.scanner_metadata = {
        **signal.scanner_metadata,
        "confidence": conf,
        "confidence_logs": conf_logs,
        "regime_primary": regime.primary,
      }
      entry = {
        "strategy": scanner.name,
        "compatible": True,
        "confidence": conf,
        "side": signal.side,
        "tsym": signal.tsym,
        "passes_gate": conf >= self._min_confidence,
        "logs": conf_logs,
      }
      scores.append(entry)
      logs.append(f"{scanner.name}: confidence={conf}")

      if conf > best_conf:
        best_conf = conf
        best_signal = signal
        best_name = scanner.name
        best_logs = conf_logs

    if best_signal is None or best_conf < self._min_confidence:
      if best_signal is not None:
        reason = f"best_confidence={best_conf} < min={self._min_confidence}"
      else:
        reasons = [f"{s['strategy']}={s.get('reason')}" for s in scores]
        reason = "no_strategy_produced_signal: " + "; ".join(reasons)
      decision = StrategyDecision(
        ts=now,
        selected_strategy="NO_TRADE",
        confidence=max(0, best_conf),
        trade_allowed=False,
        position_side="NONE",
        selected_reason=reason,
        regime=regime,
        strategy_scores=scores,
        warnings=[f"features={feature_debug}"],
        logs=logs + best_logs,
      )
      self.last_decision = decision
      logger.info(
        "strategy_routed_no_trade",
        reason=reason,
        features=feature_debug,
        regime=regime.primary,
      )
      return decision, None

    rivals = [s for s in scores if s.get("compatible") and s["strategy"] != best_name]
    if rivals:
      second = max((s.get("confidence") or 0) for s in rivals)
      if second == best_conf:
        warnings.append("confidence_tie_prefer_first_registered")

    decision = StrategyDecision(
      ts=now,
      selected_strategy=best_name,
      confidence=best_conf,
      trade_allowed=True,
      position_side=best_signal.side,
      selected_reason=(
        f"selected {best_name} confidence={best_conf} "
        f"regime={regime.primary} risk={regime.risk_score}"
      ),
      regime=regime,
      strategy_scores=scores,
      warnings=warnings,
      logs=logs + best_logs,
    )
    self.last_decision = decision
    logger.info(
      "strategy_routed",
      strategy=best_name,
      confidence=best_conf,
      side=best_signal.side,
      tsym=best_signal.tsym,
      regime=regime.primary,
    )
    return decision, best_signal
