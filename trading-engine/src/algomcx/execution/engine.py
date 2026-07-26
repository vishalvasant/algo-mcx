from __future__ import annotations

import json
from decimal import Decimal
from uuid import UUID

import structlog

from algomcx.broker.base import BrokerAdapter
from algomcx.config import AppConfig
from algomcx.journal.writer import JournalWriter
from algomcx.models.events import (
  CandidateSignal,
  ExecutionRequest,
  OrderUpdate,
  TradingMode,
)
from algomcx.risk.engine import EntrySizing, RiskEngine
from algomcx.notifications.trade_alerts import build_trade_entry_message
from algomcx.runtime.trading_mode import get_execution_mode

logger = structlog.get_logger(__name__)


class ExecutionEngine:
  def __init__(
    self,
    config: AppConfig,
    broker: BrokerAdapter,
    journal: JournalWriter,
    risk: RiskEngine,
  ) -> None:
    self._config = config
    self._broker = broker
    self._journal = journal
    self._risk = risk
    self._exec_cfg = config.execution

  async def enter(
    self,
    signal: CandidateSignal,
    sizing: EntrySizing,
  ) -> tuple[UUID, UUID, OrderUpdate]:
    exchange = signal.scanner_metadata.get("exchange", self._config.symbols["exchange_options"])
    client_id = f"AF-{signal.id.hex[:12]}"
    request = ExecutionRequest(
      client_order_id=client_id,
      ts=signal.ts,
      candidate_signal_id=signal.id,
      instrument_token=signal.instrument_token,
      exchange=exchange,
      tsym=signal.tsym,
      side="BUY",
      quantity=sizing.quantity,
      order_type=self._exec_cfg.get("order_type", "MKT"),
      limit_price=sizing.entry_ltp,
      product=self._exec_cfg.get("product", "MIS"),
      reference_ltp=sizing.entry_ltp,
      mode=TradingMode.LIVE if get_execution_mode() == "live" else TradingMode.PAPER,
    )

    order_id = await self._journal.write_order_created(request, signal.id)
    update = await self._broker.place_order(request)
    if update.status == "REJECTED":
      raise RuntimeError(update.rejection_reason or "broker rejected order")
    await self._journal.write_order_filled(order_id, update)

    position_id = await self._journal.write_position_opened(
      order_id=order_id,
      signal=signal,
      fill_price=update.fill_price or sizing.entry_ltp,
      quantity=sizing.quantity,
      stop_loss=None,
      target=None,
      mode=request.mode.value,
    )

    fill = update.fill_price or sizing.entry_ltp
    entry_message = await build_trade_entry_message(
      self._risk,
      tsym=signal.tsym,
      fill=fill or Decimal("0"),
      quantity=sizing.quantity,
      setup_type=signal.setup_type,
    )
    await self._journal.write_notification(
      "trade",
      "info",
      "BUY filled",
      entry_message,
      related_entity="position",
      related_id=str(position_id),
    )
    logger.info(
      "entry_filled",
      mode=get_execution_mode(),
      tsym=signal.tsym,
      qty=sizing.quantity,
      fill=str(update.fill_price),
      position_id=str(position_id),
    )
    return position_id, order_id, update
