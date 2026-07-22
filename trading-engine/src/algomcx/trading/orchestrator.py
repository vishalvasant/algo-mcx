from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog

from pathlib import Path
from typing import Any

from algomcx.config import AppConfig
from algomcx.contract_selector.expiry import parse_expiry_tag
from algomcx.contract_selector.resolve import resolve_side_contract
from algomcx.contract_selector.selector import ContractSelector, ContractUniverse
from algomcx.contract_selector.strike_picker import atm_band_instruments
from algomcx.execution.engine import ExecutionEngine
from algomcx.features.chain_intel import build_chain_snapshot
from algomcx.features.engine import FeatureEngine
from algomcx.journal.analytics import StrategyLearner
from algomcx.journal.writer import JournalWriter
from algomcx.market_data.engine import MarketDataEngine
from algomcx.models.events import (
  Bias,
  CandidateSignal,
  CandleInterval,
  OptionState,
  QuoteUpdate,
  SystemEvent,
)
from algomcx.option_data.greeks import compute_greeks
from algomcx.option_data.layer import OptionDataLayer
from algomcx.position.manager import PositionManager
from algomcx.quality.gate import QualityGate
from algomcx.regime.classifier import RegimeClassifier
from algomcx.risk.engine import RiskEngine
from algomcx.scanner.library import build_strategy_scanners
from algomcx.strategy.router import StrategyRouter
from algomcx.validator.engine import RuleValidator

from algomcx.symbols_util import list_underlyings

logger = structlog.get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")


