from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import structlog
import uvicorn

from algomcx.api.health import app as health_app
from algomcx.api.health import get_engine_state, set_engine_app, set_engine_state
from algomcx.broker.auth import resolve_session
from algomcx.broker.flattrade import FlattradeAdapter
from algomcx.broker.paper import PaperBrokerAdapter
from algomcx.bus.event_bus import EventBus
from algomcx.config import get_config
from algomcx.contract_selector.selector import ContractSelector, ContractUniverse
from algomcx.db.connection import close_pool, get_pool, init_pool
from algomcx.db.migrate import apply_migrations
from algomcx.db.paper_account import ensure_paper_account
from algomcx.journal.writer import JournalWriter
from algomcx.logging_setup import setup_logging
from algomcx.market_data.engine import MarketDataEngine
from algomcx.market_data.poller import RestQuotePoller, quote_from_rest
from algomcx.models.events import CandleInterval, QuoteUpdate, SystemEvent
from algomcx.option_data.layer import OptionDataLayer
from algomcx.trading.orchestrator import TradingOrchestrator
from algomcx.symbols_util import (
    apply_active_underlying,
    fallback_spot,
    list_underlyings,
    resolve_all_spot_tokens,
    strike_band_points,
    symbols_for,
)

logger = structlog.get_logger(__name__)


