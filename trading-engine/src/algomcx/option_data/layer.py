from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import structlog

from algomcx.config import AppConfig
from algomcx.models.events import OptionState, QuoteUpdate

logger = structlog.get_logger(__name__)

GREEK_FIELDS = ("iv", "delta", "gamma", "theta", "vega")


class OptionDataLayer:
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._states: dict[str, OptionState] = {}
        self._vwap_num: dict[str, Decimal] = {}
        self._vwap_den: dict[str, Decimal] = {}
        self._field_flags = {
            "ltp": True,
            "bid": True,
            "ask": True,
            "oi": True,
            "volume": True,
            **{field: False for field in GREEK_FIELDS},
        }

    @property
    def field_flags(self) -> dict[str, bool]:
        return dict(self._field_flags)

    def get(self, token: str) -> OptionState | None:
        return self._states.get(token)

    def all_states(self) -> list[OptionState]:
        return list(self._states.values())

    def update_from_quote(self, quote: QuoteUpdate) -> OptionState:
        state = self._states.get(quote.instrument_token) or OptionState(
            instrument_token=quote.instrument_token,
            tsym=quote.tsym or quote.instrument_token,
            field_flags=self.field_flags,
        )
        if quote.ltp is not None:
            state.ltp = quote.ltp
            # Session option VWAP — equal-weight tick VWAP (volume deltas unreliable).
            tok = quote.instrument_token
            self._vwap_num[tok] = self._vwap_num.get(tok, Decimal("0")) + quote.ltp
            self._vwap_den[tok] = self._vwap_den.get(tok, Decimal("0")) + Decimal("1")
        if quote.bid is not None:
            state.bid = quote.bid
        if quote.ask is not None:
            state.ask = quote.ask
        if quote.volume is not None:
            state.volume = quote.volume
        if quote.oi is not None:
            state.oi = quote.oi
        if state.bid and state.ask and state.ask > 0:
            state.spread_pct = ((state.ask - state.bid) / state.ask) * Decimal("100")
        state.last_update_ts = quote.ts
        state.field_flags = self.field_flags
        self._states[quote.instrument_token] = state
        return state

    def option_vwap(self, token: str) -> float | None:
        den = self._vwap_den.get(token)
        num = self._vwap_num.get(token)
        if den is None or num is None or den <= 0:
            return None
        return float(num / den)

    def probe_greek_availability(self) -> dict[str, bool]:
        expected = self._config.data_availability.get("greek_fields_expected", False)
        if not expected:
            logger.info("greek_fields_degraded_mode", fields=GREEK_FIELDS)
        return {field: False for field in GREEK_FIELDS}
