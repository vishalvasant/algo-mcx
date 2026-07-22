from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID

import structlog

from algomcx.broker.base import BrokerAdapter
from algomcx.config import AppConfig
from algomcx.journal.writer import JournalWriter
from algomcx.market_data.engine import MarketDataEngine
from algomcx.models.events import ExecutionRequest, QuoteUpdate, TradingMode
from algomcx.position.exit_rules import evaluate_momentum_exit
from algomcx.risk.engine import RiskEngine

logger = structlog.get_logger(__name__)


@dataclass
class OpenPosition:
  position_id: UUID
  order_id: UUID
  candidate_signal_id: UUID | None
  instrument_token: str
  tsym: str
  option_side: str
  quantity: int
  entry_price: Decimal
  entry_ts: datetime
  premium_deployed: Decimal
  setup_type: str
  mfe: Decimal = Decimal("0")
  mae: Decimal = Decimal("0")
  last_quote_ts: datetime | None = None
  signal_snapshot: dict = field(default_factory=dict)


class PositionManager:
  def __init__(
    self,
    config: AppConfig,
    broker: BrokerAdapter,
    journal: JournalWriter,
    risk: RiskEngine,
    market_data: MarketDataEngine,
  ) -> None:
    self._config = config
    self._broker = broker
    self._journal = journal
    self._risk = risk
    self._market_data = market_data
    self._open: dict[UUID, OpenPosition] = {}
    self._last_exit_by_token: dict[str, datetime] = {}
    self._last_exit_any: datetime | None = None
    self._regime_primary: str | None = None
    self._pending_flips: list[dict] = []
    self._trade_close_hook = None
    self._trade_open_hook = None

  def set_trade_close_hook(self, hook) -> None:
    self._trade_close_hook = hook

  def set_trade_open_hook(self, hook) -> None:
    """Called after a new position is registered (sync; may schedule async work)."""
    self._trade_open_hook = hook

  def set_regime(self, primary: str | None) -> None:
    self._regime_primary = primary

  @property
  def open_positions(self) -> list[OpenPosition]:
    return list(self._open.values())

  @property
  def has_open_position(self) -> bool:
    return bool(self._open)

  @property
  def open_count(self) -> int:
    return len(self._open)

  def has_open_for_token(self, instrument_token: str) -> bool:
    return any(p.instrument_token == instrument_token for p in self._open.values())

  def total_deployed(self) -> Decimal:
    return sum((p.premium_deployed for p in self._open.values()), Decimal("0"))

  def unrealized_pnl(self, option_data) -> Decimal:
    total = Decimal("0")
    for pos in self._open.values():
      state = option_data.get(pos.instrument_token)
      ltp = state.ltp if state and state.ltp is not None else pos.entry_price
      total += (ltp - pos.entry_price) * pos.quantity
    return total

  def register_open(
    self,
    position_id: UUID,
    order_id: UUID,
    signal_id: UUID | None,
    signal,
    sizing,
    fill_price: Decimal,
  ) -> None:
    self._open[position_id] = OpenPosition(
      position_id=position_id,
      order_id=order_id,
      candidate_signal_id=signal_id,
      instrument_token=signal.instrument_token,
      tsym=signal.tsym,
      option_side=signal.side,
      quantity=sizing.quantity,
      entry_price=fill_price,
      entry_ts=datetime.now(tz=timezone.utc),
      premium_deployed=sizing.premium_required,
      setup_type=signal.setup_type,
      signal_snapshot=signal.feature_snapshot.model_dump(mode="json"),
    )
    if self._trade_open_hook is not None:
      try:
        self._trade_open_hook(self._open[position_id])
      except Exception:
        logger.exception("trade_open_hook_failed", tsym=signal.tsym)

  async def on_quote(self, quote: QuoteUpdate) -> None:
    if not self._open or quote.ltp is None:
      return

    exit_cfg = self._config.position_exit
    force = self._risk.is_force_exit_time()

    for pos_id in list(self._open.keys()):
      pos = self._open.get(pos_id)
      if pos is None or quote.instrument_token != pos.instrument_token:
        continue

      ltp = quote.ltp
      pos.last_quote_ts = quote.ts
      pnl_points = ltp - pos.entry_price
      pos.mfe = max(pos.mfe, pnl_points)
      pos.mae = min(pos.mae, pnl_points)

      decision = evaluate_momentum_exit(
        option_side=pos.option_side,
        entry_price=pos.entry_price,
        entry_ts=pos.entry_ts,
        current_ltp=ltp,
        mfe_points=pos.mfe,
        market_data=self._market_data,
        cfg=exit_cfg,
        force_exit=force,
        regime_primary=self._regime_primary,
      )
      if decision.should_exit:
        reason = decision.reason or "exit"
        closed_side = pos.option_side
        await self._close_position(pos_id, ltp, reason)
        if reason == "trend_reversal" and bool(
          exit_cfg.get("flip_on_trend_reversal", True)
        ):
          opposite = "PE" if closed_side == "CE" else "CE"
          self._pending_flips.append(
            {
              "side": opposite,
              "from_side": closed_side,
              "reason": "trend_reversal_flip",
              "ts": datetime.now(tz=timezone.utc),
            }
          )
          logger.info(
            "trend_reversal_flip_queued",
            from_side=closed_side,
            to_side=opposite,
          )

  async def flatten(self, reason: str = "kill_switch") -> None:
    for pos_id in list(self._open.keys()):
      pos = self._open[pos_id]
      ltp = pos.entry_price
      cache = getattr(self._broker, "_ltp_cache", None)
      if cache and pos.instrument_token in cache:
        ltp = cache[pos.instrument_token]
      await self._close_position(pos_id, ltp, reason)

  def _resolve_ltp(self, pos: OpenPosition, preferred: Decimal | None = None) -> Decimal:
    if preferred is not None:
      return preferred
    cache = getattr(self._broker, "_ltp_cache", None)
    if cache and pos.instrument_token in cache:
      return cache[pos.instrument_token]
    return pos.entry_price

  async def manual_exit(
    self,
    position_id: UUID,
    exit_price: Decimal | None = None,
  ) -> dict:
    """Square-off one open position at current LTP (or provided price)."""
    pos = self._open.get(position_id)
    if pos is None:
      raise KeyError("position_not_found")
    ltp = self._resolve_ltp(pos, exit_price)
    tsym = pos.tsym
    qty = pos.quantity
    entry = pos.entry_price
    await self._close_position(position_id, ltp, "manual_exit")
    pnl = (ltp - entry) * qty
    return {
      "ok": True,
      "position_id": str(position_id),
      "tsym": tsym,
      "quantity": qty,
      "entry_price": float(entry),
      "exit_price": float(ltp),
      "pnl": float(pnl),
      "exit_reason": "manual_exit",
    }

  async def _close_position(self, position_id: UUID, exit_price: Decimal, exit_reason: str) -> None:
    pos = self._open.pop(position_id, None)
    if pos is None:
      return

    now = datetime.now(tz=timezone.utc)
    hold_seconds = int((now - pos.entry_ts).total_seconds())

    client_id = f"AF-EXIT-{pos.position_id.hex[:8]}"
    request = ExecutionRequest(
      client_order_id=client_id,
      ts=now,
      candidate_signal_id=pos.candidate_signal_id,
      instrument_token=pos.instrument_token,
      exchange=self._config.symbols["exchange_options"],
      tsym=pos.tsym,
      side="SELL",
      quantity=pos.quantity,
      order_type="MKT",
      limit_price=exit_price,
      product=self._config.execution.get("product", "MIS"),
      reference_ltp=exit_price,
      mode=TradingMode.PAPER if self._config.is_paper else TradingMode.LIVE,
    )
    exit_order_id = await self._journal.write_order_created(request, pos.candidate_signal_id)
    update = await self._broker.place_order(request)
    await self._journal.write_order_filled(exit_order_id, update)

    fill = update.fill_price or exit_price
    pnl = (fill - pos.entry_price) * pos.quantity

    await self._journal.write_position_closed(
      position_id=pos.position_id,
      mfe=pos.mfe,
      mae=pos.mae,
    )
    await self._journal.write_closed_trade(
      position=pos,
      exit_ts=now,
      exit_price=fill,
      pnl=pnl,
      exit_reason=exit_reason,
      hold_seconds=hold_seconds,
    )
    await self._risk.release_capital(pos.premium_deployed, pnl)

    await self._journal.write_notification(
      "trade",
      "info" if pnl >= 0 else "warning",
      "Trade exit",
      (
        f"Exited {pos.tsym} @ ₹{fill} · "
        f"{'profit' if pnl >= 0 else 'loss'} ₹{abs(pnl):.2f}"
        + (f" ({exit_reason})" if exit_reason else "")
      ),
      related_entity="position",
      related_id=str(pos.position_id),
    )
    logger.info("paper_exit", tsym=pos.tsym, pnl=str(pnl), reason=exit_reason)
    self._last_exit_by_token[pos.instrument_token] = now
    self._last_exit_any = now
    if self._trade_close_hook is not None:
      try:
        self._trade_close_hook(pos.setup_type, pnl, exit_reason)
      except Exception:
        logger.exception("trade_close_hook_failed")

  def in_cooldown(self, instrument_token: str | None = None) -> bool:
    minutes = int(self._config.risk.get("cooldown_after_exit_minutes", 3))
    if minutes <= 0:
      return False
    now = datetime.now(tz=timezone.utc)
    # Global cooldown after any exit — blocks strike-hopping thrash.
    if self._last_exit_any is not None:
      if (now - self._last_exit_any).total_seconds() < minutes * 60:
        return True
    if instrument_token:
      last = self._last_exit_by_token.get(instrument_token)
      if last is None:
        return False
      return (now - last).total_seconds() < minutes * 60
    return False

  def clear_cooldowns(self) -> None:
    """Drop exit cooldowns (used after paper account reset)."""
    self._last_exit_by_token.clear()
    self._last_exit_any = None

  def stale_holding_tokens(self, max_age_sec: float = 3.0) -> list[str]:
    """Holdings with no fresh quote — WS may have dropped the token."""
    now = datetime.now(tz=timezone.utc)
    stale: list[str] = []
    for pos in self._open.values():
      last = pos.last_quote_ts
      if last is None:
        age = (now - pos.entry_ts).total_seconds()
      else:
        if last.tzinfo is None:
          last = last.replace(tzinfo=timezone.utc)
        age = (now - last).total_seconds()
      if age > max_age_sec:
        stale.append(pos.instrument_token)
    return stale

  async def rehydrate_open_positions(self) -> int:
    """Reload OPEN rows from DB after engine restart so trails keep monitoring."""
    import re

    from algomcx.db.connection import get_pool

    pool = get_pool()
    async with pool.acquire() as conn:
      rows = await conn.fetch(
        """
        SELECT
          p.id,
          p.order_id,
          p.instrument_token,
          p.tsym,
          p.side,
          p.quantity,
          p.entry_price,
          p.entry_ts,
          p.mfe,
          p.mae,
          o.candidate_signal_id,
          cs.setup_type
        FROM positions p
        LEFT JOIN orders o ON o.id = p.order_id
        LEFT JOIN candidate_signals cs ON cs.id = o.candidate_signal_id
        WHERE p.status = 'OPEN'
        ORDER BY p.entry_ts ASC
        """
      )

    self._open.clear()
    for row in rows:
      qty = int(row["quantity"])
      entry = Decimal(str(row["entry_price"]))
      tsym = str(row["tsym"])
      m = re.search(r"([CP])(\d+)\s*$", tsym.upper())
      option_side = "CE" if m and m.group(1) == "C" else "PE" if m else "CE"
      prem = entry * Decimal(qty)
      pos_id = row["id"]
      self._open[pos_id] = OpenPosition(
        position_id=pos_id,
        order_id=row["order_id"],
        candidate_signal_id=row["candidate_signal_id"],
        instrument_token=row["instrument_token"],
        tsym=tsym,
        option_side=option_side,
        quantity=qty,
        entry_price=entry,
        entry_ts=row["entry_ts"],
        premium_deployed=prem,
        setup_type=row["setup_type"] or "rehydrated",
        mfe=Decimal(str(row["mfe"] or 0)),
        mae=Decimal(str(row["mae"] or 0)),
        signal_snapshot={},
      )

    logger.info(
      "positions_rehydrated",
      count=len(self._open),
      tokens=[p.instrument_token for p in self._open.values()],
    )
    return len(self._open)

  def pop_pending_flips(self) -> list[dict]:
    flips = list(self._pending_flips)
    self._pending_flips.clear()
    return flips