class TradingEngineApp:
    def __init__(self) -> None:
        self.config = get_config()
        self.bus = EventBus(max_size=self.config.runtime.get("event_queue_max_size", 10_000))
        self.journal = JournalWriter()
        self._tasks: list[asyncio.Task] = []
        self._universe: ContractUniverse | None = None
        self._universes: dict[str, ContractUniverse] = {}
        self._active_underlying = str(self.config.symbols.get("underlying", "GOLD")).upper()

        flattrade = FlattradeAdapter(self.config)
        self.broker = (
            PaperBrokerAdapter(self.config, flattrade)
            if self.config.is_paper
            else flattrade
        )
        self.market_data = MarketDataEngine(self.config, self.broker, self.bus)
        self.option_data = OptionDataLayer(self.config)
        self.contract_selector = ContractSelector(self.config, self.broker)
        self.orchestrator = TradingOrchestrator(
            self.config,
            self.broker,
            self.journal,
            self.market_data,
            self.option_data,
        )
        self.orchestrator.positions.set_trade_open_hook(self._on_position_opened)
        self.orchestrator.set_underlying_switcher(self._switch_trading_underlying)
        self._quote_poller = RestQuotePoller(
            self.broker,
            self.market_data,
            self.option_data,
            self.config.symbols["exchange_spot"],
            self.config.symbols["spot_token"],
        )
        self._feed_mode = "offline"
        self._ws_started = False
        self._last_ws_quote_ts: datetime | None = None
        self._ws_retry_after: datetime | None = None
        self._last_universe_refresh_date: date | None = None
        self._last_ws_keys: list[str] = []

    def _on_position_opened(self, _pos: Any) -> None:
        """Ensure the new holding is on the WebSocket feed for tick trails."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._ensure_holdings_on_websocket())
        except RuntimeError:
            pass

    def _has_api_credentials(self) -> bool:
        env = self.config.env
        return bool(env.flattrade_api_key and env.flattrade_api_secret)

    def _has_valid_session(self) -> bool:
        session = resolve_session(self.config.env)
        return session is not None and session.is_valid

    async def _handle_quote(self, quote: QuoteUpdate) -> None:
        await self.market_data.on_quote(quote)
        self.option_data.update_from_quote(quote)
        await self.orchestrator.on_quote(quote)
        if quote.source == "websocket":
            self._feed_mode = "websocket"
            self._last_ws_quote_ts = quote.ts
        set_engine_state(
            {
                "spot_ltp": str(self.market_data.spot_ltp) if self.market_data.spot_ltp else None,
                "last_quote_ts": quote.ts.isoformat(),
                "feed_mode": self._feed_mode,
                "ws_open": bool(getattr(self.broker, "websocket_open", False)),
            }
        )

    def _quote_callback(self, quote: QuoteUpdate) -> None:
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(self._handle_quote(quote), loop)

    async def _stop_subscription(self) -> None:
        stop = getattr(self.broker, "stop_websocket", None)
        if callable(stop):
            await stop()
        else:
            await self.broker.disconnect()
        self._ws_started = False
        self._last_ws_keys = []
        if self._feed_mode == "websocket":
            self._feed_mode = "offline"

    def _ws_subscription_keys(self) -> list[str]:
        """Universe ATM band + every open holding (trail exits need tick LTP).

        After ATM retarget / expiry roll, a held strike can fall outside the
        band and drop off WS — then MFE only updates on the ~2s REST poll and
        peaks are missed. Always keep holdings subscribed.
        """
        from algomcx.broker.base import BrokerAdapter

        keys: list[str] = []
        seen: set[str] = set()

        def _add(key: str) -> None:
            if key and key not in seen:
                seen.add(key)
                keys.append(key)

        if self._universe:
            for k in self._universe.subscription_keys:
                _add(k)

        for uni in self._universes.values():
            for k in uni.subscription_keys:
                _add(k)

        exchange = str(self.config.symbols.get("exchange_options", "NFO"))
        for pos in self.orchestrator.positions.open_positions:
            _add(BrokerAdapter.format_instrument(exchange, pos.instrument_token))
        return keys

    async def _start_subscription(self) -> None:
        if not self._universe:
            return
        now = datetime.now(tz=timezone.utc)
        if self._ws_retry_after and now < self._ws_retry_after:
            return
        keys = self._ws_subscription_keys()
        if not keys:
            return
        try:
            await self.broker.subscribe(keys, self._quote_callback)
            self._ws_started = True
            self._last_ws_keys = list(keys)
            if getattr(self.broker, "websocket_open", False):
                self._ws_retry_after = None
            else:
                self._ws_retry_after = now + timedelta(seconds=60)
            logger.info(
                "websocket_subscription_ready",
                keys=len(keys),
                open_holdings=self.orchestrator.positions.open_count,
            )
        except Exception:
            logger.exception("websocket_subscribe_failed")
            self._ws_started = False
            self._ws_retry_after = now + timedelta(seconds=60)

    async def _ensure_holdings_on_websocket(self) -> None:
        """Resubscribe if open holdings are missing from the current WS set."""
        if not self._ws_started or not self._is_market_open():
            return
        if self.orchestrator.positions.open_count < 1:
            return
        needed = self._ws_subscription_keys()
        if not needed:
            return
        if needed == self._last_ws_keys:
            return
        try:
            await self.broker.subscribe(needed, self._quote_callback)
            added = [k for k in needed if k not in self._last_ws_keys]
            self._last_ws_keys = list(needed)
            logger.info(
                "websocket_holdings_resubscribed",
                keys=len(needed),
                added=added,
                open_holdings=self.orchestrator.positions.open_count,
            )
        except Exception:
            logger.exception("websocket_holdings_resubscribe_failed")

    def _is_market_open(self) -> bool:
        from algomcx.market_session import is_market_open

        return is_market_open(self.config.market_session)

    def _ws_stale(self, max_age_sec: int = 8) -> bool:
        last = self._last_ws_quote_ts
        if last is None:
            return True
        return (datetime.now(tz=timezone.utc) - last).total_seconds() > max_age_sec

    async def _poll_rest_quotes_once(self) -> None:
        updated = 0
        primary = str(self.config.symbols.get("underlying", "GOLD")).upper()
        for sym, uni in self._universes.items():
            apply_active_underlying(self.config, sym)
            self._quote_poller.set_spot(
                str(self.config.symbols.get("exchange_spot", "MCX")),
                str(self.config.symbols.get("spot_token", "")),
            )
            updated += await self._quote_poller.poll_universe(uni)
        apply_active_underlying(self.config, primary)
        self._quote_poller.set_spot(
            str(self.config.symbols.get("exchange_spot", "MCX")),
            str(self.config.symbols.get("spot_token", "")),
        )
        if updated:
            if self._ws_stale():
                self._feed_mode = "rest"
            set_engine_state(
                {
                    "spot_ltp": str(self.market_data.spot_ltp)
                    if self.market_data.spot_ltp
                    else None,
                    "last_quote_ts": datetime.now(tz=timezone.utc).isoformat(),
                    "feed_mode": self._feed_mode,
                    "ws_open": bool(getattr(self.broker, "websocket_open", False)),
                }
            )
        # REST path used to update option_data only — drive holding exits too.
        await self._evaluate_open_exits_from_option_data(source="rest")

    async def _poll_open_position_quotes(self) -> None:
        """Refresh LTP for open holdings — prioritise tokens with stale/missing WS ticks."""
        open_positions = list(self.orchestrator.positions.open_positions)
        if not open_positions:
            return
        stale_sec = float(
            self.config.runtime.get("holding_ws_stale_seconds", 3)
        )
        stale_tokens = set(
            self.orchestrator.positions.stale_holding_tokens(stale_sec)
        )
        # Poll stale holdings first so MFE/trail sees peaks WS may have missed.
        open_positions.sort(
            key=lambda p: (0 if p.instrument_token in stale_tokens else 1, p.entry_ts)
        )
        exchange = self.config.symbols.get("exchange_options", "NFO")
        for pos in open_positions:
            try:
                raw = await self.broker.get_quotes(exchange, pos.instrument_token)
            except Exception:
                logger.exception("open_position_quote_failed", token=pos.instrument_token)
                continue
            quote = quote_from_rest(exchange, pos.instrument_token, raw)
            if quote is None or quote.ltp is None:
                continue
            quote.tsym = pos.tsym
            self.option_data.update_from_quote(quote)
            await self.orchestrator.on_quote(quote)

    async def _evaluate_open_exits_from_option_data(self, *, source: str) -> None:
        """Push latest option LTPs into PositionManager for trail / reverse exits."""
        for pos in list(self.orchestrator.positions.open_positions):
            state = self.option_data.get(pos.instrument_token)
            if state is None or state.ltp is None:
                continue
            quote = QuoteUpdate(
                ts=datetime.now(tz=timezone.utc),
                exchange=self.config.symbols.get("exchange_options", "NFO"),
                instrument_token=pos.instrument_token,
                tsym=pos.tsym,
                ltp=state.ltp,
                bid=state.bid,
                ask=state.ask,
                source=source,
            )
            await self.orchestrator.on_quote(quote)

    async def _run_rest_poll_loop(self) -> None:
        while True:
            try:
                # Keep open holdings on WS so trail MFE sees ticks, not only 2s REST.
                await self._ensure_holdings_on_websocket()
                # Full chain when WS is down; always keep open holdings ticking for trails.
                if not self._is_market_open() or self._ws_stale():
                    await self._poll_rest_quotes_once()
                elif self.orchestrator.positions.open_count > 0:
                    await self._poll_open_position_quotes()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rest_poll_failed")
            interval = int(self.config.runtime.get("rest_quote_poll_interval_seconds", 30))
            if self._is_market_open():
                interval = int(
                    self.config.runtime.get("rest_quote_poll_interval_market_seconds", 2)
                )
                if self.orchestrator.positions.open_count > 0:
                    stale = self.orchestrator.positions.stale_holding_tokens(
                        float(self.config.runtime.get("holding_ws_stale_seconds", 3))
                    )
                    if stale:
                        interval = int(
                            self.config.runtime.get("holding_rest_poll_seconds", 1)
                        )
            await asyncio.sleep(interval)

    async def _resolve_spot(self) -> Decimal:
        spot = await self._refresh_spot_from_rest()
        if spot is not None:
            return spot
        if self.market_data.spot_ltp is not None:
            return self.market_data.spot_ltp
        # After hours Flattrade quotes are often empty — use last known index
        # level from engine state or a realistic mid-range fallback (not ATM).
        raw = get_engine_state().get("spot_ltp")
        if raw not in (None, "", "None"):
            try:
                value = Decimal(str(raw))
                if value > 0:
                    return value
            except Exception:
                pass
        return fallback_spot(self.config, self._active_underlying)

    async def _switch_trading_underlying(self, symbol: str) -> None:
        sym = symbol.upper()
        merged = apply_active_underlying(self.config, sym)
        self._active_underlying = sym
        exchange = str(merged.get("exchange_spot", "MCX"))
        spot_token = str(merged.get("spot_token", ""))
        self.market_data.set_spot_context(exchange=exchange, spot_token=spot_token)
        self._quote_poller.set_spot(exchange, spot_token)

        universe = self._universes.get(sym)
        if universe is None or not universe.instruments:
            spot = await self._resolve_spot()
            universe = await self.contract_selector.build_universe(spot)
            self._universes[sym] = universe
            if universe.instruments:
                pool = get_pool()
                await self.contract_selector.persist_instruments(pool, universe)

        self._universe = universe
        self.orchestrator.set_universe(universe)
        if universe.instruments:
            self._universes[sym] = universe

    async def _refresh_universe(self, *, reason: str) -> bool:
        """Rebuild weekly option chain (ATM band) for current / next expiry."""
        from zoneinfo import ZoneInfo

        spot = await self._resolve_spot()

        previous = self._universe.expiry_symbol if self._universe else None
        had_instruments = bool(self._universe and self._universe.instruments)

        universe = await self.contract_selector.build_universe(spot)
        self._universe = universe
        self._universes[self._active_underlying] = universe
        self.orchestrator.set_universe(universe)
        self._last_universe_refresh_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()

        if universe.instruments:
            pool = get_pool()
            await self.contract_selector.persist_instruments(pool, universe)
            # Resubscribe WS if symbols changed or we previously had none.
            if (
                previous != universe.expiry_symbol
                or not had_instruments
                or self._ws_started
            ):
                if self._ws_started:
                    await self._stop_subscription()
                if self._is_market_open():
                    await self._start_subscription()
            await self._poll_rest_quotes_once()

        set_engine_state(
            {
                "instrument_count": len(universe.instruments),
                "atm_strike": str(universe.atm_strike),
                "expiry_symbol": universe.expiry_symbol,
                "spot_ltp": str(self.market_data.spot_ltp or spot),
            }
        )
        logger.info(
            "universe_refreshed",
            reason=reason,
            expiry=universe.expiry_symbol,
            instruments=len(universe.instruments),
            previous_expiry=previous,
            spot=str(spot),
        )
        if universe.instruments and previous != universe.expiry_symbol:
            await self.journal.write_notification(
                "system",
                "info",
                "Weekly expiry rolled",
                f"Now trading {universe.expiry_symbol} · {len(universe.instruments)} contracts",
            )
        return bool(universe.instruments)

    async def _maybe_roll_universe(self) -> None:
        """Auto-roll to next weekly after expiry day / empty chain / new IST day."""
        from zoneinfo import ZoneInfo

        from algomcx.contract_selector.expiry import parse_expiry_tag

        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        need = False
        reason = "periodic"

        if self._universe is None or not self._universe.instruments:
            need = True
            reason = "empty_universe"
        elif self._last_universe_refresh_date != today:
            need = True
            reason = "new_trading_day"
        else:
            exp = parse_expiry_tag(self._universe.expiry_symbol or "")
            if exp is not None and exp < today:
                need = True
                reason = "expiry_passed"
            elif exp == today and not self._is_market_open() and datetime.now(
                ZoneInfo("Asia/Kolkata")
            ).time().hour >= 15:
                # After close on expiry day, move to next weekly early.
                need = True
                reason = "expiry_day_closed"

        if need:
            await self._refresh_universe(reason=reason)

    async def _run_market_feed_loop(self) -> None:
        """Keep Flattrade WebSocket running during market hours for live option ticks."""
        while True:
            try:
                await self._maybe_roll_universe()
                if self._is_market_open() and self._universe:
                    if not self._universe.instruments:
                        await self._refresh_universe(reason="market_open_retry")
                    if not self._ws_started or not getattr(self.broker, "websocket_open", False):
                        if not self._ws_started:
                            logger.info("market_open_starting_websocket")
                            await self._start_subscription()
                        elif self._ws_stale(30):
                            logger.info("websocket_stale_restarting")
                            await self._stop_subscription()
                            await self._start_subscription()
                elif not self._is_market_open() and self._ws_started:
                    logger.info("market_closed_stopping_websocket")
                    await self._stop_subscription()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("market_feed_loop_failed")
            await asyncio.sleep(15)

    async def _refresh_spot_from_rest(self) -> Decimal | None:
        raw = await self.broker.get_quotes(
            self.config.symbols["exchange_spot"],
            self.config.symbols["spot_token"],
        )
        quote = quote_from_rest(
            self.config.symbols["exchange_spot"],
            self.config.symbols["spot_token"],
            raw,
        )
        if quote:
            await self.market_data.on_quote(quote)
            if quote.ltp is not None:
                return quote.ltp
        return None

    async def _setup_market_data(self) -> None:
        for field, available in self.option_data.probe_greek_availability().items():
            await self.journal.log_field_availability(field, "websocket", available)

        try:
            await resolve_all_spot_tokens(self.config, self.broker)
        except Exception:
            logger.exception("spot_token_resolution_failed")

        primary = str(self.config.symbols.get("underlying", "GOLD")).upper()
        merged = apply_active_underlying(self.config, primary)
        self.market_data.set_spot_context(
            exchange=str(merged.get("exchange_spot", "MCX")),
            spot_token=str(merged.get("spot_token", "")),
        )
        self._quote_poller.set_spot(
            str(merged.get("exchange_spot", "MCX")),
            str(merged.get("spot_token", "")),
        )

        await self.market_data.backfill_today()
        for interval_candles in self.market_data._candles.values():
            await self.journal.write_candles(interval_candles)

        spot = await self._resolve_spot()
        m1 = self.market_data.candles(CandleInterval.M1)
        if m1 and spot == fallback_spot(self.config, primary):
            spot = m1[-1].close

        pool = get_pool()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            apply_active_underlying(self.config, sym)
            self.market_data.set_spot_context(
                exchange=str(self.config.symbols.get("exchange_spot", "MCX")),
                spot_token=str(self.config.symbols.get("spot_token", "")),
            )
            self._quote_poller.set_spot(
                str(self.config.symbols.get("exchange_spot", "MCX")),
                str(self.config.symbols.get("spot_token", "")),
            )
            ul_spot = await self._refresh_spot_from_rest()
            if ul_spot is None:
                await asyncio.sleep(0.3)
                ul_spot = await self._refresh_spot_from_rest()
            if ul_spot is None:
                ul_spot = fallback_spot(self.config, sym)
            universe = await self.contract_selector.build_universe(ul_spot)
            universe.spot = ul_spot
            self._universes[sym] = universe
            await self.contract_selector.persist_instruments(pool, universe)
            await self._quote_poller.poll_universe(universe)

        apply_active_underlying(self.config, primary)
        self._universe = self._universes.get(primary)
        if self._universe is None:
            self._universe = await self.contract_selector.build_universe(spot)
            self._universes[primary] = self._universe
        self.orchestrator.set_universe(self._universe)
        self.market_data.set_spot_context(
            exchange=str(self.config.symbols.get("exchange_spot", "MCX")),
            spot_token=str(self.config.symbols.get("spot_token", "")),
        )
        self._quote_poller.set_spot(
            str(self.config.symbols.get("exchange_spot", "MCX")),
            str(self.config.symbols.get("spot_token", "")),
        )
        from zoneinfo import ZoneInfo

        self._last_universe_refresh_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        await self._poll_rest_quotes_once()

        set_engine_state(
            {
                "status": "running",
                "instrument_count": len(self._universe.instruments),
                "spot_ltp": str(self.market_data.spot_ltp or spot),
                "atm_strike": str(self._universe.atm_strike),
                "expiry_symbol": self._universe.expiry_symbol,
                "feed_mode": self._feed_mode,
            }
        )

        await self.orchestrator.initialize()
        scan_task = asyncio.create_task(self.orchestrator.run_periodic_scan())
        poll_task = asyncio.create_task(self._run_rest_poll_loop())
        feed_task = asyncio.create_task(self._run_market_feed_loop())
        self._tasks.extend([scan_task, poll_task, feed_task])

        logger.info("trading_engine_ready", instruments=len(self._universe.instruments))

    async def _ensure_session(self) -> bool:
        if not self._has_valid_session():
            if self.config.env.flattrade_password and self.config.env.flattrade_totp_secret:
                try:
                    from algomcx.broker.auth import login_and_save

                    await login_and_save(self.config.env)
                    logger.info("flattrade_auto_login_on_startup")
                except Exception:
                    logger.exception("flattrade_auto_login_failed")

            if not self._has_valid_session():
                logger.warning(
                    "flattrade_login_required",
                    hint="Run: python scripts/flattrade_auto_login.py",
                )
                await self.journal.write_notification(
                    "system",
                    "warning",
                    "Flattrade login required",
                    "Use Re-authenticate in the dashboard or run flattrade_auto_login.py.",
                )
                set_engine_state({"status": "standby"})
                return False
        return True

    async def reauthenticate(self, *, force: bool = True) -> dict[str, Any]:
        if not self._has_api_credentials():
            raise RuntimeError("Flattrade API credentials are not configured")

        from algomcx.broker.auth import login_and_save

        session = await login_and_save(self.config.env, force=force)
        was_subscribed = self._ws_started
        if was_subscribed:
            await self._stop_subscription()

        await self.broker.connect()
        set_engine_state({"broker_connected": True, "status": "running"})

        if self._universe is None:
            await self._setup_market_data()
        elif self._is_market_open():
            await self._start_subscription()

        return {
            "ok": True,
            "user_id": session.user_id,
            "expires_at": session.expires_at.isoformat(),
            "valid": session.is_valid,
            "broker_connected": True,
        }

    def _build_chain_items(
        self,
        universe: ContractUniverse,
        *,
        step: float,
        band: float,
        spot: Decimal | None,
        with_greeks: bool = True,
    ) -> list[dict[str, Any]]:
        from algomcx.contract_selector.expiry import parse_expiry_tag
        from algomcx.option_data.greeks import compute_greeks

        items: list[dict[str, Any]] = []
        if not universe.instruments or universe.atm_strike is None:
            return items

        atm_f = float(universe.atm_strike)
        rate = float(self.config.data_availability.get("risk_free_rate", 0.065))
        expiry_date = None
        if universe.expiry_symbol:
            expiry_date = parse_expiry_tag(universe.expiry_symbol)

        spot_f = float(spot) if spot is not None else float(universe.spot or 0)
        use_greeks = with_greeks and spot is not None and spot_f > 0

        atm_d = Decimal(str(atm_f))
        step_d = Decimal(str(step))
        band_d = Decimal(str(band))

        for inst in sorted(
            universe.instruments,
            key=lambda i: (i.strike, 0 if i.option_type == "CE" else 1),
        ):
            strike_d = inst.strike
            if abs(strike_d - atm_d) > band_d:
                continue
            diff = abs(strike_d - atm_d)
            if step_d > 0 and (diff % step_d) != 0:
                continue
            strike_f = float(strike_d)
            state = self.option_data.get(inst.token)
            lot_size = int(inst.lot_size)
            ltp = float(state.ltp) if state and state.ltp is not None else None
            greeks = None
            if use_greeks and ltp is not None:
                greeks = compute_greeks(
                    spot=spot_f,
                    strike=strike_f,
                    premium=ltp,
                    option_type=inst.option_type,
                    expiry=expiry_date,
                    rate=rate,
                )
            items.append(
                {
                    "token": inst.token,
                    "tsym": inst.tsym,
                    "strike": strike_f,
                    "option_type": inst.option_type,
                    "is_atm": abs(strike_f - atm_f) < 1e-9,
                    "tradable": abs(strike_f - atm_f) <= step + 1e-9,
                    "lot_size": lot_size,
                    "ltp": ltp,
                    "bid": float(state.bid) if state and state.bid is not None else None,
                    "ask": float(state.ask) if state and state.ask is not None else None,
                    "volume": state.volume if state else None,
                    "oi": state.oi if state else None,
                    "iv": round(greeks.iv * 100, 2)
                    if greeks and greeks.iv is not None
                    else None,
                    "delta": round(greeks.delta, 4)
                    if greeks and greeks.delta is not None
                    else None,
                    "gamma": round(greeks.gamma, 6)
                    if greeks and greeks.gamma is not None
                    else None,
                    "theta": round(greeks.theta, 2)
                    if greeks and greeks.theta is not None
                    else None,
                    "vega": round(greeks.vega, 2)
                    if greeks and greeks.vega is not None
                    else None,
                    "greeks_source": "black_scholes" if use_greeks else None,
                    "last_update_ts": state.last_update_ts.isoformat()
                    if state and state.last_update_ts
                    else None,
                }
            )
        return items

    def get_watchlist_snapshot(self) -> dict[str, Any]:
        underlying = self._active_underlying
        sym_cfg = symbols_for(self.config, underlying)
        step = float(sym_cfg.get("strike_step", 100))
        band = float(strike_band_points(sym_cfg))
        atm_steps = int(sym_cfg.get("atm_strike_steps", 10))

        commodities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            seen.add(sym)
            uni = self._universes.get(sym)
            cfg = symbols_for(self.config, sym)
            c_step = float(cfg.get("strike_step", step))
            c_band = float(strike_band_points(cfg))
            c_steps = int(cfg.get("atm_strike_steps", atm_steps))
            spot_val = uni.spot if uni else fallback_spot(self.config, sym)
            chain_items = (
                self._build_chain_items(uni, step=c_step, band=c_band, spot=spot_val)
                if uni and uni.instruments
                else []
            )
            commodities.append(
                {
                    "underlying": sym,
                    "display_name": str(cfg.get("display_name", sym)),
                    "spot_ltp": float(spot_val) if spot_val is not None else None,
                    "atm_strike": float(uni.atm_strike)
                    if uni and uni.atm_strike is not None
                    else None,
                    "expiry_symbol": uni.expiry_symbol if uni else None,
                    "instrument_count": len(uni.instruments) if uni else 0,
                    "strike_band_points": c_band,
                    "strike_step": c_step,
                    "atm_strike_steps": c_steps,
                    "items": chain_items,
                    "strike_count": len({i["strike"] for i in chain_items}),
                }
            )
        for sym, uni in self._universes.items():
            if sym in seen:
                continue
            cfg = symbols_for(self.config, sym)
            c_step = float(cfg.get("strike_step", step))
            c_band = float(strike_band_points(cfg))
            c_steps = int(cfg.get("atm_strike_steps", atm_steps))
            spot = uni.spot
            chain_items = self._build_chain_items(
                uni, step=c_step, band=c_band, spot=spot
            )
            commodities.append(
                {
                    "underlying": sym,
                    "display_name": str(cfg.get("display_name", sym)),
                    "spot_ltp": float(spot) if spot is not None else None,
                    "atm_strike": float(uni.atm_strike)
                    if uni.atm_strike is not None
                    else None,
                    "expiry_symbol": uni.expiry_symbol,
                    "instrument_count": len(uni.instruments),
                    "strike_band_points": c_band,
                    "strike_step": c_step,
                    "atm_strike_steps": c_steps,
                    "items": chain_items,
                    "strike_count": len({i["strike"] for i in chain_items}),
                }
            )

        active = next((c for c in commodities if c["underlying"] == underlying), None)
        if active is None and commodities:
            active = commodities[0]
        items = active["items"] if active else []
        spot = active["spot_ltp"] if active else None
        atm_strike = active["atm_strike"] if active else None
        expiry_symbol = active["expiry_symbol"] if active else None

        open_positions = []
        for p in self.orchestrator.positions.open_positions:
            lot_size = 65
            if self._universe:
                match = next(
                    (i for i in self._universe.instruments if i.token == p.instrument_token),
                    None,
                )
                if match:
                    lot_size = int(match.lot_size)
            state = self.option_data.get(p.instrument_token)
            ltp = state.ltp if state and state.ltp is not None else p.entry_price
            open_positions.append(
                {
                    "position_id": str(p.position_id),
                    "tsym": p.tsym,
                    "side": p.option_side,
                    "quantity": p.quantity,
                    "lot_size": lot_size,
                    "lots": p.quantity // max(lot_size, 1),
                    "entry_price": float(p.entry_price),
                    "entry_ts": p.entry_ts.isoformat(),
                    "current_ltp": float(ltp),
                    "unrealized_pnl": float((ltp - p.entry_price) * p.quantity),
                    "premium_deployed": float(p.premium_deployed),
                    "setup_type": p.setup_type,
                }
            )

        return {
            "underlying": underlying,
            "active_underlying": self._active_underlying,
            "commodities": commodities,
            "spot_ltp": spot,
            "atm_strike": atm_strike,
            "expiry_symbol": expiry_symbol,
            "instrument_count": len(items),
            "strike_count": len({i["strike"] for i in items}),
            "strike_band_points": band,
            "strike_step": step,
            "atm_strike_steps": atm_steps,
            "last_quote_ts": get_engine_state().get("last_quote_ts"),
            "feed_mode": self._feed_mode,
            "ws_open": bool(getattr(self.broker, "websocket_open", False)),
            "market_open": self._is_market_open(),
            "greeks_source": "black_scholes",
            "items": items,
            "open_positions": open_positions,
        }

    async def get_trade_blotter(self, limit: int = 200) -> list[dict[str, Any]]:
        """Today's closed trades with entry/exit time, LTP, lots, P&L."""
        pool = get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    ct.id,
                    ct.entry_ts,
                    ct.exit_ts,
                    ct.entry_price,
                    ct.exit_price,
                    ct.quantity,
                    ct.pnl,
                    ct.exit_reason,
                    ct.setup_type,
                    ct.hold_seconds,
                    p.tsym,
                    p.side AS position_side
                FROM closed_trades ct
                JOIN positions p ON p.id = ct.position_id
                WHERE (ct.entry_ts AT TIME ZONE 'Asia/Kolkata')::date
                      = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                ORDER BY ct.exit_ts DESC
                LIMIT $1
                """,
                limit,
            )
        blotter = []
        lot_default = 65
        for r in rows:
            qty = int(r["quantity"])
            lot_size = lot_default
            if self._universe:
                match = next((i for i in self._universe.instruments if i.tsym == r["tsym"]), None)
                if match:
                    lot_size = match.lot_size
            blotter.append(
                {
                    "id": str(r["id"]),
                    "tsym": r["tsym"],
                    "side": r["position_side"],
                    "entry_ts": r["entry_ts"].isoformat(),
                    "exit_ts": r["exit_ts"].isoformat(),
                    "entry_price": float(r["entry_price"]),
                    "exit_price": float(r["exit_price"]),
                    "quantity": qty,
                    "lot_size": lot_size,
                    "lots": qty // max(lot_size, 1),
                    "pnl": float(r["pnl"]),
                    "exit_reason": r["exit_reason"],
                    "setup_type": r["setup_type"],
                    "hold_seconds": r["hold_seconds"],
                }
            )
        return blotter

    async def sync_missing_data(self) -> dict[str, Any]:
        """Fetch from Flattrade only what DB / in-memory state is missing."""
        from zoneinfo import ZoneInfo

        band = float(self.config.symbols.get("strike_band_points", 300))
        step = float(self.config.symbols.get("strike_step", 50))
        expected_strikes = int(round((2 * band) / step)) + 1
        expected_inst = expected_strikes * 2
        spot_token = self.config.symbols["spot_token"]
        pool = get_pool()

        report: dict[str, Any] = {
            "ok": True,
            "universe": {},
            "candles": {},
            "quotes": {},
        }

        async with pool.acquire() as conn:
            before_inst = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM instruments WHERE in_band = TRUE"
                )
                or 0
            )
            m1_before = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_1m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )
            m3_before = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_3m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )
            m5_before = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_5m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )

        need_universe = (
            before_inst < expected_inst
            or self._universe is None
            or not self._universe.instruments
        )
        if need_universe:
            await self._refresh_universe(reason="manual_sync_missing")
            universe_action = "refreshed"
        else:
            universe_action = "ok"

        after_inst = len(self._universe.instruments) if self._universe else 0
        report["universe"] = {
            "action": universe_action,
            "before": before_inst,
            "after": after_inst,
            "expected": expected_inst,
            "expiry_symbol": self._universe.expiry_symbol if self._universe else None,
        }

        await self.market_data.refresh_session_candles(force=True)
        for interval_candles in self.market_data._candles.values():
            await self.journal.write_candles(interval_candles)

        async with pool.acquire() as conn:
            m1_after = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_1m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )
            m3_after = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_3m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )
            m5_after = int(
                await conn.fetchval(
                    "SELECT COUNT(*) FROM candles_5m WHERE instrument_token = $1",
                    spot_token,
                )
                or 0
            )

        report["candles"] = {
            "m1_in_memory": len(self.market_data.candles(CandleInterval.M1)),
            "m3_in_memory": len(self.market_data.candles(CandleInterval.M3)),
            "m5_in_memory": len(self.market_data.candles(CandleInterval.M5)),
            "m1_added": max(0, m1_after - m1_before),
            "m3_added": max(0, m3_after - m3_before),
            "m5_added": max(0, m5_after - m5_before),
        }

        missing_tokens: list[str] = []
        if self._universe:
            for inst in self._universe.instruments:
                state = self.option_data.get(inst.token)
                if state is None or state.ltp is None:
                    missing_tokens.append(inst.token)

        report["quotes"]["missing_ltp_before"] = len(missing_tokens)
        polled = 0
        if missing_tokens and self._universe:
            by_token = {i.token: i for i in self._universe.instruments}
            async def _one(token: str):
                inst = by_token[token]
                raw = await self.broker.get_quotes(inst.exchange, inst.token)
                from algomcx.market_data.poller import quote_from_rest

                return quote_from_rest(inst.exchange, inst.token, raw)

            results = await asyncio.gather(
                *[_one(t) for t in missing_tokens],
                return_exceptions=True,
            )
            for q in results:
                if isinstance(q, Exception) or q is None:
                    continue
                await self.market_data.on_quote(q)
                self.option_data.update_from_quote(q)
                polled += 1
        else:
            await self._poll_rest_quotes_once()
            polled = after_inst

        still_missing = 0
        if self._universe:
            for inst in self._universe.instruments:
                state = self.option_data.get(inst.token)
                if state is None or state.ltp is None:
                    still_missing += 1
        report["quotes"]["polled"] = polled
        report["quotes"]["missing_ltp_after"] = still_missing
        report["spot_ltp"] = (
            float(self.market_data.spot_ltp) if self.market_data.spot_ltp is not None else None
        )
        report["ist_time"] = datetime.now(ZoneInfo("Asia/Kolkata")).isoformat()

        await self.journal.write_system_event(
            SystemEvent(
                event_type="manual_sync",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message="manual_sync_missing_data",
                metadata=report,
            )
        )
        return report

    async def reset_paper_account(self) -> dict[str, Any]:
        """Wipe mock/paper trades and restore capital for a clean session."""
        capital = Decimal(str(self.config.risk.get("account_capital_inr", 50000)))
        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM closed_trades")
                await conn.execute("DELETE FROM positions")
                await conn.execute("DELETE FROM orders")
                await conn.execute("DELETE FROM validation_results")
                await conn.execute("DELETE FROM ml_scores")
                await conn.execute("DELETE FROM candidate_signals")
                # Drop prior-day risk rows so carry-forward does not revive old equity.
                await conn.execute(
                    """
                    DELETE FROM daily_risk_state
                    WHERE trade_date <> (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                    """
                )
                await conn.execute(
                    "DELETE FROM instruments WHERE in_band = FALSE OR expiry_date < CURRENT_DATE"
                )
                await conn.execute(
                    """
                    UPDATE daily_risk_state SET
                        starting_capital = $1,
                        available_capital = $1,
                        deployed_capital = 0,
                        realized_pnl = 0,
                        trade_count = 0,
                        consecutive_losses = 0,
                        kill_switch = FALSE,
                        entries_blocked = FALSE,
                        block_reason = NULL,
                        updated_at = now()
                    WHERE trade_date = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                    """,
                    capital,
                )
                # Ensure today's risk row exists
                await conn.execute(
                    """
                    INSERT INTO daily_risk_state (
                        trade_date, starting_capital, available_capital, deployed_capital,
                        realized_pnl, trade_count, consecutive_losses, kill_switch, entries_blocked
                    ) VALUES (
                        (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date,
                        $1, $1, 0, 0, 0, 0, FALSE, FALSE
                    )
                    ON CONFLICT (trade_date) DO NOTHING
                    """,
                    capital,
                )
                await conn.execute(
                    """
                    INSERT INTO notifications (type, severity, title, message)
                    VALUES ('system', 'info', 'Paper account reset', $1)
                    """,
                    f"Balance restored to ₹{capital:,.0f} · old trades and expired tokens removed",
                )

        # Clear in-memory open positions + exit cooldowns so trading can resume.
        self.orchestrator.positions._open.clear()
        self.orchestrator.positions.clear_cooldowns()
        self.orchestrator.positions._pending_flips.clear()
        await self._refresh_universe(reason="paper_account_reset")
        # Force a fresh candle pull so the next scan is not on pre-reset bars.
        refreshed = await self.market_data.refresh_session_candles(force=True)
        if refreshed:
            for interval_candles in self.market_data._candles.values():
                await self.journal.write_candles(interval_candles)
        return {
            "ok": True,
            "starting_capital": float(capital),
            "available_capital": float(capital),
            "expiry_symbol": self._universe.expiry_symbol if self._universe else None,
            "instrument_count": len(self._universe.instruments) if self._universe else 0,
            "candles_refreshed": refreshed,
            "m1_count": len(self.market_data.candles(CandleInterval.M1)),
        }

    def get_market_summary(self) -> dict[str, Any]:
        from zoneinfo import ZoneInfo

        from algomcx.market_session import session_label

        ist = datetime.now(ZoneInfo("Asia/Kolkata"))
        ms = self.config.market_session
        session = session_label(ms)

        m5 = self.market_data.candles(CandleInterval.M5)
        features = self.orchestrator.features.compute()
        bias = features.bias_5m.value.upper()

        vwap = self.market_data.session_vwap_value
        spot = self.market_data.spot_ltp
        spot_vs_vwap: str | None = None
        if spot is not None and vwap is not None:
            if spot > vwap:
                spot_vs_vwap = "ABOVE"
            elif spot < vwap:
                spot_vs_vwap = "BELOW"
            else:
                spot_vs_vwap = "AT"

        decision = self.orchestrator.router.last_decision
        commodity_rows: list[dict[str, Any]] = []
        if self._universes:
            for sym, uni in self._universes.items():
                cfg = symbols_for(self.config, sym)
                commodity_rows.append(
                    {
                        "underlying": sym,
                        "display_name": str(cfg.get("display_name", sym)),
                        "spot_ltp": float(uni.spot) if uni.spot is not None else None,
                        "atm_strike": float(uni.atm_strike)
                        if uni.atm_strike is not None
                        else None,
                        "expiry_symbol": uni.expiry_symbol,
                    }
                )
        else:
            for row in list_underlyings(self.config):
                commodity_rows.append(
                    {
                        "underlying": str(row.get("symbol", "")).upper(),
                        "display_name": str(row.get("display_name", row.get("symbol", ""))),
                        "spot_ltp": None,
                        "atm_strike": None,
                        "expiry_symbol": None,
                    }
                )

        active_uni = self._universes.get(self._active_underlying)
        return {
            "underlying": self._active_underlying,
            "active_underlying": self._active_underlying,
            "commodities": commodity_rows,
            "spot_ltp": float(active_uni.spot)
            if active_uni and active_uni.spot is not None
            else (float(spot) if spot is not None else None),
            "session_vwap": float(vwap) if vwap is not None else None,
            "spot_vs_vwap": spot_vs_vwap,
            "atm_strike": float(active_uni.atm_strike)
            if active_uni and active_uni.atm_strike is not None
            else None,
            "bias_5m": bias,
            "market_session": session,
            "market_open": session == "OPEN",
            "strategy": (
                decision.selected_strategy
                if decision
                else self.config.strategy.get("active_scanner", "router")
            ),
            "regime": decision.regime.primary if decision and decision.regime else None,
            "confidence": decision.confidence if decision else None,
            "trading_mode": self.config.env.trading_mode,
            "ist_time": ist.isoformat(),
            "feed_mode": self._feed_mode,
            "expiry_symbol": active_uni.expiry_symbol if active_uni else None,
            "instrument_count": len(active_uni.instruments) if active_uni else 0,
            "m5_count": len(m5),
        }

    async def start(self) -> None:
        setup_logging(self.config.env.log_level, self.config.logging.get("format", "json"))
        await init_pool()
        await apply_migrations()
        capital = float(self.config.risk.get("account_capital_inr", 50000))
        await ensure_paper_account(capital)
        set_engine_state({"status": "running", "broker_connected": False})

        await self.journal.write_system_event(
            SystemEvent(
                event_type="SYSTEM_START",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message="Trading engine started",
                metadata={"mode": self.config.env.trading_mode},
            )
        )

        if not self._has_api_credentials():
            logger.warning(
                "broker_api_key_missing",
                hint="Set FLATTRADE_API_KEY and FLATTRADE_API_SECRET in .env",
            )
            await self.journal.write_notification(
                "system",
                "warning",
                "Flattrade API key missing",
                "Set FLATTRADE_API_KEY and FLATTRADE_API_SECRET in .env (from Flattrade Wall → Pi).",
            )
            set_engine_state({"status": "standby"})
            return

        if not await self._ensure_session():
            return

        await self.broker.connect()
        set_engine_state({"broker_connected": True})
        await self._setup_market_data()
        try:
            n = await self.orchestrator.positions.rehydrate_open_positions()
            if n:
                logger.info("open_positions_restored", count=n)
                await self._ensure_holdings_on_websocket()
        except Exception:
            logger.exception("position_rehydrate_failed")

    async def stop(self) -> None:
        self.orchestrator.stop()
        await self._stop_subscription()
        await close_pool()
        set_engine_state({"status": "stopped", "broker_connected": False})


async def _run() -> None:
    engine = TradingEngineApp()
    set_engine_app(engine)
    import os

    port = int(os.environ.get("ENGINE_PORT", "8001"))
    config = uvicorn.Config(health_app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    api_task = asyncio.create_task(server.serve())

    try:
        await engine.start()
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await engine.stop()
        server.should_exit = True
        await api_task


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        logger.info("shutdown_signal_received")


if __name__ == "__main__":
    main()
