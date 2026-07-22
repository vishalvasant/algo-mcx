from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog

from algomcx.config import AppConfig
from algomcx.db.connection import get_pool
from algomcx.db.paper_account import ensure_paper_account
from algomcx.models.events import CandidateSignal, OptionState

logger = structlog.get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


@dataclass
class EntrySizing:
  approved: bool
  quantity: int
  lot_size: int
  entry_ltp: Decimal
  premium_required: Decimal
  rejection_reason: str | None = None
  lots: int = 0
  confidence: int = 0


def lots_for_confidence(risk_cfg: dict, confidence: int) -> int:
  """Map signal confidence (0–100) → number of lots from config tiers."""
  default = max(1, int(risk_cfg.get("default_lots", 1)))
  sizing = risk_cfg.get("confidence_lot_sizing") or {}
  if not sizing.get("enabled", False):
    return default

  tiers = list(sizing.get("tiers") or [])
  # Highest min_confidence that confidence meets
  tiers_sorted = sorted(
    tiers,
    key=lambda t: int(t.get("min_confidence", 0)),
    reverse=True,
  )
  chosen = default
  for tier in tiers_sorted:
    if confidence >= int(tier.get("min_confidence", 0)):
      chosen = max(1, int(tier.get("lots", default)))
      break

  max_lots = int(sizing.get("max_lots", chosen))
  return max(1, min(chosen, max_lots))


def fit_lots_to_capital(
  risk_cfg: dict,
  *,
  confidence: int,
  entry_ltp: Decimal,
  lot_size: int,
  available: Decimal,
  deployed: Decimal,
  equity: Decimal,
) -> tuple[int, Decimal]:
  """Confidence lots, then fill remaining deploy room toward ~85% when conf is high.

  Same sizing path used by live RiskEngine and day backtest.
  """
  target = lots_for_confidence(risk_cfg, confidence)
  max_lots = int((risk_cfg.get("confidence_lot_sizing") or {}).get("max_lots", target))
  max_pct = Decimal(str(risk_cfg.get("max_premium_pct_of_available", 65)))
  max_for_trade = available * max_pct / Decimal("100")
  deploy_cap = equity * Decimal(str(risk_cfg.get("max_deployed_pct_of_equity", 85))) / Decimal("100")
  room = deploy_cap - deployed

  def _ok(n: int) -> Decimal | None:
    if n < 1 or lot_size < 1 or entry_ltp <= 0:
      return None
    prem = entry_ltp * lot_size * n
    if prem <= available and prem <= max_for_trade and prem <= room:
      return prem
    return None

  lots = target
  while lots >= 1 and _ok(lots) is None:
    lots -= 1
  if lots < 1:
    return 0, Decimal("0")

  # High-confidence: use remaining deploy room up to max_lots (backtest parity).
  if confidence >= 80:
    while lots < max_lots and _ok(lots + 1) is not None:
      lots += 1

  prem = _ok(lots)
  assert prem is not None
  return lots, prem


@dataclass
class DailyRiskSnapshot:
  trade_date: date
  starting_capital: Decimal
  available_capital: Decimal
  deployed_capital: Decimal
  realized_pnl: Decimal
  trade_count: int
  consecutive_losses: int
  kill_switch: bool
  entries_blocked: bool
  block_reason: str | None = None

  @property
  def equity(self) -> Decimal:
    return self.starting_capital + self.realized_pnl

  @property
  def auto_trade_enabled(self) -> bool:
    return not self.kill_switch and not self.entries_blocked


