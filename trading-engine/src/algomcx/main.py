from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

import structlog
import uvicorn

from algomcx.api.health import app as health_app
from algomcx.api.health import get_engine_state, set_engine_app, set_engine_state
from algomcx.broker.auth import resolve_session
from algomcx.broker.credentials import load_flattrade_config
from algomcx.broker.flattrade import FlattradeAdapter
from algomcx.broker.paper import PaperBrokerAdapter
from algomcx.broker.routed import RoutedBrokerAdapter
from algomcx.bus.event_bus import EventBus
from algomcx.config import get_config
from algomcx.contract_selector.selector import ContractSelector, ContractUniverse
from algomcx.db.connection import close_pool, get_pool, init_pool
from algomcx.db.migrate import apply_migrations
from algomcx.db.paper_account import ensure_paper_account
from algomcx.journal.writer import JournalWriter
from algomcx.logging_setup import setup_logging
from algomcx.market_data.engine import (
    FRESH_SETUP_LOOKBACK_DAYS,
    MarketDataEngine,
    needs_fresh_setup_backfill,
)
from algomcx.market_data.poller import RestQuotePoller, quote_from_rest
from algomcx.models.events import Candle, CandleInterval, QuoteUpdate, SystemEvent
from algomcx.option_data.layer import OptionDataLayer
from algomcx.trading.orchestrator import TradingOrchestrator
from algomcx.runtime.trading_mode import (
    get_execution_mode,
    init_execution_mode,
    load_execution_mode_from_db,
    set_execution_mode,
)
from algomcx.symbols_util import (
    NIFTY_LOT_SIZE,
    apply_active_underlying,
    capital_for,
    fallback_spot,
    fut_expiry_label_from_tsym,
    list_index_tickers,
    list_underlyings,
    price_context,
    price_exchange_for,
    resolve_all_spot_tokens,
    resolve_index_future_token,
    resolve_index_ticker_tokens,
    risk_underlying_key,
    strike_band_points,
    symbols_for,
    total_account_capital,
    is_ticker_only,
    is_sane_nifty_spot,
    is_sane_spot,
    uses_futures_price,
    uses_pooled_capital,
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
        paper = PaperBrokerAdapter(self.config, flattrade)
        init_execution_mode(self.config.env.trading_mode)
        self._flattrade = flattrade
        self.broker = RoutedBrokerAdapter(flattrade, paper)
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
        self._last_log_maintenance_date: date | None = None
        self._last_ws_keys: list[str] = []
        self._index_spot_ltps: dict[str, Decimal] = {}
        self._sticky_index_spots: dict[str, Decimal] = {}
        self._fut_contract_meta: dict[str, dict[str, str]] = {}
        self._token_to_underlying: dict[str, str] = {}
        self._session_open_by_symbol: dict[str, dict[date, Decimal]] = {}
        self._ticker_only_symbols = {
            str(row.get("symbol", "")).upper()
            for row in list_index_tickers(self.config)
            if row.get("symbol")
        }
        self._chain_item_cache: dict[str, list[dict[str, Any]]] = {}
        self._last_chain_options_poll: datetime | None = None
        self._index_quote_tokens: set[str] = set()
        self._last_option_ws_ts: datetime | None = None
        self._watchlist_tick = asyncio.Event()
        self._futures_watchlist_items: list[dict[str, Any]] = []
        self._futures_watchlist_tokens: set[str] = set()

    def _uses_futures_watchlist(self) -> bool:
        return str(self.config.symbols.get("watchlist_mode", "")).lower() == "futures"

    @property
    def watchlist_tick(self) -> asyncio.Event:
        return self._watchlist_tick

    def _on_position_opened(self, _pos: Any) -> None:
        """Ensure the new holding is on the WebSocket feed for tick trails."""
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self._ensure_holdings_on_websocket())
        except RuntimeError:
            pass

    async def _has_api_credentials(self) -> bool:
        cfg = await load_flattrade_config()
        return cfg.has_api_credentials()

    async def _has_valid_session(self) -> bool:
        cfg = await load_flattrade_config()
        session = resolve_session(cfg)
        return session is not None and session.is_valid

    async def _handle_quote(self, quote: QuoteUpdate) -> None:
        ul = self._token_to_underlying.get(quote.instrument_token)
        if ul and quote.ltp is not None:
            await self._apply_index_spot(ul, quote.ltp)
        await self.market_data.on_quote(quote)
        self.option_data.update_from_quote(quote)
        await self.orchestrator.on_quote(quote)
        if quote.source == "websocket":
            self._feed_mode = "websocket"
            self._last_ws_quote_ts = quote.ts
            if quote.instrument_token in self._index_quote_tokens:
                self._watchlist_tick.set()
            elif quote.instrument_token in self._futures_watchlist_tokens:
                self._watchlist_tick.set()
            else:
                self._last_option_ws_ts = quote.ts
                self._watchlist_tick.set()
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

        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            token = str(row.get("spot_token", "")).strip()
            if not sym or not token:
                continue
            exchange = price_exchange_for(row)
            _add(BrokerAdapter.format_instrument(exchange, token))

        for row in list_index_tickers(self.config):
            sym = str(row.get("symbol", "")).upper()
            token = str(row.get("spot_token", "")).strip()
            if not sym or not token:
                continue
            exchange = str(row.get("exchange", "NSE"))
            _add(BrokerAdapter.format_instrument(exchange, token))

        for pos in self.orchestrator.positions.open_positions:
            pos_exchange = str(self.config.symbols.get("exchange_options", "NFO"))
            for uni in self._universes.values():
                match = next(
                    (i for i in uni.instruments if i.token == pos.instrument_token),
                    None,
                )
                if match:
                    pos_exchange = match.exchange
                    break
            _add(BrokerAdapter.format_instrument(pos_exchange, pos.instrument_token))

        if self._uses_futures_watchlist():
            exchange = str(self.config.symbols.get("exchange_spot", "MCX"))
            for row in self._futures_watchlist_items:
                token = str(row.get("token", "")).strip()
                if token:
                    _add(BrokerAdapter.format_instrument(exchange, token))

        return list(keys)

    async def _start_subscription(self) -> None:
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

    def _register_index_spot_tokens(self) -> None:
        """Map each index futures token → underlying for live spot on all books."""
        self._token_to_underlying.clear()
        self._index_quote_tokens = set()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            token = str(row.get("spot_token", "")).strip()
            if sym and token:
                self._token_to_underlying[token] = sym
                self._index_quote_tokens.add(token)
        for row in list_index_tickers(self.config):
            sym = str(row.get("symbol", "")).upper()
            token = str(row.get("spot_token", "")).strip()
            if sym and token:
                self._token_to_underlying[token] = sym
                self._index_quote_tokens.add(token)

    @staticmethod
    def _spots_diverge(a: Decimal, b: Decimal, pct: float = 0.008) -> bool:
        if a <= 0 or b <= 0:
            return True
        return abs(a - b) / a > Decimal(str(pct))

    def _trading_spot(self, sym: str, uni: ContractUniverse | None = None) -> Decimal | None:
        """Futures price used for ATM, option chain, header, and signals."""
        ul = sym.upper()
        uni = uni or self._universes.get(ul)

        if ul == self._active_underlying and self.market_data.spot_ltp is not None:
            px = self.market_data.spot_ltp
            if uni and uni.spot is not None and self._spots_diverge(px, uni.spot, 0.025):
                return uni.spot
            if is_sane_nifty_spot(px):
                return px

        if uni and uni.spot is not None and is_sane_nifty_spot(uni.spot):
            live = self._index_spot_ltps.get(ul)
            if live is not None and self._spots_diverge(uni.spot, live):
                return uni.spot
            return uni.spot

        live = self._index_spot_ltps.get(ul)
        if live is not None and is_sane_nifty_spot(live):
            return live

        sticky = self._sticky_index_spots.get(ul)
        if sticky is not None and is_sane_nifty_spot(sticky):
            if uni and uni.spot is not None and self._spots_diverge(sticky, uni.spot):
                return uni.spot
            return sticky
        return None

    def _header_spot(self, sym: str, uni: ContractUniverse | None = None) -> Decimal | None:
        """Top-bar LTP — same futures trading price for NIFTY and GOLD_FUT."""
        ul = sym.upper()
        if ul in self._ticker_only_symbols:
            live = self._index_spot_ltps.get(ul)
            if live is not None:
                return live
            sticky = self._sticky_index_spots.get(ul)
            if sticky is not None:
                return sticky
            return None
        if ul.endswith("_FUT"):
            base = ul.removesuffix("_FUT")
            return self._trading_spot(base, self._universes.get(base))
        return self._trading_spot(ul, uni)

    def _spot_for_symbol(self, sym: str, uni: ContractUniverse | None) -> Decimal | None:
        return self._trading_spot(sym, uni)

    def _sync_engine_spot_state(self, spot: Decimal | None) -> None:
        if spot is None or self._active_underlying is None:
            return
        set_engine_state({"spot_ltp": str(spot)})

    def _retarget_universe_spot(self, sym: str, spot: Decimal) -> None:
        uni = self._universes.get(sym.upper())
        if uni is None or not uni.instruments:
            return
        cfg = symbols_for(self.config, sym)
        step = float(cfg.get("strike_step", 50))
        step_d = Decimal(str(step))
        old_atm = uni.atm_strike
        updated = self.contract_selector.retarget_atm(uni, spot, strike_step=step)
        if old_atm is not None and abs(updated.atm_strike - old_atm) >= step_d:
            self._chain_item_cache.pop(sym.upper(), None)
        self._universes[sym.upper()] = updated
        if sym.upper() == self._active_underlying:
            self._universe = updated
            self.orchestrator.set_universe(updated)
        if sym.upper() == self._active_underlying:
            self._universe = updated
            self.orchestrator.set_universe(updated)

    def _strike_coverage_ok(
        self,
        uni: ContractUniverse,
        atm: Decimal,
        sym: str,
    ) -> bool:
        """True when universe has ATM±atm_strike_steps on the strike grid."""
        if not uni.instruments:
            return False
        cfg = symbols_for(self.config, sym)
        steps = int(cfg.get("atm_strike_steps", 5))
        step = Decimal(str(cfg.get("strike_step", 50)))
        strikes = {i.strike for i in uni.instruments}
        for offset in range(-steps, steps + 1):
            target = atm + (step * offset)
            if not any(abs(s - target) <= Decimal("0.01") for s in strikes):
                return False
        return True

    async def _rebuild_universe_for_symbol(
        self,
        ul: str,
        spot: Decimal,
        *,
        reason: str,
    ) -> bool:
        primary = self._active_underlying
        apply_active_underlying(self.config, ul)
        try:
            rebuilt = await self.contract_selector.build_universe(spot)
            rebuilt.spot = spot
            self._universes[ul] = rebuilt
            if ul == self._active_underlying:
                self._universe = rebuilt
                self.orchestrator.set_universe(rebuilt)
            if rebuilt.instruments:
                pool = get_pool()
                await self.contract_selector.persist_instruments(pool, rebuilt)
                px_exchange, px_token = price_context(symbols_for(self.config, ul))
                self._quote_poller.set_spot(px_exchange, px_token)
                await self._quote_poller.poll_universe(rebuilt)
            logger.info(
                "universe_rebuilt",
                symbol=ul,
                reason=reason,
                spot=str(spot),
                atm=str(rebuilt.atm_strike),
                instruments=len(rebuilt.instruments),
            )
            if self._is_market_open() and self._ws_started:
                await self._ensure_holdings_on_websocket()
            return bool(rebuilt.instruments)
        except Exception:
            logger.exception("universe_rebuild_failed", symbol=ul, reason=reason)
            return False
        finally:
            apply_active_underlying(self.config, primary)

    async def _apply_index_spot(self, sym: str, spot: Decimal) -> None:
        ul = sym.upper()
        if ul not in self._ticker_only_symbols and not is_sane_spot(spot, symbol=ul):
            logger.warning("index_spot_rejected_out_of_band", symbol=ul, spot=str(spot))
            return
        self._index_spot_ltps[ul] = spot
        self._sticky_index_spots[ul] = spot
        self._capture_session_open(ul, spot)
        self._watchlist_tick.set()
        if ul in self._ticker_only_symbols:
            return
        uni = self._universes.get(ul)
        if uni is not None:
            uni.spot = spot
        if ul == self._active_underlying:
            self._sync_engine_spot_state(spot)
        if self._uses_futures_watchlist():
            return
        if uni is None:
            return
        cfg = symbols_for(self.config, ul)
        step = float(cfg.get("strike_step", 50))
        step_d = Decimal(str(step))
        new_atm = self.contract_selector.atm_strike_for_spot(spot, strike_step=step)
        needs_rebuild = (
            not uni.instruments
            or abs(new_atm - uni.atm_strike) >= step_d
            or not self._strike_coverage_ok(uni, new_atm, ul)
        )
        if needs_rebuild:
            await self._rebuild_universe_for_symbol(ul, spot, reason="spot_drift")
        else:
            self._retarget_universe_spot(ul, spot)

    async def _ensure_header_quote_tokens(self) -> None:
        """Resolve futures / index tokens if missing (e.g. after config reload)."""
        try:
            await resolve_all_spot_tokens(self.config, self.broker)
            await resolve_index_ticker_tokens(self.config, self.broker)
            self._register_index_spot_tokens()
            for row in list_underlyings(self.config):
                sym = str(row.get("symbol", "")).upper()
                if sym:
                    self._sync_fut_contract_meta(sym)
        except Exception:
            logger.exception("header_token_resolution_failed")

    async def _last_close_from_db(self, sym: str) -> Decimal | None:
        """Last 1m candle close — REST fallback when broker quote is empty after hours."""
        ul = sym.upper()
        merged = symbols_for(self.config, ul)
        if is_ticker_only(self.config, ul):
            token = str(merged.get("spot_token", "")).strip()
        else:
            _, token = price_context(merged)
        if not token:
            return None
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT close FROM candles_1m
                    WHERE instrument_token = $1
                      AND ts >= NOW() - INTERVAL '10 days'
                    ORDER BY ts DESC
                    LIMIT 1
                    """,
                    token,
                )
            if row and row["close"] is not None:
                return Decimal(str(row["close"]))
        except Exception:
            logger.exception("header_spot_db_fallback_failed", symbol=ul)
        return None

    async def _hydrate_header_spots_from_db(self) -> None:
        """Fill missing header LTPs from last stored candle when live quotes are absent."""
        symbols: list[str] = []
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if sym:
                symbols.append(sym)
        for row in list_index_tickers(self.config):
            sym = str(row.get("symbol", "")).upper()
            if sym:
                symbols.append(sym)
        for sym in symbols:
            if sym in self._ticker_only_symbols:
                if self._header_spot(sym) is not None:
                    continue
            elif self._trading_spot(sym) is not None:
                continue
            db_close = await self._last_close_from_db(sym)
            if db_close is not None:
                await self._apply_index_spot(sym, db_close)

    async def _coalesce_spot_with_db(
        self, sym: str, quote_ltp: Decimal, *, token: str, exchange: str
    ) -> Decimal:
        """Prefer recent DB close when REST quote diverges sharply (wrong token/scrip)."""
        db_close = await self._last_close_from_db(sym)
        if (
            db_close is not None
            and is_sane_nifty_spot(db_close)
            and self._spots_diverge(quote_ltp, db_close, 0.025)
        ):
            logger.warning(
                "rest_spot_diverges_from_db",
                symbol=sym,
                exchange=exchange,
                token=token,
                rest=str(quote_ltp),
                db_close=str(db_close),
            )
            return db_close
        return quote_ltp

    async def _refresh_index_ticker_spots(self) -> bool:
        """REST poll for display-only tickers (e.g. INDIAVIX) — runs even when market is closed."""
        updated = False
        for row in list_index_tickers(self.config):
            sym = str(row.get("symbol", "")).upper()
            token = str(row.get("spot_token", "")).strip()
            if not sym or not token:
                continue
            exchange = str(row.get("exchange", "NSE"))
            try:
                raw = await self.broker.get_quotes(exchange, token)
                quote = quote_from_rest(exchange, token, raw)
                if quote is None or quote.ltp is None:
                    db_close = await self._last_close_from_db(sym)
                    if db_close is not None:
                        await self._apply_index_spot(sym, db_close)
                        updated = True
                    continue
                self._token_to_underlying[token] = sym
                await self._apply_index_spot(sym, quote.ltp)
                updated = True
            except Exception:
                logger.exception("index_ticker_rest_failed", symbol=sym)
        return updated

    async def _refresh_index_spots_rest(self) -> None:
        """Poll each index futures / display ticker LTP for live top-bar quotes."""
        await self._ensure_header_quote_tokens()
        updated = False
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            token = str(row.get("spot_token", "")).strip()
            if not sym or not token:
                continue
            exchange = price_exchange_for(row)
            try:
                raw = await self.broker.get_quotes(exchange, token)
                quote = quote_from_rest(exchange, token, raw)
                if quote is None or quote.ltp is None:
                    db_close = await self._last_close_from_db(sym)
                    if db_close is not None:
                        await self._apply_index_spot(sym, db_close)
                        updated = True
                    continue
                self._token_to_underlying[token] = sym
                spot = await self._coalesce_spot_with_db(
                    sym, quote.ltp, token=token, exchange=exchange
                )
                await self._apply_index_spot(sym, spot)
                updated = True
            except Exception:
                logger.exception("index_spot_rest_failed", symbol=sym)
        if await self._refresh_index_ticker_spots():
            updated = True
        if not updated:
            await self._hydrate_header_spots_from_db()
        self._watchlist_tick.set()

    def _sync_fut_contract_meta(self, sym: str) -> None:
        ul = sym.upper()
        cfg = symbols_for(self.config, ul)
        token = str(cfg.get("spot_token", "")).strip()
        tsym = str(cfg.get("fut_tsym", "")).strip()
        if not token or not tsym:
            self._fut_contract_meta.pop(ul, None)
            return
        label = fut_expiry_label_from_tsym(tsym) or tsym
        self._fut_contract_meta[ul] = {
            "token": token,
            "tsym": tsym,
            "expiry_label": label,
        }

    def _fut_header_snapshot_row(self, sym: str) -> dict[str, Any] | None:
        ul = sym.upper()
        if not uses_futures_price(symbols_for(self.config, ul)):
            return None
        if ul not in self._fut_contract_meta:
            self._sync_fut_contract_meta(ul)
        meta = self._fut_contract_meta.get(ul)
        if not meta:
            return None
        spot_val = self._trading_spot(ul)
        if spot_val is None and ul == self._active_underlying:
            spot_val = self.market_data.spot_ltp
        session_open = self._session_open_for_symbol(ul)
        change: float | None = None
        change_pct: float | None = None
        if spot_val is not None and session_open is not None and session_open != 0:
            chg = spot_val - session_open
            change = float(chg)
            change_pct = float((chg / session_open) * Decimal("100"))
        return {
            "underlying": f"{ul}_FUT",
            "display_name": f"{ul} FUT",
            "card_type": "fut",
            "expiry_label": meta.get("expiry_label"),
            "fut_tsym": meta.get("tsym"),
            "spot_ltp": float(spot_val) if spot_val is not None else None,
            "session_open": float(session_open) if session_open is not None else None,
            "change": change,
            "change_pct": change_pct,
            "atm_strike": None,
            "expiry_symbol": meta.get("expiry_label"),
            "instrument_count": 0,
            "strike_band_points": 0,
            "strike_step": 0,
            "atm_strike_steps": 0,
            "items": [],
            "strike_count": 0,
            "ticker_only": True,
        }

    def _build_header_commodity_rows(
        self,
        *,
        default_step: float,
        default_band: float,
        default_atm_steps: int,
        include_chain: bool,
    ) -> list[dict[str, Any]]:
        commodities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            seen.add(sym)
            uni = self._universes.get(sym)
            commodities.append(
                self._commodity_snapshot_row(
                    sym,
                    uni,
                    default_step=default_step,
                    default_band=default_band,
                    default_atm_steps=default_atm_steps,
                )
                if include_chain
                else self._commodity_summary_row(sym, uni)
            )
            fut_row = self._fut_header_snapshot_row(sym)
            if fut_row:
                commodities.append(fut_row)
        for row in list_index_tickers(self.config):
            sym = str(row.get("symbol", "")).upper()
            if not sym or sym in seen:
                continue
            commodities.append(self._ticker_snapshot_row(row))
        return commodities

    def _commodity_summary_row(
        self, sym: str, uni: ContractUniverse | None
    ) -> dict[str, Any]:
        """Lightweight commodity row for market summary (no option chain)."""
        full = self._commodity_snapshot_row(
            sym,
            uni,
            default_step=float(symbols_for(self.config, sym).get("strike_step", 50)),
            default_band=float(
                strike_band_points(symbols_for(self.config, sym))
            ),
            default_atm_steps=int(
                symbols_for(self.config, sym).get("atm_strike_steps", 5)
            ),
        )
        return {
            k: v
            for k, v in full.items()
            if k
            not in (
                "items",
                "strike_count",
                "instrument_count",
                "strike_band_points",
                "strike_step",
                "atm_strike_steps",
            )
        }

    def _ws_stale(self, max_age_sec: int = 8) -> bool:
        last = self._last_ws_quote_ts
        if last is None:
            return True
        return (datetime.now(tz=timezone.utc) - last).total_seconds() > max_age_sec

    async def _poll_rest_quotes_once(self) -> None:
        updated = 0
        primary = self._active_underlying
        for sym, uni in self._universes.items():
            apply_active_underlying(self.config, sym)
            sym_cfg = symbols_for(self.config, sym)
            px_exchange, px_token = price_context(sym_cfg)
            self._quote_poller.set_spot(px_exchange, px_token)
            updated += await self._quote_poller.poll_universe(uni)
        apply_active_underlying(self.config, primary)
        if self._universe:
            sym_cfg = symbols_for(self.config, primary)
            px_exchange, px_token = price_context(sym_cfg)
            self._quote_poller.set_spot(px_exchange, px_token)
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
        await self._refresh_index_ticker_spots()
        # REST path used to update option_data only — drive holding exits too.
        await self._evaluate_open_exits_from_option_data(source="rest")

    async def _poll_universe_options(
        self,
        sym: str,
        uni: ContractUniverse | None,
        *,
        max_concurrency: int = 10,
    ) -> int:
        if uni is None or not uni.instruments:
            return 0
        sem = asyncio.Semaphore(max(1, max_concurrency))

        async def _one(inst) -> bool:
            async with sem:
                try:
                    raw = await self.broker.get_quotes(inst.exchange, inst.token)
                    quote = quote_from_rest(inst.exchange, inst.token, raw)
                    if quote is None:
                        return False
                    self.option_data.update_from_quote(quote)
                    return True
                except Exception:
                    logger.exception(
                        "option_quote_poll_failed",
                        symbol=sym,
                        token=inst.token,
                        exchange=inst.exchange,
                    )
                    return False

        results = await asyncio.gather(
            *[_one(inst) for inst in uni.instruments],
            return_exceptions=True,
        )
        return sum(1 for r in results if r is True)

    async def _poll_all_universe_option_quotes(self) -> None:
        """REST refresh for option chain when WebSocket is thin."""
        primary = self._active_underlying
        updated = 0
        for sym, uni in self._universes.items():
            apply_active_underlying(self.config, sym)
            updated += await self._poll_universe_options(sym, uni, max_concurrency=12)
        apply_active_underlying(self.config, primary)
        if updated:
            logger.debug("chain_options_rest_polled", updated=updated)

    def _option_ws_stale(self, max_age_sec: float = 3.0) -> bool:
        last = self._last_option_ws_ts
        if last is None:
            return True
        return (datetime.now(tz=timezone.utc) - last).total_seconds() > max_age_sec

    async def _poll_stale_chain_options(self) -> int:
        """REST refresh only for option tokens missing a recent WebSocket tick."""
        if not self._is_market_open():
            return 0
        stale_sec = float(self.config.runtime.get("option_ws_stale_seconds", 1.5))
        max_batch = int(self.config.runtime.get("option_stale_poll_batch", 12))
        now = datetime.now(tz=timezone.utc)
        candidates: list[tuple[float, str, Any]] = []

        for sym, uni in self._universes.items():
            if uni is None or not uni.instruments:
                continue
            for inst in uni.instruments:
                state = self.option_data.get(inst.token)
                age = stale_sec + 1.0
                if state is not None and state.last_update_ts is not None:
                    age = (now - state.last_update_ts).total_seconds()
                if state is None or state.ltp is None or age >= stale_sec:
                    candidates.append((age, sym, inst))

        if not candidates:
            return 0

        candidates.sort(key=lambda row: row[0], reverse=True)
        batch = candidates[:max_batch]
        updated = 0
        for _, sym, inst in batch:
            try:
                raw = await self.broker.get_quotes(inst.exchange, inst.token)
                quote = quote_from_rest(inst.exchange, inst.token, raw)
                if quote is None:
                    continue
                self.option_data.update_from_quote(quote)
                updated += 1
            except Exception:
                logger.exception(
                    "stale_option_quote_poll_failed",
                    symbol=sym,
                    token=inst.token,
                )
        if updated:
            self._watchlist_tick.set()
            logger.debug("stale_chain_options_polled", updated=updated)
        return updated

    async def _maybe_poll_chain_options(self) -> None:
        if not self._is_market_open():
            return
        now = datetime.now(tz=timezone.utc)
        interval = int(self.config.runtime.get("chain_rest_poll_seconds", 5))
        if self._last_chain_options_poll is not None:
            if (now - self._last_chain_options_poll).total_seconds() < interval:
                return
        self._last_chain_options_poll = now
        await self._poll_all_universe_option_quotes()

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
            pos_exchange = exchange
            for uni in self._universes.values():
                match = next(
                    (i for i in uni.instruments if i.token == pos.instrument_token),
                    None,
                )
                if match:
                    pos_exchange = match.exchange
                    break
            try:
                raw = await self.broker.get_quotes(pos_exchange, pos.instrument_token)
            except Exception:
                logger.exception("open_position_quote_failed", token=pos.instrument_token)
                continue
            quote = quote_from_rest(pos_exchange, pos.instrument_token, raw)
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
                await self._refresh_index_spots_rest()
                if self._uses_futures_watchlist():
                    await self._poll_futures_watchlist_quotes()
                # Full chain when WS is down; always keep open holdings ticking for trails.
                if not self._is_market_open() or self._ws_stale():
                    await self._poll_rest_quotes_once()
                else:
                    stale_updated = await self._poll_stale_chain_options()
                    if stale_updated == 0 and self._option_ws_stale(
                        float(self.config.runtime.get("option_ws_stale_seconds", 1.5)) * 2
                    ):
                        await self._maybe_poll_chain_options()
                    if self.orchestrator.positions.open_count > 0:
                        await self._poll_open_position_quotes()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("rest_poll_failed")
            interval = int(self.config.runtime.get("rest_quote_poll_interval_seconds", 30))
            if not self._is_market_open():
                interval = int(
                    self.config.runtime.get("rest_index_poll_interval_seconds", 10)
                )
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
        uni = self._universes.get(self._active_underlying)
        if uni and uni.spot is not None:
            return uni.spot
        db_close = await self._last_close_from_db(self._active_underlying)
        if db_close is not None:
            return db_close
        return fallback_spot(self.config, self._active_underlying)

    @staticmethod
    def _expected_instrument_count(sym_cfg: dict[str, Any]) -> int:
        steps = int(sym_cfg.get("atm_strike_steps", 5))
        return (steps * 2 + 1) * 2

    def _any_universe_undersized(self) -> bool:
        for sym, uni in self._universes.items():
            cfg = symbols_for(self.config, sym)
            if len(uni.instruments) < self._expected_instrument_count(cfg):
                return True
        return False

    async def _rebuild_all_universes(self, *, reason: str) -> None:
        """Rebuild option universes for every configured underlying."""
        primary = self._active_underlying
        pool = get_pool()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            merged = apply_active_underlying(self.config, sym)
            px_exchange, px_token = price_context(merged)
            self.market_data.set_spot_context(exchange=px_exchange, spot_token=px_token)
            self._quote_poller.set_spot(px_exchange, px_token)
            try:
                await self.market_data.backfill_today()
            except Exception:
                logger.exception("candle_backfill_failed", symbol=sym)
            ul_spot = await self._refresh_spot_from_rest()
            if ul_spot is None:
                ul_spot = fallback_spot(self.config, sym)
            else:
                self._index_spot_ltps[sym] = ul_spot
                self._sticky_index_spots[sym] = ul_spot
            universe = await self.contract_selector.build_universe(ul_spot)
            universe.spot = ul_spot
            self._universes[sym] = universe
            if universe.instruments:
                await self.contract_selector.persist_instruments(pool, universe)
                await self._quote_poller.poll_universe(universe)

        apply_active_underlying(self.config, primary)
        self._universe = self._universes.get(primary)
        if self._universe is not None:
            self.orchestrator.set_universe(self._universe)
        if self._ws_started and self._is_market_open():
            await self._stop_subscription()
            await self._start_subscription()
        await self._poll_rest_quotes_once()
        logger.info("all_universes_rebuilt", reason=reason, symbols=list(self._universes.keys()))

    async def _switch_trading_underlying(self, symbol: str) -> None:
        sym = symbol.upper()
        merged = apply_active_underlying(self.config, sym)
        self._active_underlying = sym
        exchange, spot_token = price_context(merged)
        self.market_data.set_spot_context(exchange=exchange, spot_token=spot_token)
        self._quote_poller.set_spot(exchange, spot_token)
        refreshed = await self._refresh_spot_from_rest()
        if refreshed is not None:
            await self._apply_index_spot(sym, refreshed)

        universe = self._universes.get(sym)
        if universe is None or not universe.instruments:
            spot = await self._resolve_spot()
            universe = await self.contract_selector.build_universe(spot)
            universe.spot = spot
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

    async def _maybe_roll_futures(self) -> None:
        """Re-resolve current-month FUT when contract rolls."""
        for row in list_underlyings(self.config):
            if not uses_futures_price(row):
                continue
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            exchange = str(
                row.get("exchange_futures")
                or row.get("exchange_options")
                or row.get("exchange_spot", "NFO")
            )
            result = await resolve_index_future_token(self.broker, sym, exchange)
            if not result:
                continue
            token, tsym = result
            old = str(row.get("spot_token", ""))
            if token == old:
                continue
            row["spot_token"] = token
            row["fut_tsym"] = tsym
            row["exchange_spot"] = exchange
            self._sync_fut_contract_meta(sym)
            logger.info(
                "futures_rolled",
                symbol=sym,
                old_token=old,
                new_token=token,
                tsym=tsym,
            )
            if sym == self._active_underlying:
                apply_active_underlying(self.config, sym)
                self.market_data.set_spot_context(exchange=exchange, spot_token=token)
                self._quote_poller.set_spot(exchange, token)
                await self.market_data.backfill_today()
                if self._ws_started:
                    await self._stop_subscription()
                    await self._start_subscription()

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
            if reason == "new_trading_day":
                await self._maybe_run_log_retention(trigger="new_trading_day")

    async def _maybe_run_log_retention(self, *, trigger: str = "scheduled") -> None:
        """Archive yesterday's logs to disk and purge from Postgres (once per IST day)."""
        from zoneinfo import ZoneInfo

        cfg = self.config.runtime.get("log_retention") or {}
        if not cfg.get("enabled", True):
            return

        ist = ZoneInfo("Asia/Kolkata")
        now = datetime.now(ist)
        today = now.date()
        if self._last_log_maintenance_date == today:
            return

        run_at_raw = str(cfg.get("run_at_ist", "15:35"))
        try:
            hour_s, minute_s = run_at_raw.split(":", 1)
            run_at = datetime.combine(today, time(int(hour_s), int(minute_s)), tzinfo=ist)
        except ValueError:
            run_at = datetime.combine(today, time(15, 35), tzinfo=ist)

        if trigger != "new_trading_day" and now < run_at:
            return

        from algomcx.journal.retention import run_log_retention

        try:
            stats = await run_log_retention(cfg)
            self._last_log_maintenance_date = today
            logger.info("daily_log_retention_done", trigger=trigger, stats=stats)
        except Exception:
            logger.exception("daily_log_retention_failed", trigger=trigger)

    async def _run_daily_log_maintenance_loop(self) -> None:
        """Check after market close whether today's log purge has run."""
        while True:
            try:
                await self._maybe_run_log_retention(trigger="scheduled")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("log_maintenance_loop_failed")
            await asyncio.sleep(300)

    async def _run_market_feed_loop(self) -> None:
        """Keep Flattrade WebSocket running during market hours for live option ticks."""
        while True:
            try:
                await self._maybe_roll_futures()
                if not self._uses_futures_watchlist():
                    await self._maybe_roll_universe()
                has_feed = bool(self._universe and self._universe.instruments) or (
                    self._uses_futures_watchlist() and self._futures_watchlist_items
                )
                if self._is_market_open() and has_feed:
                    if (
                        not self._uses_futures_watchlist()
                        and self._universe
                        and not self._universe.instruments
                    ):
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
        sym = self._active_underlying
        merged = symbols_for(self.config, sym)
        exchange, token = price_context(merged)
        raw = await self.broker.get_quotes(exchange, token)
        quote = quote_from_rest(exchange, token, raw)
        if quote and quote.ltp is not None:
            spot = await self._coalesce_spot_with_db(sym, quote.ltp, token=token, exchange=exchange)
            trusted = self.market_data._spot_quote_trusted(spot)
            if trusted:
                await self.market_data.on_quote(
                    quote.model_copy(update={"ltp": spot})
                )
                return spot
            ref = self.market_data._reference_spot_from_candles()
            if ref is not None:
                return ref
            return spot
        return None

    async def _setup_market_data(self) -> None:
        for field, available in self.option_data.probe_greek_availability().items():
            await self.journal.log_field_availability(field, "websocket", available)

        try:
            await resolve_all_spot_tokens(self.config, self.broker)
            await resolve_index_ticker_tokens(self.config, self.broker)
        except Exception:
            logger.exception("spot_token_resolution_failed")
        if self._uses_futures_watchlist():
            await self._refresh_futures_watchlist()
        self._register_index_spot_tokens()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if sym:
                self._sync_fut_contract_meta(sym)

        primary = str(self.config.symbols.get("underlying", "GOLD")).upper()
        merged = apply_active_underlying(self.config, primary)
        px_exchange, px_token = price_context(merged)
        self.market_data.set_spot_context(exchange=px_exchange, spot_token=px_token)
        self._quote_poller.set_spot(px_exchange, px_token)

        await self._maybe_auto_fetch_fresh_setup_data()

        await self.market_data.backfill_today()
        for interval_candles in self.market_data._candles.values():
            await self.journal.write_candles(interval_candles)

        pool = get_pool()
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            merged = apply_active_underlying(self.config, sym)
            px_exchange, px_token = price_context(merged)
            self.market_data.set_spot_context(exchange=px_exchange, spot_token=px_token)
            self._quote_poller.set_spot(px_exchange, px_token)
            try:
                await self.market_data.backfill_today()
            except Exception:
                logger.exception("candle_backfill_failed", symbol=sym)
            self.market_data.seed_spot_from_candles()
            ul_spot = await self._refresh_spot_from_rest()
            if ul_spot is None:
                await asyncio.sleep(0.3)
                ul_spot = await self._refresh_spot_from_rest()
            if ul_spot is not None and px_token:
                ul_spot = await self._coalesce_spot_with_db(
                    sym, ul_spot, token=px_token, exchange=px_exchange
                )
            if ul_spot is None and self.market_data.spot_ltp is not None:
                ul_spot = self.market_data.spot_ltp
            if ul_spot is None:
                db_close = await self._last_close_from_db(sym)
                if db_close is not None:
                    ul_spot = db_close
            if ul_spot is None or not is_sane_nifty_spot(ul_spot):
                ul_spot = fallback_spot(self.config, sym)
            else:
                self._index_spot_ltps[sym] = ul_spot
                self._sticky_index_spots[sym] = ul_spot
            universe = await self.contract_selector.build_universe(ul_spot)
            universe.spot = ul_spot
            self._universes[sym] = universe
            if universe.instruments:
                await self.contract_selector.persist_instruments(pool, universe)
                await self._quote_poller.poll_universe(universe)

        apply_active_underlying(self.config, primary)
        self._universe = self._universes.get(primary)
        if self._universe is None:
            spot = await self._resolve_spot()
            self._universe = await self.contract_selector.build_universe(spot)
            self._universes[primary] = self._universe
        if self._universe:
            self.orchestrator.set_universe(self._universe)
        self.orchestrator.set_underlying_switcher(self._switch_trading_underlying)
        from zoneinfo import ZoneInfo

        self._last_universe_refresh_date = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        await self._poll_rest_quotes_once()
        await self._refresh_index_spots_rest()

        set_engine_state(
            {
                "status": "running",
                "instrument_count": len(self._futures_watchlist_items)
                if self._uses_futures_watchlist()
                else len(self._universe.instruments)
                if self._universe
                else 0,
                "spot_ltp": str(self.market_data.spot_ltp or (self._universe.spot if self._universe else "")),
                "atm_strike": str(self._universe.atm_strike) if self._universe else None,
                "expiry_symbol": self._universe.expiry_symbol if self._universe else None,
                "feed_mode": self._feed_mode,
            }
        )

        await self.orchestrator.initialize()
        scan_task = asyncio.create_task(self.orchestrator.run_periodic_scan())
        poll_task = asyncio.create_task(self._run_rest_poll_loop())
        feed_task = asyncio.create_task(self._run_market_feed_loop())
        maintenance_task = asyncio.create_task(self._run_daily_log_maintenance_loop())
        self._tasks.extend([scan_task, poll_task, feed_task, maintenance_task])

        inst_count = (
            len(self._futures_watchlist_items)
            if self._uses_futures_watchlist()
            else len(self._universe.instruments)
            if self._universe
            else 0
        )
        logger.info("trading_engine_ready", instruments=inst_count)

    async def _ensure_session(self) -> bool:
        if not await self._has_valid_session():
            cfg = await load_flattrade_config()
            if cfg.password and cfg.totp_secret:
                try:
                    from algomcx.broker.auth import login_and_save

                    await login_and_save(cfg)
                    logger.info("flattrade_auto_login_on_startup")
                except Exception:
                    logger.exception("flattrade_auto_login_failed")

            if not await self._has_valid_session():
                logger.warning(
                    "flattrade_login_required",
                    hint="Configure credentials in Settings or run flattrade_auto_login.py",
                )
                await self.journal.write_notification(
                    "system",
                    "warning",
                    "Flattrade login required",
                    "Open Settings → Flattrade and save credentials, then Re-authenticate.",
                )
                set_engine_state({"status": "standby"})
                return False
        return True

    async def reauthenticate(self, *, force: bool = True) -> dict[str, Any]:
        from algomcx.broker.credentials import invalidate_flattrade_config_cache

        invalidate_flattrade_config_cache()
        cfg = await load_flattrade_config(force=True)

        if not cfg.has_api_credentials():
            raise RuntimeError(
                "Flattrade API credentials are not configured. "
                "Open Settings → Flattrade and save your API key and secret."
            )
        if not cfg.has_auto_login():
            raise RuntimeError(
                "Stored credentials are missing password or TOTP for auto login. "
                "Open Settings → Flattrade, save password + TOTP secret, then Re-authenticate."
            )

        from algomcx.broker.auth import login_and_save

        session = await login_and_save(cfg, force=force)
        was_subscribed = self._ws_started
        if was_subscribed:
            await self._stop_subscription()

        await self.broker.connect()
        set_engine_state({"broker_connected": True, "status": "running"})

        if self._universe is None:
            await self._setup_market_data()
        else:
            await self._refresh_index_spots_rest()
            if self._is_market_open():
                await self._start_subscription()

        await self.journal.write_notification(
            "system",
            "info",
            "Flattrade session refreshed",
            (
                f"User {session.user_id} · valid until "
                f"{session.expires_at.astimezone().strftime('%d %b %H:%M %Z')}"
            ),
        )

        return {
            "ok": True,
            "user_id": session.user_id,
            "expires_at": session.expires_at.isoformat(),
            "valid": session.is_valid,
            "broker_connected": True,
        }

    async def _refresh_futures_watchlist(self) -> None:
        from algomcx.contract_selector.mcx_futures_watchlist import resolve_mcx_futures_watchlist

        exchange = str(self.config.symbols.get("exchange_spot", "MCX"))
        try:
            rows = await resolve_mcx_futures_watchlist(
                self.broker,
                exchange=exchange,
                config=self.config.symbols,
            )
        except Exception:
            logger.exception("futures_watchlist_refresh_failed")
            return
        self._futures_watchlist_items = rows
        self._futures_watchlist_tokens = {
            str(r.get("token", "")) for r in rows if r.get("token")
        }
        await self._poll_futures_watchlist_quotes()

    def _hydrate_futures_watchlist_items(self) -> list[dict[str, Any]]:
        if not self._futures_watchlist_items:
            return []
        out: list[dict[str, Any]] = []
        for row in self._futures_watchlist_items:
            token = str(row.get("token", ""))
            state = self.option_data.get(token)
            next_row = dict(row)
            if state is None:
                out.append(next_row)
                continue
            if state.ltp is not None:
                next_row["ltp"] = float(state.ltp)
            if state.bid is not None:
                next_row["bid"] = float(state.bid)
            if state.ask is not None:
                next_row["ask"] = float(state.ask)
            if state.volume is not None:
                next_row["volume"] = state.volume
            if state.oi is not None:
                next_row["oi"] = state.oi
            if state.last_update_ts is not None:
                next_row["last_update_ts"] = state.last_update_ts.isoformat()
            out.append(next_row)
        return out

    async def _poll_futures_watchlist_quotes(self) -> int:
        if not self._futures_watchlist_items:
            return 0
        exchange = str(self.config.symbols.get("exchange_spot", "MCX"))
        updated = 0
        for row in self._futures_watchlist_items:
            token = str(row.get("token", "")).strip()
            if not token:
                continue
            try:
                raw = await self.broker.get_quotes(exchange, token)
                quote = quote_from_rest(exchange, token, raw)
                if quote is None:
                    continue
                if quote.tsym:
                    quote = quote.model_copy(update={"tsym": str(row.get("tsym", quote.tsym))})
                self.option_data.update_from_quote(quote)
                updated += 1
            except Exception:
                logger.exception("futures_watchlist_quote_failed", token=token)
        if updated:
            self._watchlist_tick.set()
        return updated

    def _futures_items_for_group(self, sym: str) -> list[dict[str, Any]]:
        ul = sym.upper()
        group_by_underlying = {
            "GOLD": "Gold",
            "SILVER": "Silver",
            "NATURALGAS": "Energy",
            "CRUDEOIL": "Energy",
            "COPPER": "Metals",
            "ZINC": "Metals",
            "ALUMINIUM": "Metals",
            "LEAD": "Metals",
            "NICKEL": "Metals",
        }
        group = group_by_underlying.get(ul)
        rows = self._hydrate_futures_watchlist_items()
        if group:
            return [
                row
                for row in rows
                if str(row.get("segment_group", "")).lower() == group.lower()
            ]
        return [row for row in rows if str(row.get("segment_key", "")).upper() == ul]

    def _build_chain_items(
        self,
        universe: ContractUniverse,
        *,
        step: float,
        band: float,
        spot: Decimal | None,
        with_greeks: bool = True,
        atm_steps: int = 5,
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
        max_span = step_d * Decimal(str(int(atm_steps)))

        for inst in sorted(
            universe.instruments,
            key=lambda i: (i.strike, 0 if i.option_type == "CE" else 1),
        ):
            strike_d = inst.strike
            diff = abs(strike_d - atm_d)
            if step_d > 0 and (diff % step_d) != 0:
                continue
            if diff > max_span:
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

    def _hydrate_chain_quotes(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Merge latest option_data LTP/OI into chain rows."""
        if not items:
            return items
        out: list[dict[str, Any]] = []
        for row in items:
            token = str(row.get("token", ""))
            state = self.option_data.get(token)
            if state is None:
                out.append(row)
                continue
            next_row = dict(row)
            if state.ltp is not None:
                next_row["ltp"] = float(state.ltp)
            if state.bid is not None:
                next_row["bid"] = float(state.bid)
            if state.ask is not None:
                next_row["ask"] = float(state.ask)
            if state.volume is not None:
                next_row["volume"] = state.volume
            if state.oi is not None:
                next_row["oi"] = state.oi
            if state.last_update_ts is not None:
                next_row["last_update_ts"] = state.last_update_ts.isoformat()
            out.append(next_row)
        return out

    @staticmethod
    def _chain_strike_count(items: list[dict[str, Any]]) -> int:
        return len({i.get("strike") for i in items if i.get("strike") is not None})

    def _merge_chain_items(
        self,
        base: list[dict[str, Any]],
        fresh: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Keep full strike structure; overlay latest quotes from a partial rebuild."""
        if not base:
            return list(fresh)
        if not fresh:
            return list(base)
        by_token: dict[str, dict[str, Any]] = {
            str(row["token"]): dict(row) for row in base if row.get("token")
        }
        for row in fresh:
            token = str(row.get("token", ""))
            if not token:
                continue
            if token in by_token:
                merged = dict(by_token[token])
                for key, value in row.items():
                    if value is not None:
                        merged[key] = value
                by_token[token] = merged
            else:
                by_token[token] = dict(row)
        return sorted(
            by_token.values(),
            key=lambda i: (float(i.get("strike", 0)), 0 if i.get("option_type") == "CE" else 1),
        )

    def _stabilize_chain_items(
        self,
        sym: str,
        built: list[dict[str, Any]],
        *,
        atm_steps: int,
    ) -> list[dict[str, Any]]:
        """Never replace a full ATM±band chain with a thin partial snapshot."""
        min_strikes = int(atm_steps) * 2 + 1
        cached = self._chain_item_cache.get(sym, [])
        built_strikes = self._chain_strike_count(built)
        cached_strikes = self._chain_strike_count(cached)

        if built_strikes >= min_strikes:
            self._chain_item_cache[sym] = built
            return built

        if built_strikes >= cached_strikes and built:
            self._chain_item_cache[sym] = built
            return built

        if cached:
            merged = self._merge_chain_items(cached, built)
            if self._chain_strike_count(merged) >= cached_strikes:
                self._chain_item_cache[sym] = merged
            return merged

        if built:
            self._chain_item_cache[sym] = built
        return built

    def _lot_size_for_position(self, instrument_token: str, tsym: str) -> int:
        for uni in self._universes.values():
            match = next((i for i in uni.instruments if i.token == instrument_token), None)
            if match:
                return int(match.lot_size)
        if self._universe:
            match = next(
                (i for i in self._universe.instruments if i.token == instrument_token),
                None,
            )
            if match:
                return int(match.lot_size)
        return NIFTY_LOT_SIZE

    def _capture_session_open(self, sym: str, spot: Decimal) -> None:
        if sym.upper() not in self._ticker_only_symbols:
            return
        from zoneinfo import ZoneInfo

        ul = sym.upper()
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        by_day = self._session_open_by_symbol.setdefault(ul, {})
        if today not in by_day:
            by_day[today] = spot

    def _session_open_for_symbol(self, sym: str) -> Decimal | None:
        """Today's session open from cached 1m bars (09:15 IST first bar) or first tick."""
        from zoneinfo import ZoneInfo

        ul = sym.upper()
        today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
        cached = self._session_open_by_symbol.get(ul, {}).get(today)
        if cached is not None:
            return cached
        merged = symbols_for(self.config, ul)
        _, token = price_context(merged)
        if ul == self._active_underlying:
            m1 = self.market_data.candles(CandleInterval.M1)
        elif token:
            m1 = self.market_data.candles_for_token(token, CandleInterval.M1)
        else:
            m1 = []
        if m1:
            open_px = m1[0].open
            self._session_open_by_symbol.setdefault(ul, {})[today] = open_px
            return open_px
        return None

    def _commodity_snapshot_row(
        self,
        sym: str,
        uni: ContractUniverse | None,
        *,
        default_step: float,
        default_band: float,
        default_atm_steps: int,
    ) -> dict[str, Any]:
        cfg = symbols_for(self.config, sym)
        c_step = float(cfg.get("strike_step", default_step))
        c_band = float(strike_band_points(cfg))
        c_steps = int(cfg.get("atm_strike_steps", default_atm_steps))
        spot_val = self._trading_spot(sym, uni)
        if self._uses_futures_watchlist():
            chain_items = self._futures_items_for_group(sym)
            session_open = self._session_open_for_symbol(sym)
            change: float | None = None
            change_pct: float | None = None
            if spot_val is not None and session_open is not None and session_open != 0:
                chg = spot_val - session_open
                change = float(chg)
                change_pct = float((chg / session_open) * Decimal("100"))
            return {
                "underlying": sym,
                "display_name": str(cfg.get("display_name", sym)),
                "spot_ltp": float(spot_val) if spot_val is not None else None,
                "trading_spot_ltp": float(spot_val) if spot_val is not None else None,
                "session_open": float(session_open) if session_open is not None else None,
                "change": change,
                "change_pct": change_pct,
                "atm_strike": None,
                "expiry_symbol": None,
                "instrument_count": len(chain_items),
                "strike_band_points": 0,
                "strike_step": 0,
                "atm_strike_steps": 0,
                "items": chain_items,
                "strike_count": len(chain_items),
            }
        display_uni = uni
        if uni is not None and spot_val is not None:
            display_uni = self.contract_selector.retarget_atm(
                uni, spot_val, strike_step=c_step
            )
            if display_uni.atm_strike != uni.atm_strike:
                self._chain_item_cache.pop(sym.upper(), None)
        chain_items: list[dict[str, Any]] = []
        if display_uni and display_uni.instruments:
            built = self._build_chain_items(
                display_uni,
                step=c_step,
                band=c_band,
                spot=spot_val,
                atm_steps=c_steps,
            )
            chain_items = self._stabilize_chain_items(sym, built, atm_steps=c_steps)
        elif sym in self._chain_item_cache:
            chain_items = list(self._chain_item_cache[sym])
        chain_items = self._hydrate_chain_quotes(chain_items)
        session_open = self._session_open_for_symbol(sym)
        change: float | None = None
        change_pct: float | None = None
        if spot_val is not None and session_open is not None and session_open != 0:
            chg = spot_val - session_open
            change = float(chg)
            change_pct = float((chg / session_open) * Decimal("100"))
        return {
            "underlying": sym,
            "display_name": str(cfg.get("display_name", sym)),
            "spot_ltp": float(spot_val) if spot_val is not None else None,
            "trading_spot_ltp": float(spot_val) if spot_val is not None else None,
            "session_open": float(session_open) if session_open is not None else None,
            "change": change,
            "change_pct": change_pct,
            "atm_strike": float(display_uni.atm_strike)
            if display_uni and display_uni.atm_strike is not None
            else None,
            "expiry_symbol": uni.expiry_symbol if uni else None,
            "instrument_count": len(uni.instruments) if uni else 0,
            "strike_band_points": c_band,
            "strike_step": c_step,
            "atm_strike_steps": c_steps,
            "items": chain_items,
            "strike_count": len({i["strike"] for i in chain_items}),
        }

    def _ticker_snapshot_row(self, row: dict[str, Any]) -> dict[str, Any]:
        sym = str(row.get("symbol", "")).upper()
        spot_val = self._header_spot(sym)
        session_open = self._session_open_for_symbol(sym)
        change: float | None = None
        change_pct: float | None = None
        if spot_val is not None and session_open is not None and session_open != 0:
            chg = spot_val - session_open
            change = float(chg)
            change_pct = float((chg / session_open) * Decimal("100"))
        return {
            "underlying": sym,
            "display_name": str(row.get("display_name", sym)),
            "spot_ltp": float(spot_val) if spot_val is not None else None,
            "session_open": float(session_open) if session_open is not None else None,
            "change": change,
            "change_pct": change_pct,
            "atm_strike": None,
            "expiry_symbol": None,
            "instrument_count": 0,
            "strike_band_points": 0,
            "strike_step": 0,
            "atm_strike_steps": 0,
            "items": [],
            "strike_count": 0,
            "ticker_only": True,
        }

    def get_watchlist_snapshot(self) -> dict[str, Any]:
        underlying = self._active_underlying
        sym_cfg = symbols_for(self.config, underlying)
        step = float(sym_cfg.get("strike_step", 50))
        band = float(strike_band_points(sym_cfg))
        atm_steps = int(sym_cfg.get("atm_strike_steps", 5))

        commodities = self._build_header_commodity_rows(
            default_step=step,
            default_band=band,
            default_atm_steps=atm_steps,
            include_chain=True,
        )
        seen = {c["underlying"] for c in commodities}
        for sym, uni in self._universes.items():
            if sym in seen:
                continue
            commodities.append(
                self._commodity_snapshot_row(
                    sym,
                    uni,
                    default_step=step,
                    default_band=band,
                    default_atm_steps=atm_steps,
                )
            )

        active = next((c for c in commodities if c["underlying"] == underlying), None)
        if active is None and commodities:
            active = commodities[0]
        if self._uses_futures_watchlist():
            items = self._hydrate_futures_watchlist_items()
        else:
            items = active["items"] if active else []
        spot = (
            active.get("trading_spot_ltp")
            if active
            else None
        )
        if spot is None and active:
            spot = active.get("spot_ltp")
        atm_strike = active["atm_strike"] if active else None
        expiry_symbol = active["expiry_symbol"] if active else None

        open_positions = []
        for p in self.orchestrator.positions.open_positions:
            lot_size = self._lot_size_for_position(p.instrument_token, p.tsym)
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

        if spot is not None:
            self._sync_engine_spot_state(Decimal(str(spot)))
            set_engine_state(
                {
                    "instrument_count": len(items)
                    or len(self._universe.instruments)
                    if self._universe
                    else 0,
                    "atm_strike": str(atm_strike) if atm_strike is not None else None,
                }
            )

        return {
            "underlying": underlying,
            "active_underlying": self._active_underlying,
            "watchlist_mode": "futures" if self._uses_futures_watchlist() else "options",
            "commodities": commodities,
            "spot_ltp": spot,
            "atm_strike": atm_strike,
            "expiry_symbol": expiry_symbol,
            "instrument_count": len(items),
            "strike_count": len(items) if self._uses_futures_watchlist() else len({i["strike"] for i in items}),
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

    async def get_underlying_chart_bars(
        self,
        underlying: str,
        *,
        minutes: int = 15,
        days: int = 30,
    ) -> dict[str, Any]:
        """OHLC bars for dashboard chart (1m / 3m / 5m / 15m) with DB history."""
        from algomcx.contract_selector.scripmaster import futures_tokens_for_underlying
        from algomcx.features.indicators import aggregate_from_m5
        from algomcx.market_data.engine import session_start_utc

        sym = underlying.upper()
        merged = apply_active_underlying(self.config, sym)
        exchange, token = price_context(merged)
        if not token:
            return {"underlying": sym, "interval": f"{minutes}m", "bars": []}

        chart_tokens = list(
            dict.fromkeys(
                [
                    token,
                    *futures_tokens_for_underlying(underlying=sym, exchange=exchange),
                    *(
                        tok
                        for tok, ul in self._token_to_underlying.items()
                        if ul == sym
                    ),
                    *self.market_data._candles_by_token.keys(),
                ]
            )
        )

        interval_map = {
            1: (CandleInterval.M1, "1m"),
            3: (CandleInterval.M3, "3m"),
            5: (CandleInterval.M5, "5m"),
        }
        if minutes == 15:
            source_interval = CandleInterval.M5
            interval_label = "15m"
        else:
            source_interval, interval_label = interval_map.get(
                minutes, (CandleInterval.M5, "15m")
            )

        now = datetime.now(tz=timezone.utc)
        history_start = now - timedelta(days=max(1, days))
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                chart_tokens = await self.market_data.chart_tokens_from_db(
                    conn,
                    seed_tokens=chart_tokens,
                    start=history_start,
                    ref_token=token,
                )
        except Exception:
            logger.exception("chart_token_discovery_failed", underlying=sym)

        db_bars: list[Candle] = []
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                db_bars = await self.market_data.candles_from_db_for_tokens(
                    conn, chart_tokens, source_interval, history_start, now
                )
        except Exception:
            logger.exception("chart_candle_db_load_failed", underlying=sym)

        if db_bars:
            by_token: dict[str, list[Candle]] = {}
            for candle in db_bars:
                by_token.setdefault(candle.instrument_token, []).append(candle)
            db_bars = self.market_data.stitch_contract_candles(by_token)

        cached: list[Candle] = []
        if sym == self._active_underlying:
            cached = self.market_data.candles(source_interval)
        elif token:
            cached = self.market_data.candles_for_token(token, source_interval)

        bars_source = self.market_data.merge_candles(db_bars, cached)
        session_start = session_start_utc(now)
        has_today = bool(bars_source and bars_source[-1].ts >= session_start)
        if not has_today or self._is_market_open():
            try:
                broker_bars = await self.broker.get_candles(
                    exchange,
                    token,
                    source_interval,
                    session_start,
                    now,
                )
                bars_source = self.market_data.merge_candles(bars_source, broker_bars)
            except Exception:
                logger.exception("chart_candle_fetch_failed", underlying=sym)

        if minutes == 15:
            m5 = bars_source if source_interval == CandleInterval.M5 else []
            if not m5:
                try:
                    pool = get_pool()
                    async with pool.acquire() as conn:
                        m5 = await self.market_data.candles_from_db_for_tokens(
                            conn, chart_tokens, CandleInterval.M5, history_start, now
                        )
                except Exception:
                    m5 = []
                if sym == self._active_underlying:
                    m5 = self.market_data.merge_candles(
                        m5, self.market_data.candles(CandleInterval.M5)
                    )
                elif token:
                    m5 = self.market_data.merge_candles(
                        m5,
                        self.market_data.candles_for_token(token, CandleInterval.M5),
                    )
                if not m5:
                    try:
                        m5 = await self.broker.get_candles(
                            exchange,
                            token,
                            CandleInterval.M5,
                            session_start_utc(now),
                            now,
                        )
                    except Exception:
                        m5 = []
            bars = aggregate_from_m5(m5, 15)
        else:
            bars = bars_source

        live_spot = self._index_spot_ltps.get(sym)
        session_start = session_start_utc(now)
        if live_spot is not None and bars and self._is_market_open():
            last = bars[-1]
            if last.ts >= session_start:
                px = Decimal(str(live_spot))
                bars = [
                    *bars[:-1],
                    last.model_copy(
                        update={
                            "high": max(last.high, px),
                            "low": min(last.low, px),
                            "close": px,
                        }
                    ),
                ]

        fut_tsym = str(merged.get("fut_tsym") or "")
        return {
            "underlying": sym,
            "interval": interval_label,
            "price_source": "futures",
            "instrument_token": token,
            "fut_tsym": fut_tsym or None,
            "bars": [
                {
                    "ts": c.ts.isoformat(),
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": c.volume,
                }
                for c in bars
            ],
        }

    async def get_contract_chart_bars(
        self,
        *,
        token: str,
        exchange: str = "MCX",
        tsym: str = "",
        minutes: int = 15,
        days: int = 7,
    ) -> dict[str, Any]:
        """OHLC bars for a specific MCX futures contract (watchlist row)."""
        from algomcx.features.indicators import aggregate_from_m5
        from algomcx.market_data.engine import session_start_utc

        tok = str(token).strip()
        if not tok:
            return {"underlying": tsym or "", "interval": f"{minutes}m", "bars": []}

        ex = (exchange or "MCX").upper()
        interval_map = {
            1: (CandleInterval.M1, "1m"),
            3: (CandleInterval.M3, "3m"),
            5: (CandleInterval.M5, "5m"),
        }
        if minutes == 15:
            source_interval = CandleInterval.M5
            interval_label = "15m"
        else:
            source_interval, interval_label = interval_map.get(
                minutes, (CandleInterval.M5, "15m")
            )

        now = datetime.now(tz=timezone.utc)
        history_start = now - timedelta(days=max(1, days))
        chart_tokens = [tok]
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                chart_tokens = await self.market_data.chart_tokens_from_db(
                    conn,
                    seed_tokens=chart_tokens,
                    start=history_start,
                    ref_token=tok,
                )
        except Exception:
            logger.exception("contract_chart_token_discovery_failed", token=tok)

        db_bars: list[Candle] = []
        try:
            pool = get_pool()
            async with pool.acquire() as conn:
                db_bars = await self.market_data.candles_from_db_for_tokens(
                    conn, chart_tokens, source_interval, history_start, now
                )
        except Exception:
            logger.exception("contract_chart_db_load_failed", token=tok)

        cached = self.market_data.candles_for_token(tok, source_interval)
        bars_source = self.market_data.merge_candles(db_bars, cached)

        session_start = session_start_utc(now)
        if not bars_source or self._is_market_open():
            try:
                broker_bars = await self.broker.get_candles(
                    ex,
                    tok,
                    source_interval,
                    history_start,
                    now,
                )
                bars_source = self.market_data.merge_candles(bars_source, broker_bars)
            except Exception:
                logger.exception("contract_chart_broker_fetch_failed", token=tok)

        if minutes == 15:
            m5 = bars_source if source_interval == CandleInterval.M5 else []
            if not m5:
                try:
                    pool = get_pool()
                    async with pool.acquire() as conn:
                        m5 = await self.market_data.candles_from_db_for_tokens(
                            conn, chart_tokens, CandleInterval.M5, history_start, now
                        )
                except Exception:
                    m5 = []
                m5 = self.market_data.merge_candles(
                    m5, self.market_data.candles_for_token(tok, CandleInterval.M5)
                )
                if not m5:
                    try:
                        m5 = await self.broker.get_candles(
                            ex,
                            tok,
                            CandleInterval.M5,
                            history_start,
                            now,
                        )
                    except Exception:
                        m5 = []
            bars = aggregate_from_m5(m5, 15)
        else:
            bars = bars_source

        state = self.option_data.get(tok)
        live_ltp = float(state.ltp) if state and state.ltp is not None else None
        if live_ltp is not None and bars and self._is_market_open():
            last = bars[-1]
            if last.ts >= session_start:
                px = Decimal(str(live_ltp))
                bars = [
                    *bars[:-1],
                    last.model_copy(
                        update={
                            "high": max(last.high, px),
                            "low": min(last.low, px),
                            "close": px,
                        }
                    ),
                ]

        display_tsym = (tsym or (state.tsym if state else "") or "").upper()
        return {
            "underlying": display_tsym or tok,
            "interval": interval_label,
            "price_source": "futures",
            "instrument_token": tok,
            "fut_tsym": display_tsym or None,
            "bars": [
                {
                    "ts": c.ts.isoformat(),
                    "open": float(c.open),
                    "high": float(c.high),
                    "low": float(c.low),
                    "close": float(c.close),
                    "volume": c.volume,
                }
                for c in bars
            ],
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

    async def _discover_history_tokens(self) -> tuple[str | None, list[str]]:
        merged = apply_active_underlying(self.config, self._active_underlying)
        exchange, active_token = price_context(merged)
        history_tokens = [active_token] if active_token else []
        try:
            from algomcx.contract_selector.scripmaster import futures_tokens_for_underlying

            history_tokens = list(
                dict.fromkeys(
                    [
                        *history_tokens,
                        *futures_tokens_for_underlying(
                            underlying=self._active_underlying,
                            exchange=exchange,
                        ),
                    ]
                )
            )
            pool = get_pool()
            async with pool.acquire() as conn:
                history_tokens = await self.market_data.chart_tokens_from_db(
                    conn,
                    seed_tokens=history_tokens,
                    start=datetime.now(tz=timezone.utc) - timedelta(days=30),
                    ref_token=active_token or history_tokens[0],
                )
        except Exception:
            logger.exception("history_token_discovery_failed")
        return exchange, history_tokens

    async def _backfill_chart_history(self, *, days: int = 30) -> dict[str, Any]:
        exchange, history_tokens = await self._discover_history_tokens()
        result: dict[str, Any] = {
            "exchange": exchange,
            "tokens": history_tokens,
            "days": days,
            "added": {},
            "rows": 0,
        }
        if not history_tokens or not exchange:
            return result
        history_added, history_rows = await self.market_data.backfill_contract_history(
            exchange=exchange,
            tokens=history_tokens,
            days=days,
        )
        if history_rows:
            by_interval: dict[CandleInterval, list[Candle]] = {}
            for candle in history_rows:
                by_interval.setdefault(candle.interval, []).append(candle)
            for interval_rows in by_interval.values():
                await self.journal.write_candles(interval_rows)
        result["added"] = history_added
        result["rows"] = len(history_rows)
        return result

    async def _needs_fresh_setup_data(self) -> tuple[bool, str]:
        merged = apply_active_underlying(self.config, self._active_underlying)
        _, active_token = price_context(merged)
        if not active_token:
            return False, "no_price_token"
        pool = get_pool()
        async with pool.acquire() as conn:
            inst_count = int(
                await conn.fetchval("SELECT COUNT(*) FROM instruments WHERE in_band = TRUE")
                or 0
            )
            coverage = await self.market_data.historical_coverage_from_db(
                conn,
                active_token,
                lookback_days=FRESH_SETUP_LOOKBACK_DAYS,
            )
        if inst_count == 0:
            return True, "no_instruments"
        need, reason = needs_fresh_setup_backfill(coverage)
        if need:
            return True, reason
        return False, "ok"

    async def _maybe_auto_fetch_fresh_setup_data(self) -> dict[str, Any] | None:
        """On first run / empty DB, pull last week's candles without manual sync."""
        need, reason = await self._needs_fresh_setup_data()
        if not need:
            return None

        logger.info("fresh_setup_auto_fetch_start", reason=reason)
        await self.journal.write_notification(
            "system",
            "info",
            "Fresh setup — fetching market data",
            (
                f"Missing history ({reason}). "
                f"Pulling last {FRESH_SETUP_LOOKBACK_DAYS} days from Flattrade…"
            ),
        )
        history = await self._backfill_chart_history(days=FRESH_SETUP_LOOKBACK_DAYS)
        await self.market_data.refresh_session_candles(force=True)
        for interval_candles in self.market_data._candles.values():
            await self.journal.write_candles(interval_candles)

        report: dict[str, Any] = {
            "triggered": True,
            "reason": reason,
            "history": history,
        }
        await self.journal.write_system_event(
            SystemEvent(
                event_type="fresh_setup_auto_fetch",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message="Automatic data fetch on fresh setup",
                metadata=report,
            )
        )
        logger.info("fresh_setup_auto_fetch_done", **report)
        return report

    async def sync_missing_data(self) -> dict[str, Any]:
        """Fetch from Flattrade only what DB / in-memory state is missing."""
        from zoneinfo import ZoneInfo

        if not await self._has_valid_session():
            return {
                "ok": False,
                "error": "broker_not_connected",
                "message": "Flattrade session invalid — use Re-authenticate first.",
            }

        await self._maybe_roll_futures()

        band = float(self.config.symbols.get("strike_band_points", 300))
        step = float(self.config.symbols.get("strike_step", 50))
        expected_strikes = int(round((2 * band) / step)) + 1
        expected_inst = expected_strikes * 2
        spot_token = self.config.symbols["spot_token"]
        pool = get_pool()
        market_open = self._is_market_open()

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
            db_candle_stats = await self.market_data.session_candle_stats_from_db(
                conn, spot_token
            )
            m1_before = int(db_candle_stats["1m"]["count"])
            m3_before = int(db_candle_stats["3m"]["count"])
            m5_before = int(db_candle_stats["5m"]["count"])

        need_universe = (
            before_inst < expected_inst
            or self._universe is None
            or not self._universe.instruments
            or self._any_universe_undersized()
        )
        if need_universe:
            await self._rebuild_all_universes(reason="manual_sync_missing")
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

        hydrated: dict[str, int] = {}
        if not self.market_data.candles(CandleInterval.M1) and m1_before > 0:
            async with pool.acquire() as conn:
                hydrated = await self.market_data.hydrate_from_db(conn, spot_token)
            report["candles"]["hydrated_from_db"] = hydrated

        need_candles, candle_reason = self.market_data.needs_broker_candle_sync(
            db_candle_stats,
            market_open=market_open,
        )
        if need_candles:
            await self.market_data.refresh_session_candles(force=True)
            for interval_candles in self.market_data._candles.values():
                await self.journal.write_candles(interval_candles)
            candle_action = "refreshed"
        else:
            candle_action = "ok"

        history_result = await self._backfill_chart_history(days=30)
        history_added = history_result.get("added") or {}
        if history_result.get("rows"):
            candle_action = "refreshed"

        async with pool.acquire() as conn:
            db_after = await self.market_data.session_candle_stats_from_db(conn, spot_token)
            m1_after = int(db_after["1m"]["count"])
            m3_after = int(db_after["3m"]["count"])
            m5_after = int(db_after["5m"]["count"])

        report["candles"] = {
            **report.get("candles", {}),
            "action": candle_action,
            "reason": candle_reason,
            "m1_in_memory": len(self.market_data.candles(CandleInterval.M1)),
            "m3_in_memory": len(self.market_data.candles(CandleInterval.M3)),
            "m5_in_memory": len(self.market_data.candles(CandleInterval.M5)),
            "m1_db_before": m1_before,
            "m3_db_before": m3_before,
            "m5_db_before": m5_before,
            "m1_added": max(0, m1_after - m1_before),
            "m3_added": max(0, m3_after - m3_before),
            "m5_added": max(0, m5_after - m5_before),
            "history_backfill": history_added,
        }

        missing_tokens: list[str] = []
        if self._universe:
            for inst in self._universe.instruments:
                state = self.option_data.get(inst.token)
                if state is None or state.ltp is None:
                    missing_tokens.append(inst.token)

        report["quotes"]["missing_ltp_before"] = len(missing_tokens)
        polled = 0
        quote_action = "ok"
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
            quote_action = "polled_missing"
        elif not self._universe:
            quote_action = "skipped_no_universe"

        still_missing = 0
        if self._universe:
            for inst in self._universe.instruments:
                state = self.option_data.get(inst.token)
                if state is None or state.ltp is None:
                    still_missing += 1
        report["quotes"]["action"] = quote_action
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
        if uses_pooled_capital(self.config):
            total_capital = total_account_capital(self.config)
            reset_rows: list[tuple[str, Decimal]] = [
                (risk_underlying_key(self.config), total_capital)
            ]
        else:
            total_capital = Decimal("0")
            reset_rows = []
            for row in list_underlyings(self.config):
                sym = str(row.get("symbol", "")).upper()
                if not sym:
                    continue
                cap = capital_for(self.config, sym)
                reset_rows.append((sym, cap))
                total_capital += cap
            if not reset_rows:
                cap = total_account_capital(self.config)
                reset_rows = [("GOLD", cap)]
                total_capital = cap

        pool = get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM closed_trades")
                await conn.execute("DELETE FROM positions")
                await conn.execute("DELETE FROM orders")
                await conn.execute("DELETE FROM validation_results")
                await conn.execute("DELETE FROM ml_scores")
                await conn.execute("DELETE FROM candidate_signals")
                await conn.execute(
                    """
                    DELETE FROM daily_risk_state
                    WHERE trade_date <> (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                    """
                )
                if uses_pooled_capital(self.config):
                    await conn.execute(
                        """
                        DELETE FROM daily_risk_state
                        WHERE trade_date = (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date
                          AND underlying <> $1
                        """,
                        risk_underlying_key(self.config),
                    )
                await conn.execute(
                    "DELETE FROM instruments WHERE in_band = FALSE OR expiry_date < CURRENT_DATE"
                )
                for sym, cap in reset_rows:
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
                          AND underlying = $2
                        """,
                        cap,
                        sym,
                    )
                    await conn.execute(
                        """
                        INSERT INTO daily_risk_state (
                            trade_date, underlying, starting_capital, available_capital,
                            deployed_capital, realized_pnl, trade_count, consecutive_losses,
                            kill_switch, entries_blocked
                        ) VALUES (
                            (CURRENT_TIMESTAMP AT TIME ZONE 'Asia/Kolkata')::date,
                            $2, $1, $1, 0, 0, 0, 0, FALSE, FALSE
                        )
                        ON CONFLICT (trade_date, underlying) DO NOTHING
                        """,
                        cap,
                        sym,
                    )

        reset_msg = (
            f"Balance restored to ₹{total_capital:,.0f}"
            + (
                " (shared pool)"
                if uses_pooled_capital(self.config)
                else " across indices"
            )
            + " · old trades removed"
        )
        await self.journal.write_notification(
            "system",
            "info",
            "Paper account reset",
            reset_msg,
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
            "starting_capital": float(total_capital),
            "available_capital": float(total_capital),
            "expiry_symbol": self._universe.expiry_symbol if self._universe else None,
            "instrument_count": sum(len(u.instruments) for u in self._universes.values()),
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
        sym_cfg = symbols_for(self.config, self._active_underlying)
        commodity_rows: list[dict[str, Any]] = []
        if self._universes:
            commodity_rows = self._build_header_commodity_rows(
                default_step=float(sym_cfg.get("strike_step", 50)),
                default_band=float(strike_band_points(sym_cfg)),
                default_atm_steps=int(sym_cfg.get("atm_strike_steps", 5)),
                include_chain=False,
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
            "trading_mode": get_execution_mode(),
            "ist_time": ist.isoformat(),
            "feed_mode": self._feed_mode,
            "expiry_symbol": active_uni.expiry_symbol if active_uni else None,
            "instrument_count": len(active_uni.instruments) if active_uni else 0,
            "m5_count": len(m5),
        }

    async def live_account_limits(self):
        await self._flattrade.connect()
        return await self._flattrade.get_account_limits()

    async def set_execution_mode_runtime(self, mode: str) -> dict[str, Any]:
        normalized = "live" if str(mode).lower() == "live" else "paper"
        if self.orchestrator.positions.open_count > 0:
            raise ValueError("Close all open positions before switching Paper / Live mode")

        if normalized == "live":
            await self._flattrade.connect()
            await self._flattrade.get_account_limits()

        new_mode = await set_execution_mode(normalized)
        await self.journal.write_system_event(
            SystemEvent(
                event_type="execution_mode_changed",
                ts=datetime.now(tz=timezone.utc),
                severity="critical" if new_mode == "live" else "info",
                message=f"Execution mode set to {new_mode.upper()}",
                metadata={"trading_mode": new_mode},
            )
        )
        await self.journal.write_notification(
            "system",
            "critical" if new_mode == "live" else "info",
            f"{'LIVE' if new_mode == 'live' else 'Paper'} trading enabled",
            (
                "Real broker orders will be placed for new entries."
                if new_mode == "live"
                else "Orders are simulated against the paper ledger."
            ),
        )
        return {"ok": True, "trading_mode": new_mode}

    async def start(self) -> None:
        setup_logging(self.config.env.log_level, self.config.logging.get("format", "json"))
        await init_pool()
        await apply_migrations()
        await load_execution_mode_from_db(self.config.env.trading_mode)
        for row in list_underlyings(self.config):
            sym = str(row.get("symbol", "")).upper()
            if sym:
                await ensure_paper_account(
                    float(total_account_capital(self.config)),
                    underlying=risk_underlying_key(self.config),
                )
                break
        else:
            await ensure_paper_account(
                float(total_account_capital(self.config)),
                underlying=risk_underlying_key(self.config),
            )
        set_engine_state({"status": "running", "broker_connected": False})

        await self.journal.write_system_event(
            SystemEvent(
                event_type="SYSTEM_START",
                ts=datetime.now(tz=timezone.utc),
                severity="info",
                message="Trading engine started",
                metadata={"mode": get_execution_mode()},
            )
        )

        if not await self._has_api_credentials():
            logger.warning(
                "broker_api_key_missing",
                hint="Configure Flattrade credentials in Settings",
            )
            await self.journal.write_notification(
                "system",
                "warning",
                "Flattrade API key missing",
                "Open Settings → Flattrade and save your API key and secret from Flattrade Wall → Pi.",
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
    config = uvicorn.Config(health_app, host="0.0.0.0", port=8001, log_level="info")
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