class TradingOrchestrator:
  def __init__(
    self,
    config: AppConfig,
    broker,
    journal: JournalWriter,
    market_data: MarketDataEngine,
    option_data: OptionDataLayer,
  ) -> None:
    self._config = config
    self._broker = broker
    self._journal = journal
    self._market_data = market_data
    self._option_data = option_data
    self._universe: ContractUniverse | None = None
    self._prior_oi: dict[str, int] = {}

    self.features = FeatureEngine(config, market_data)
    self.regime = RegimeClassifier(config)
    self.quality = QualityGate(config)
    root = Path(__file__).resolve().parents[4]
    self.learner = StrategyLearner(root / "reports" / "strategy_learner.json")
    self.quality.set_learner(self.learner)
    scanners = build_strategy_scanners(config)
    # Keep attribute for debug endpoints that still expect .scanner
    self.scanner = next((s for s in scanners if s.name == "vwap_reclaim"), scanners[0])
    self.router = StrategyRouter(config, scanners, self.quality)
    self.validator = RuleValidator(config)
    self.risk = RiskEngine(config)
    self.execution = ExecutionEngine(config, broker, journal)
    self.positions = PositionManager(
      config, broker, journal, self.risk, market_data
    )
    self.positions.set_trade_close_hook(self._on_trade_closed)
    self._contract_selector = ContractSelector(config, broker)

    self._scan_lock = asyncio.Lock()
    self._entering_tokens: set[str] = set()
    self._last_scan_at: datetime | None = None
    self._scan_interval_sec = int(config.runtime.get("scan_interval_seconds", 10))
    self._log_every = bool(config.strategy.get("router", {}).get("log_every_decision", True))
    self._running = False
    self._underlyings = [
      str(u.get("symbol", "")).upper()
      for u in list_underlyings(config)
      if u.get("symbol")
    ]
    self._scan_underlying_idx = 0
    self._switch_underlying: Any | None = None

  def set_underlying_switcher(self, callback) -> None:
    self._switch_underlying = callback

  def _on_trade_closed(self, setup_type: str, pnl: Decimal, exit_reason: str) -> None:
    self.learner.record_trade(setup_type, pnl, exit_reason=exit_reason)

  def set_universe(self, universe: ContractUniverse) -> None:
    self._universe = universe

  async def initialize(self) -> None:
    snap = await self.risk.ensure_daily_state()
    await self._load_prior_day_levels()
    logger.info(
      "paper_account_initialized",
      capital=str(snap.available_capital),
      starting=str(snap.starting_capital),
      strategies=len(self.router._scanners),
    )

  async def _load_prior_day_levels(self) -> None:
    """PDH/PDL/CPR inputs from previous session."""
    try:
      from datetime import date, time, timedelta

      today = datetime.now(IST).date()
      # Walk back up to 5 calendar days for prior session
      for back in range(1, 6):
        d = today - timedelta(days=back)
        if d.weekday() >= 5:
          continue
        start = datetime.combine(d, time(9, 15), tzinfo=IST).astimezone(timezone.utc)
        end = datetime.combine(d, time(15, 30), tzinfo=IST).astimezone(timezone.utc)
        rows = await self._broker.get_candles(
          self._config.symbols["exchange_spot"],
          self._config.symbols["spot_token"],
          CandleInterval.M5,
          start,
          end,
        )
        if rows:
          hi = max(c.high for c in rows)
          lo = min(c.low for c in rows)
          cl = rows[-1].close
          self.features.set_prior_day(hi, lo, cl)
          logger.info("prior_day_levels", date=str(d), pdh=str(hi), pdl=str(lo), close=str(cl))
          return
    except Exception:
      logger.exception("prior_day_levels_failed")

  async def run_periodic_scan(self) -> None:
    self._running = True
    while self._running:
      try:
        if self._market_open():
          # Must share _scan_lock with on_quote — otherwise two concurrent
          # scans can both pass has_open_for_token and double-enter the same
          # contract (seen live: identical entry+exit timestamps).
          async with self._scan_lock:
            await self._scan_for_entry()
      except asyncio.CancelledError:
        raise
      except Exception:
        logger.exception("periodic_scan_failed")
      await asyncio.sleep(self._scan_interval_sec)

  def stop(self) -> None:
    self._running = False

  async def on_quote(self, quote: QuoteUpdate) -> None:
    await self.positions.on_quote(quote)

    if self._universe is None or not self._market_open():
      return

    # Flips + entry scans share one lock so they cannot race a periodic scan
    # into a duplicate same-token fill.
    async with self._scan_lock:
      if self.positions._pending_flips:
        flips = self.positions.pop_pending_flips()
        for flip in flips:
          try:
            await self._try_reversal_flip(flip)
          except Exception:
            logger.exception("reversal_flip_failed", flip=flip)

      now = datetime.now(tz=timezone.utc)
      if (
        self._last_scan_at
        and (now - self._last_scan_at).total_seconds() < self._scan_interval_sec
      ):
        return
      self._last_scan_at = now
      await self._scan_for_entry()

  def _band_option_states(self, universe: ContractUniverse) -> dict[str, OptionState | None]:
    """LTP map for ATM±N CE/PE — used by Greek strike picker."""
    step = Decimal(str(self._config.symbols.get("strike_step", 50)))
    band = int(
      (self._config.strategy.get("strike_selection") or {}).get("atm_band_steps", 1)
    )
    states: dict[str, OptionState | None] = {}
    for side in ("CE", "PE"):
      for inst in atm_band_instruments(universe, side, band_steps=band, step=step):
        states[inst.token] = self._option_data.get(inst.token)
    if universe.atm_ce:
      states.setdefault(
        universe.atm_ce.token, self._option_data.get(universe.atm_ce.token)
      )
    if universe.atm_pe:
      states.setdefault(
        universe.atm_pe.token, self._option_data.get(universe.atm_pe.token)
      )
    return states

  def _refresh_chain_and_option_context(
    self,
    universe: ContractUniverse,
    band_states: dict[str, OptionState | None],
    spot: Decimal | None,
  ) -> None:
    """OI/PCR/MaxPain + ATM option VWAP/Greeks into FeatureEngine."""
    chain = build_chain_snapshot(universe, band_states, prior_oi=self._prior_oi)
    self.features.set_chain_snapshot(chain)
    # Refresh prior OI baseline for next delta
    for tok, st in band_states.items():
      if st is not None and st.oi is not None:
        self._prior_oi[tok] = int(st.oi)

    # Prefer ATM side matching live bias for option VWAP / greeks context
    atm = universe.atm_ce or universe.atm_pe
    bias_spot = spot
    vwap = self._market_data.session_vwap_value
    prefer_pe = (
      bias_spot is not None and vwap is not None and bias_spot < vwap
    )
    inst = universe.atm_pe if prefer_pe and universe.atm_pe else universe.atm_ce
    if inst is None:
      inst = atm
    ctx: dict = {}
    if inst is not None:
      st = band_states.get(inst.token) or self._option_data.get(inst.token)
      if st is not None:
        ctx["ltp"] = float(st.ltp) if st.ltp is not None else None
        ctx["spread_pct"] = float(st.spread_pct) if st.spread_pct is not None else None
        ctx["volume"] = st.volume
        ctx["oi"] = st.oi
        ctx["option_vwap"] = self._option_data.option_vwap(inst.token)
        if spot is not None and st.ltp is not None and inst.expiry_date is not None:
          g = compute_greeks(
            spot=float(spot),
            strike=float(inst.strike),
            premium=float(st.ltp),
            option_type=inst.option_type,
            expiry=inst.expiry_date.date(),
          )
          ctx["delta"] = g.delta
          ctx["gamma"] = g.gamma
          ctx["iv"] = g.iv
          ctx["theta"] = g.theta
          ctx["vega"] = g.vega
    self.features.set_option_context(ctx)

  async def _scan_for_entry(self) -> None:
    if self._universe is None:
      return

    if self._underlyings and self._switch_underlying is not None:
      symbol = self._underlyings[self._scan_underlying_idx % len(self._underlyings)]
      self._scan_underlying_idx += 1
      try:
        await self._switch_underlying(symbol)
      except Exception:
        logger.exception("underlying_switch_failed", symbol=symbol)
        return

    if self._universe is None:
      return

    refreshed = await self._market_data.refresh_session_candles()
    if refreshed:
      for interval_candles in self._market_data._candles.values():
        await self._journal.write_candles(interval_candles)

    # Stale candle feed → do not trade on outdated VWAP/setups.
    # A failed refresh alone is OK if we still have a fresh last bar.
    if self._market_data.candles_stale():
      if self._log_every:
        latest = self._market_data.latest_candle_ts()
        await self._journal.write_system_event(
          SystemEvent(
            event_type="strategy_decision",
            ts=datetime.now(tz=timezone.utc),
            severity="warning",
            message="NO_TRADE",
            metadata={
              "selected_strategy": "NO_TRADE",
              "selected_reason": "stale_candle_feed",
              "trade_allowed": False,
              "confidence": 0,
              "last_m1_ts": latest.isoformat() if latest else None,
              "last_refresh_ok": self._market_data.last_refresh_ok,
            },
          )
        )
      return

    features = self.features.compute()
    m1 = self._market_data.candles(CandleInterval.M1)
    m5 = self._market_data.candles(CandleInterval.M5)
    is_expiry = self._is_expiry_day()
    self.features.is_expiry_day = is_expiry
    regime = self.regime.classify(
      features, m1, m5, is_expiry_day=is_expiry
    )
    self.positions.set_regime(regime.primary)

    # Retarget ATM from live spot so concurrent books can use moved strikes
    # (same behaviour as day_backtest bar-by-bar ATM rounding).
    spot = features.nifty_spot or self._market_data.spot_ltp
    universe = self._universe
    if spot is not None:
      universe = self._contract_selector.retarget_atm(self._universe, spot)
      self._universe = universe

    # ATM±1 (and ATM) LTP map for Greek strike selection on both sides.
    band_states = self._band_option_states(universe)
    self._refresh_chain_and_option_context(universe, band_states, spot)
    # Recompute features with chain + option Greeks context.
    features = self.features.compute()
    ce_state = (
      band_states.get(universe.atm_ce.token) if universe.atm_ce else None
    )
    pe_state = (
      band_states.get(universe.atm_pe.token) if universe.atm_pe else None
    )

    options_by_strategy = {s.name: band_states for s in self.router._scanners}

    decision, signal = self.router.route(
      features, regime, universe, options_by_strategy
    )

    # Persist every scan decision so the Decision Logs page stays current.
    if self._log_every:
      meta = decision.model_dump(mode="json")
      meta["scan_interval_seconds"] = self._scan_interval_sec
      meta["ce_ltp"] = float(ce_state.ltp) if ce_state and ce_state.ltp is not None else None
      meta["pe_ltp"] = float(pe_state.ltp) if pe_state and pe_state.ltp is not None else None
      await self._journal.write_system_event(
        SystemEvent(
          event_type="strategy_decision",
          ts=decision.ts,
          severity="info" if decision.trade_allowed else "debug",
          message=decision.selected_reason or decision.selected_strategy,
          metadata=meta,
        )
      )

    if signal is None:
      logger.debug(
        "scan_no_trade",
        strategy=decision.selected_strategy,
        reason=decision.selected_reason,
        bias=features.bias_5m.value,
        regime=regime.primary,
        setup=features.setup_3m,
        trigger=features.trigger_1m,
        pullback=(features.extra or {}).get("setup_vwap_pullback"),
        pe_ltp=str(pe_state.ltp) if pe_state and pe_state.ltp is not None else None,
        ce_ltp=str(ce_state.ltp) if ce_state and ce_state.ltp is not None else None,
      )
      return

    await self._execute_signal(signal, is_expiry=is_expiry)

  async def _try_reversal_flip(self, flip: dict) -> None:
    """After trend_reversal exit, buy the opposite side (CE↔PE) if risk allows."""
    if self._universe is None:
      return
    side = flip.get("side")
    if side not in ("CE", "PE"):
      return

    features = self.features.compute()
    spot = features.nifty_spot or self._market_data.spot_ltp
    if spot is None:
      return
    universe = self._contract_selector.retarget_atm(self._universe, spot)
    self._universe = universe
    band_states = self._band_option_states(universe)
    resolved = resolve_side_contract(
      config=self._config,
      universe=universe,
      side=side,
      spot=spot,
      option_states=band_states,
    )
    if resolved is None:
      logger.info("reversal_flip_no_contract", side=side)
      return
    inst, opt_state, pick = resolved

    # Align feature bias with flip direction for quality scoring.
    bias = Bias.BULLISH if side == "CE" else Bias.BEARISH
    feat = features.model_copy(update={"bias_5m": bias})
    signal = CandidateSignal(
      ts=datetime.now(tz=timezone.utc),
      setup_type="trend_reversal_flip",
      side=side,
      instrument_token=inst.token,
      tsym=inst.tsym,
      strategy_version=self._config.strategy.get(
        "strategy_version", "strategy_router_v1.1.0"
      ),
      feature_snapshot=feat,
      confidence=80,
      scanner_metadata={
        "atm_strike": str(universe.atm_strike),
        "option_ltp": str(opt_state.ltp),
        "exchange": inst.exchange,
        "lot_size": inst.lot_size,
        "flip_from": flip.get("from_side"),
        "strike_pick": pick,
      },
    )
    logger.info(
      "reversal_flip_entry_attempt",
      from_side=flip.get("from_side"),
      to_side=side,
      tsym=inst.tsym,
      strike=str(inst.strike),
      delta=pick.get("delta"),
    )
    await self._execute_signal(signal, is_expiry=self._is_expiry_day())

  async def _execute_signal(self, signal: CandidateSignal, *, is_expiry: bool) -> None:
    token = signal.instrument_token
    if self.positions.has_open_for_token(token) or token in self._entering_tokens:
      logger.info(
        "entry_skipped_duplicate_guard",
        tsym=signal.tsym,
        token=token,
        already_open=self.positions.has_open_for_token(token),
        entering=token in self._entering_tokens,
      )
      return

    self._entering_tokens.add(token)
    try:
      await self._execute_signal_locked(signal, is_expiry=is_expiry)
    finally:
      self._entering_tokens.discard(token)

  async def _execute_signal_locked(
    self, signal: CandidateSignal, *, is_expiry: bool
  ) -> None:
    token = signal.instrument_token
    # Re-check after any await gap from the caller path.
    if self.positions.has_open_for_token(token):
      return

    risk_snap = await self.risk.ensure_daily_state()
    if risk_snap.kill_switch or risk_snap.entries_blocked:
      await self._journal.write_system_event(
        SystemEvent(
          event_type="entry_skipped",
          ts=signal.ts,
          severity="warning",
          message=f"Signal {signal.setup_type} {signal.side} skipped — entries blocked",
          metadata={
            "setup": signal.setup_type,
            "side": signal.side,
            "tsym": signal.tsym,
            "confidence": signal.confidence,
            "kill_switch": risk_snap.kill_switch,
            "entries_blocked": risk_snap.entries_blocked,
            "block_reason": risk_snap.block_reason,
          },
        )
      )
      return

    await self._journal.write_candidate_signal(signal)

    opt_for_signal = self._option_data.get(token)
    if opt_for_signal is None or opt_for_signal.ltp is None:
      # Flip path may have just resolved from band map — synthesize OptionState.
      ltp = signal.scanner_metadata.get("option_ltp")
      if ltp is None:
        return
      opt_for_signal = OptionState(
        instrument_token=token,
        tsym=signal.tsym,
        ltp=Decimal(str(ltp)),
      )

    if self.positions.has_open_for_token(token):
      return

    validation = self.validator.validate(
      signal,
      opt_for_signal,
      has_open_for_token=self.positions.has_open_for_token(token),
      # Opposite-side flip must not be blocked by the exit we just did.
      in_cooldown=(
        False
        if signal.setup_type == "trend_reversal_flip"
        else self.positions.in_cooldown(token)
      ),
      kill_switch=risk_snap.kill_switch,
      is_expiry_day=is_expiry,
    )
    await self._journal.write_validation(validation)
    if not validation.passed:
      return

    sizing = await self.risk.size_entry(
      signal,
      opt_for_signal,
      risk_snap,
      open_position_count=self.positions.open_count,
    )
    if not sizing.approved:
      await self._journal.write_notification(
        "trade",
        "warning",
        "Entry blocked by risk",
        sizing.rejection_reason or "risk_rejected",
      )
      return

    # Final guard immediately before capital reserve + fill.
    if self.positions.has_open_for_token(token):
      logger.info(
        "entry_aborted_token_opened_during_sizing",
        tsym=signal.tsym,
        token=token,
      )
      return

    await self.risk.reserve_capital(sizing.premium_required)
    try:
      position_id, order_id, update = await self.execution.enter(signal, sizing)
      self.positions.register_open(
        position_id,
        order_id,
        signal.id,
        signal,
        sizing,
        update.fill_price or sizing.entry_ltp,
      )
    except Exception:
      await self.risk.release_capital(sizing.premium_required, Decimal("0"))
      logger.exception("paper_entry_failed", tsym=signal.tsym)
      raise

  def _is_expiry_day(self) -> bool:
    if self._universe is None or not self._universe.expiry_symbol:
      return False
    exp = parse_expiry_tag(self._universe.expiry_symbol)
    if exp is None:
      return False
    return exp == datetime.now(IST).date()

  def _market_open(self) -> bool:
    from algomcx.market_session import is_market_open

    return is_market_open(self._config.market_session)
