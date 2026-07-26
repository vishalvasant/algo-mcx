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
from algomcx.risk.greeks_sizing import greeks_confirmation_multiplier
from algomcx.runtime.trading_mode import is_live_execution
from algomcx.notifications.telegram import maybe_send_telegram_alert
from algomcx.symbols_util import (
  capital_for,
  list_underlyings,
  lot_size_for_tsym,
  risk_underlying_key,
  total_account_capital,
  uses_pooled_capital,
)

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


def _sizing_cfg(risk_cfg: dict) -> dict:
  return risk_cfg.get("confidence_lot_sizing") or {}


def strategy_lot_cap(risk_cfg: dict, setup_type: str | None) -> int | None:
  """Per-setup max lots from risk_config.strategy_lot_caps."""
  if not setup_type:
    return None
  caps = risk_cfg.get("strategy_lot_caps") or {}
  raw = caps.get(setup_type)
  if raw is None:
    return None
  return max(1, int(raw))


def confidence_capital_pct(risk_cfg: dict, confidence: int) -> Decimal:
  """Map confidence → % of available capital to deploy (dynamic mode)."""
  sizing = _sizing_cfg(risk_cfg)
  base = int(sizing.get("base_confidence", 78))
  top = int(sizing.get("max_confidence", 100))
  min_pct = Decimal(str(sizing.get("min_capital_pct", 25)))
  max_pct = Decimal(str(sizing.get("max_capital_pct", 90)))
  conf = max(base, min(int(confidence), top))
  aggressive_from = int(sizing.get("aggressive_deploy_min_confidence", 90))
  if aggressive_from > 0 and conf >= aggressive_from:
    return max_pct
  if top <= base:
    return max_pct
  span = Decimal(conf - base) / Decimal(top - base)
  return min_pct + span * (max_pct - min_pct)


def _tier_lots_for_confidence(risk_cfg: dict, confidence: int) -> int:
  """Legacy fixed tier → lots mapping."""
  default = max(1, int(risk_cfg.get("default_lots", 1)))
  sizing = _sizing_cfg(risk_cfg)
  tiers = list(sizing.get("tiers") or [])
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
  if max_lots > 0:
    return max(1, min(chosen, max_lots))
  return max(1, chosen)


def lots_for_confidence(
  risk_cfg: dict,
  confidence: int,
  *,
  entry_ltp: Decimal | None = None,
  lot_size: int | None = None,
  available: Decimal | None = None,
  deployed: Decimal | None = None,
  equity: Decimal | None = None,
) -> int:
  """Lots for confidence — dynamic capital % or legacy tiers."""
  sizing = _sizing_cfg(risk_cfg)
  if not sizing.get("enabled", False):
    return max(1, int(risk_cfg.get("default_lots", 1)))

  mode = str(sizing.get("mode", "dynamic")).lower()
  if mode == "dynamic" and entry_ltp is not None and lot_size is not None:
    if available is None or deployed is None or equity is None:
      available = available if available is not None else Decimal("25000")
      equity = equity if equity is not None else available
      deployed = deployed if deployed is not None else Decimal("0")
    lots, _ = fit_lots_to_capital(
      risk_cfg,
      confidence=confidence,
      entry_ltp=entry_ltp,
      lot_size=lot_size,
      available=available,
      deployed=deployed,
      equity=equity,
    )
    return max(0, lots)

  return _tier_lots_for_confidence(risk_cfg, confidence)