class RiskEngine:
  def __init__(self, config: AppConfig) -> None:
    self._config = config
    self._risk = config.risk

  def _today_ist(self) -> date:
    return datetime.now(IST).date()

  async def ensure_daily_state(self) -> DailyRiskSnapshot:
    capital = Decimal(str(self._risk.get("account_capital_inr", 50000)))
    await ensure_paper_account(capital)
    today = self._today_ist()
    pool = get_pool()
    async with pool.acquire() as conn:
      row = await conn.fetchrow(
        """
        SELECT trade_date, starting_capital, available_capital, deployed_capital,
               realized_pnl, trade_count, consecutive_losses, kill_switch,
               entries_blocked, block_reason
        FROM daily_risk_state WHERE trade_date = $1
        """,
        today,
      )
    assert row is not None
    return DailyRiskSnapshot(
      trade_date=row["trade_date"],
      starting_capital=Decimal(str(row["starting_capital"] or capital)),
      available_capital=Decimal(str(row["available_capital"] or capital)),
      deployed_capital=Decimal(str(row["deployed_capital"] or 0)),
      realized_pnl=Decimal(str(row["realized_pnl"] or 0)),
      trade_count=int(row["trade_count"] or 0),
      consecutive_losses=int(row["consecutive_losses"] or 0),
      kill_switch=bool(row["kill_switch"]),
      entries_blocked=bool(row["entries_blocked"]),
      block_reason=row["block_reason"],
    )

  async def set_auto_trade(self, enabled: bool) -> DailyRiskSnapshot:
    """Enable/disable new entries without kill-switch flatten."""
    today = self._today_ist()
    pool = get_pool()
    await self.ensure_daily_state()
    async with pool.acquire() as conn:
      row = await conn.fetchrow(
        "SELECT kill_switch FROM daily_risk_state WHERE trade_date = $1",
        today,
      )
      kill = bool(row["kill_switch"]) if row else False
      if enabled:
        if kill:
          await conn.execute(
            """
            UPDATE daily_risk_state SET
              block_reason = COALESCE(block_reason, 'kill_switch'),
              updated_at = now()
            WHERE trade_date = $1
            """,
            today,
          )
        else:
          await conn.execute(
            """
            UPDATE daily_risk_state SET
              entries_blocked = FALSE,
              block_reason = NULL,
              updated_at = now()
            WHERE trade_date = $1
            """,
            today,
          )
          await conn.execute(
            """
            INSERT INTO notifications (type, severity, title, message)
            VALUES ('system', 'info', 'Auto trading ON',
                    'Engine may place entries when setups fire')
            """
          )
      else:
        await conn.execute(
          """
          UPDATE daily_risk_state SET
            entries_blocked = TRUE,
            block_reason = 'auto_trade_off',
            updated_at = now()
          WHERE trade_date = $1
          """,
          today,
        )
        await conn.execute(
          """
          INSERT INTO notifications (type, severity, title, message)
          VALUES ('system', 'warning', 'Auto trading OFF',
                  'New entries paused — scans and decision logs continue')
          """
        )
    return await self.ensure_daily_state()


  async def size_entry(
    self,
    signal: CandidateSignal,
    option: OptionState,
    snapshot: DailyRiskSnapshot,
    *,
    open_position_count: int = 0,
  ) -> EntrySizing:
    lot_size = int(signal.scanner_metadata.get("lot_size", 1))
    confidence = int(
      signal.confidence
      if signal.confidence is not None
      else signal.scanner_metadata.get("confidence", 0)
    )
    entry_ltp = option.ltp or Decimal("0")

    if entry_ltp <= 0:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "invalid_ltp",
        lots=0, confidence=confidence,
      )

    max_daily_loss = Decimal(str(self._risk.get("max_daily_loss", 0)))
    if max_daily_loss > 0 and snapshot.realized_pnl <= -max_daily_loss:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "max_daily_loss",
        lots=0, confidence=confidence,
      )

    max_trades = int(self._risk.get("max_trades_per_day", 0))
    if max_trades > 0 and snapshot.trade_count >= max_trades:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "max_trades_per_day",
        lots=0, confidence=confidence,
      )

    max_concurrent = int(self._risk.get("max_concurrent_positions", 0))
    if max_concurrent > 0 and open_position_count >= max_concurrent:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "max_concurrent_positions",
        lots=0, confidence=confidence,
      )

    max_consec = int(self._risk.get("max_consecutive_losses", 0))
    if max_consec > 0 and snapshot.consecutive_losses >= max_consec:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "max_consecutive_losses",
        lots=0, confidence=confidence,
      )

    if snapshot.kill_switch or snapshot.entries_blocked:
      return EntrySizing(
        False, 0, lot_size, entry_ltp, Decimal("0"), "entries_blocked",
        lots=0, confidence=confidence,
      )

    max_premium_pct = Decimal(str(self._risk.get("max_premium_pct_of_available", 65)))
    max_for_trade = snapshot.available_capital * max_premium_pct / Decimal("100")
    max_deployed_pct = Decimal(str(self._risk.get("max_deployed_pct_of_equity", 85)))
    deploy_cap = snapshot.equity * max_deployed_pct / Decimal("100")
    deploy_room = deploy_cap - snapshot.deployed_capital

    lots, premium = fit_lots_to_capital(
      self._risk,
      confidence=confidence,
      entry_ltp=entry_ltp,
      lot_size=lot_size,
      available=snapshot.available_capital,
      deployed=snapshot.deployed_capital,
      equity=snapshot.equity,
    )
    target_lots = lots_for_confidence(self._risk, confidence)
    if lots < 1:
      quantity = lot_size * max(target_lots, 1)
      premium_req = entry_ltp * quantity
      reason = "insufficient_capital"
      if premium_req > max_for_trade:
        reason = "premium_pct_exceeded"
      elif premium_req > deploy_room:
        reason = "deployed_cap_exceeded"
      return EntrySizing(
        False, quantity, lot_size, entry_ltp, premium_req, reason,
        lots=target_lots, confidence=confidence,
      )

    quantity = lot_size * lots
    logger.info(
      "entry_sized",
      confidence=confidence,
      target_lots=target_lots,
      lots=lots,
      quantity=quantity,
      premium=str(premium),
      deploy_room=str(deploy_room),
    )
    return EntrySizing(
      True, quantity, lot_size, entry_ltp, premium, None,
      lots=lots, confidence=confidence,
    )

  async def reserve_capital(self, premium: Decimal) -> None:
    today = self._today_ist()
    pool = get_pool()
    async with pool.acquire() as conn:
      await conn.execute(
        """
        UPDATE daily_risk_state SET
            deployed_capital = deployed_capital + $2,
            available_capital = available_capital - $2,
            updated_at = now()
        WHERE trade_date = $1
        """,
        today,
        premium,
      )

  async def release_capital(self, premium: Decimal, pnl: Decimal) -> None:
    today = self._today_ist()
    pool = get_pool()
    async with pool.acquire() as conn:
      await conn.execute(
        """
        UPDATE daily_risk_state SET
            deployed_capital = GREATEST(deployed_capital - $2, 0),
            available_capital = available_capital + $2 + $3,
            realized_pnl = realized_pnl + $3,
            trade_count = trade_count + 1,
            consecutive_losses = CASE WHEN $3 < 0 THEN consecutive_losses + 1 ELSE 0 END,
            updated_at = now()
        WHERE trade_date = $1
        """,
        today,
        premium,
        pnl,
      )

  def is_force_exit_time(self) -> bool:
    force = self._risk.get("force_exit_time", "15:20")
    now_ist = datetime.now(IST).time()
    return now_ist >= time.fromisoformat(force)
