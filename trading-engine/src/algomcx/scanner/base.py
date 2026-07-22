from __future__ import annotations

from typing import Protocol

from algomcx.contract_selector.selector import ContractUniverse
from algomcx.models.events import CandidateSignal, FeatureSnapshot, OptionState


class StrategyScanner(Protocol):
  name: str

  def scan(
    self,
    features: FeatureSnapshot,
    universe: ContractUniverse,
    option_states: dict[str, OptionState | None] | OptionState | None,
  ) -> CandidateSignal | None:
    ...