def fit_lots_to_capital(
  risk_cfg: dict,
  *,
  confidence: int,
  entry_ltp: Decimal,
  lot_size: int,
  available: Decimal,
  deployed: Decimal,
  equity: Decimal,
  greeks_mult: Decimal = Decimal("1"),
) -> tuple[int, Decimal]:
  """Size entry: confidence % of available margin × greeks confirmation → max lots."""
  sizing = _sizing_cfg(risk_cfg)
  max_pct = Decimal(str(risk_cfg.get("max_premium_pct_of_available", 65)))
  deploy_cap = equity * Decimal(str(risk_cfg.get("max_deployed_pct_of_equity", 85))) / Decimal("100")
  room = deploy_cap - deployed
  hard_cap = min(available * max_pct / Decimal("100"), room, available)

  def _ok(n: int) -> Decimal | None:
    if n < 1 or lot_size < 1 or entry_ltp <= 0:
      return None
    prem = entry_ltp * lot_size * n
    if prem <= available and prem <= hard_cap:
      return prem
    return None

  cost_per_lot = entry_ltp * lot_size
  if cost_per_lot <= 0:
    return 0, Decimal("0")

  g_mult = max(Decimal("0"), min(Decimal("1"), greeks_mult))

  if sizing.get("enabled", False) and str(sizing.get("mode", "dynamic")).lower() == "dynamic":
    pct = confidence_capital_pct(risk_cfg, confidence)
    # High confidence → up to 90% of available margin, scaled by greeks confirmation.
    budget = min(available * pct / Decimal("100"), hard_cap) * g_mult
    lots = int(budget // cost_per_lot)
    cap = int(sizing.get("max_lots", 0))
    if cap > 0:
      lots = min(lots, cap)
    if lots < 1 and _ok(1) is not None:
      lots = 1
    if lots < 1:
      return 0, Decimal("0")
    prem = _ok(lots)
    if prem is None:
      while lots >= 1:
        prem = _ok(lots)
        if prem is not None:
          break
        lots -= 1
    if lots < 1 or prem is None:
      return 0, Decimal("0")
    return lots, prem

  # Legacy tier mode
  target = _tier_lots_for_confidence(risk_cfg, confidence)
  max_lots = int(sizing.get("max_lots", target)) or target
  lots = target
  while lots >= 1 and _ok(lots) is None:
    lots -= 1
  if lots < 1:
    return 0, Decimal("0")

  scale_min = int(sizing.get("scale_to_max_lots_min_confidence", 92))
  if confidence >= scale_min and max_lots > 0:
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


def overlay_live_limits(snapshot: DailyRiskSnapshot, limits) -> DailyRiskSnapshot:
  """Replace capital fields with broker margin while keeping risk counters."""
  return DailyRiskSnapshot(
    trade_date=snapshot.trade_date,
    starting_capital=limits.cash,
    available_capital=limits.available,
    deployed_capital=limits.margin_used,
    realized_pnl=snapshot.realized_pnl,
    trade_count=snapshot.trade_count,
    consecutive_losses=snapshot.consecutive_losses,
    kill_switch=snapshot.kill_switch,
    entries_blocked=snapshot.entries_blocked,
    block_reason=snapshot.block_reason,
  )


class RiskEngine:
  def __init__(self, config: AppConfig) -> None:
    self._config = config
    self._risk = config.risk

  def _today_ist(self) -> date:
    return datetime.now(IST).date()

  async def ensure_daily_state(self, underlying: str | None = None) -> DailyRiskSnapshot:
    ul = risk_underlying_key(self._config, underlying)
    capital = (
      total_account_capital(self._config)
      if uses_pooled_capital(self._config)
      else capital_for(self._config, ul)
    )
    await ensure_paper_account(capital, underlying=ul)
    today = self._today_ist()
    pool = get_pool()
    async with pool.acquire() as conn:
      row = await conn.fetchrow(
        """
        SELECT trade_date, starting_capital, available_capital, deployed_capital,
               realized_pnl, trade_count, consecutive_losses, kill_switch,
               entries_blocked, block_reason
        FROM daily_risk_state WHERE trade_date = $1 AND underlying = $2
        """,
        today,
        ul,
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
      if uses_pooled_capital(self._config):
        underlyings = [risk_underlying_key(self._config)]
      else:
        underlyings = [
          str(u.get("symbol", "")).upper()
          for u in list_underlyings(self._config)
          if u.get("symbol")
        ] or [self._config.symbols.get("underlying", "GOLD").upper()]
      for ul in underlyings:
        row = await conn.fetchrow(
          "SELECT kill_switch FROM daily_risk_state WHERE trade_date = $1 AND underlying = $2",
          today,
          ul,
        )
        kill = bool(row["kill_switch"]) if row else False
        if enabled:
          if kill:
            await conn.execute(
              """
              UPDATE daily_risk_state SET
                block_reason = COALESCE(block_reason, 'kill_switch'),
                updated_at = now()
              WHERE trade_date = $1 AND underlying = $2
              """,
              today,
              ul,
            )
          else:
            await conn.execute(
              """
              UPDATE daily_risk_state SET
                entries_blocked = FALSE,
                block_reason = NULL,
                updated_at = now()
              WHERE trade_date = $1 AND underlying = $2
              """,
              today,
              ul,
            )
        else:
          await conn.execute(
            """
            UPDATE daily_risk_state SET
              entries_blocked = TRUE,
              block_reason = 'auto_trade_off',
              updated_at = now()
            WHERE trade_date = $1 AND underlying = $2
            """,
            today,
            ul,
          )
      if enabled:
        await conn.execute(
          """
          INSERT INTO notifications (type, severity, title, message)
          VALUES ('system', 'info', 'Auto trading ON',
                  'Engine may place entries when setups fire')
          """
        )
        await maybe_send_telegram_alert(
          type_="system",
          severity="info",
          title="Auto trading ON",
          message="Engine may place entries when setups fire",
        )
      else:
        await conn.execute(
          """
          INSERT INTO notifications (type, severity, title, message)
          VALUES ('system', 'warning', 'Auto trading OFF',
                  'New entries paused — scans and decision logs continue')
          """
        )
        await maybe_send_telegram_alert(
          type_="system",
          severity="warning",
          title="Auto trading OFF",
          message="New entries paused — scans and decision logs continue",
        )
    return await self.ensure_daily_state()


  async def size_entry(
    self,
    signal: CandidateSignal,
    option: OptionState,
    snapshot: DailyRiskSnapshot,
    *,
    open_position_count: int = 0,
    is_expiry_day: bool = False,
  ) -> EntrySizing:
    raw_lot = signal.scanner_metadata.get("lot_size")
    meta_lot = int(raw_lot) if raw_lot is not None else None
    lot_size = lot_size_for_tsym(signal.tsym, metadata_lot_size=meta_lot)
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

    max_concurrent = int(self._risk.get("max_concurrent_positions_per_index", 0))
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

    greeks_mult = greeks_confirmation_multiplier(signal, option, self._risk)

    lots, premium = fit_lots_to_capital(
      self._risk,
      confidence=confidence,
      entry_ltp=entry_ltp,
      lot_size=lot_size,
      available=snapshot.available_capital,
      deployed=snapshot.deployed_capital,
      equity=snapshot.equity,
      greeks_mult=greeks_mult,
    )

    target_lots = lots_for_confidence(
      self._risk,
      confidence,
      entry_ltp=entry_ltp,
      lot_size=lot_size,
      available=snapshot.available_capital,
      deployed=snapshot.deployed_capital,
      equity=snapshot.equity,
    )
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
      capital_pct=str(confidence_capital_pct(self._risk, confidence)),
      greeks_mult=str(greeks_mult),
      target_lots=target_lots,
      lots=lots,
      quantity=quantity,
      premium=str(premium),
      deploy_room=str(deploy_room),
      setup_type=signal.setup_type,
      available=str(snapshot.available_capital),
    )
    return EntrySizing(
      True, quantity, lot_size, entry_ltp, premium, None,
      lots=lots, confidence=confidence,
    )

  async def reserve_capital(self, premium: Decimal, underlying: str | None = None) -> None:
    if is_live_execution():
      return
    today = self._today_ist()
    ul = risk_underlying_key(self._config, underlying)
    pool = get_pool()
    async with pool.acquire() as conn:
      await conn.execute(
        """
        UPDATE daily_risk_state SET
            deployed_capital = deployed_capital + $3,
            available_capital = available_capital - $3,
            updated_at = now()
        WHERE trade_date = $1 AND underlying = $2
        """,
        today,
        ul,
        premium,
      )

  async def release_capital(self, premium: Decimal, pnl: Decimal, underlying: str | None = None) -> None:
    """Return premium to cash and compound trade P&L into deployable capital."""
    today = self._today_ist()
    ul = risk_underlying_key(self._config, underlying)
    pool = get_pool()
    async with pool.acquire() as conn:
      if is_live_execution():
        await conn.execute(
          """
          UPDATE daily_risk_state SET
              realized_pnl = realized_pnl + $3,
              trade_count = trade_count + 1,
              consecutive_losses = CASE WHEN $3 < 0 THEN consecutive_losses + 1 ELSE 0 END,
              updated_at = now()
          WHERE trade_date = $1 AND underlying = $2
          """,
          today,
          ul,
          pnl,
        )
        return
      await conn.execute(
        """
        UPDATE daily_risk_state SET
            deployed_capital = GREATEST(deployed_capital - $3, 0),
            available_capital = available_capital + $3 + $4,
            realized_pnl = realized_pnl + $4,
            trade_count = trade_count + 1,
            consecutive_losses = CASE WHEN $4 < 0 THEN consecutive_losses + 1 ELSE 0 END,
            updated_at = now()
        WHERE trade_date = $1 AND underlying = $2
        """,
        today,
        ul,
        premium,
        pnl,
      )
      row = await conn.fetchrow(
        """
        SELECT starting_capital, available_capital, deployed_capital, realized_pnl
        FROM daily_risk_state WHERE trade_date = $1 AND underlying = $2
        """,
        today,
        ul,
      )
    if row:
      equity = Decimal(str(row["starting_capital"] or 0)) + Decimal(str(row["realized_pnl"] or 0))
      logger.info(
        "capital_compounded",
        underlying=ul,
        pnl=str(pnl),
        equity=str(equity),
        available=str(row["available_capital"]),
        deployed=str(row["deployed_capital"]),
      )

  def is_force_exit_time(self) -> bool:
    force = self._risk.get("force_exit_time", "15:20")
    now_ist = datetime.now(IST).time()
    return now_ist >= time.fromisoformat(force)
