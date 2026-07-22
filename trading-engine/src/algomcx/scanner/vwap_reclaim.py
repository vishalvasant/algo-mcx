from __future__ import annotations

from datetime import datetime, timezone

import structlog

from algomcx.config import AppConfig
from algomcx.contract_selector.resolve import resolve_side_contract
from algomcx.contract_selector.selector import ContractUniverse
from algomcx.models.events import Bias, CandidateSignal, FeatureSnapshot, OptionState

logger = structlog.get_logger(__name__)


class VwapReclaimScanner:
  name = "vwap_reclaim"

  def __init__(self, config: AppConfig) -> None:
    self._config = config
    self._version = config.strategy.get("strategy_version", "strategy_router_v1.0.0")

  def scan(
    self,
    features: FeatureSnapshot,
    universe: ContractUniverse,
    option_states: dict[str, OptionState | None] | OptionState | None,
  ) -> CandidateSignal | None:
    if features.bias_5m == Bias.NEUTRAL:
      return None
    if not features.setup_3m or not features.trigger_1m:
      return None
    if features.session_vwap is None or features.nifty_spot is None:
      return None

    side = ""
    if (
      features.bias_5m == Bias.BULLISH
      and features.setup_3m == "vwap_reclaim_bull"
      and features.trigger_1m == "vwap_reclaim_cross_up"
    ):
      side = "CE"
    elif (
      features.bias_5m == Bias.BEARISH
      and features.setup_3m == "vwap_reclaim_bear"
      and features.trigger_1m == "vwap_reclaim_cross_down"
    ):
      side = "PE"
    else:
      return None

    states = _as_states(option_states)
    resolved = resolve_side_contract(
      config=self._config,
      universe=universe,
      side=side,
      spot=features.nifty_spot,
      option_states=states,
    )
    if resolved is None:
      return None
    inst, option_state, pick = resolved

    signal = CandidateSignal(
      ts=datetime.now(tz=timezone.utc),
      setup_type=self.name,
      side=side,
      instrument_token=inst.token,
      tsym=inst.tsym,
      strategy_version=self._version,
      feature_snapshot=features,
      scanner_metadata={
        "atm_strike": str(universe.atm_strike),
        "option_ltp": str(option_state.ltp),
        "exchange": inst.exchange,
        "lot_size": inst.lot_size,
        "setup_3m": features.setup_3m,
        "trigger_1m": features.trigger_1m,
        "strike_pick": pick,
      },
    )
    logger.info(
      "candidate_signal",
      setup=signal.setup_type,
      side=side,
      tsym=inst.tsym,
      strike=str(inst.strike),
      ltp=str(option_state.ltp),
      delta=pick.get("delta"),
    )
    return signal


def _as_states(
  option_states: dict[str, OptionState | None] | OptionState | None,
) -> dict[str, OptionState | None]:
  if option_states is None:
    return {}
  if isinstance(option_states, dict):
    return option_states
  return {option_states.instrument_token: option_states}
