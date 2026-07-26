from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

import structlog
from NorenRestApiPy.NorenApi import NorenApi

from algomcx.broker.auth import ensure_session, resolve_session
from algomcx.broker.credentials import load_flattrade_config
from algomcx.broker.base import BrokerAdapter
from algomcx.broker.flattrade_ws import FlattradeMarketSocket
from algomcx.config import AppConfig
from algomcx.models.events import Candle, CandleInterval, ExecutionRequest, OrderUpdate, QuoteUpdate, TradingMode

logger = structlog.get_logger(__name__)

_INTERVAL_MAP = {
    CandleInterval.M1: "1",
    CandleInterval.M3: "3",
    CandleInterval.M5: "5",
}


@dataclass(frozen=True)
class AccountLimits:
    cash: Decimal
    available: Decimal
    margin_used: Decimal
    collateral: Decimal

    @property
    def equity(self) -> Decimal:
        return self.cash + self.collateral


def _decimal_field(raw: dict[str, Any], *keys: str) -> Decimal:
    for key in keys:
        val = raw.get(key)
        if val not in (None, "", "0", "0.00"):
            try:
                return Decimal(str(val))
            except Exception:
                continue
    return Decimal("0")


class _FlattradeNorenApi(NorenApi):
    def __init__(self, host: str, websocket: str) -> None:
        NorenApi.__init__(self, host=host, websocket=websocket)


class FlattradeAdapter(BrokerAdapter):
    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._api = _FlattradeNorenApi(
            host=config.broker["api_base_url"],
            websocket=config.broker["websocket_url"],
        )
        self._connected = False
        self._quote_callback: Callable[[QuoteUpdate], None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._session_user_id: str | None = None
        self._access_token: str | None = None
        self._market_socket: FlattradeMarketSocket | None = None
        self._start_lock = asyncio.Lock()
        self._ws_tick_cache: dict[str, dict[str, Any]] = {}
        self._token_exchange: dict[str, str] = {}

    @property
    def session_user_id(self) -> str | None:
        return self._session_user_id

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def websocket_open(self) -> bool:
        return bool(self._market_socket and self._market_socket.is_open)

    async def connect(self) -> None:
        cfg = await load_flattrade_config()
        session = await ensure_session(cfg)
        user_id = session.user_id or cfg.user_id
        if not user_id:
            raise ValueError("FLATTRADE_USER_ID is required (or returned from OAuth client field)")

        self._loop = asyncio.get_running_loop()
        self._session_user_id = user_id
        self._access_token = session.access_token

        def _set_session(token: str) -> Any:
            return self._api.set_session(
                userid=user_id,
                password="",
                usertoken=token,
            )

        result = await asyncio.to_thread(_set_session, session.access_token)
        if isinstance(result, dict) and result.get("stat") == "Not_Ok":
            raise ConnectionError(result.get("emsg", "Flattrade session failed"))

        # Local expiry cache can outlive broker-side invalidation — verify live.
        limits = await asyncio.to_thread(self._api.get_limits)
        emsg = ""
        if isinstance(limits, dict) and limits.get("stat") == "Not_Ok":
            emsg = str(limits.get("emsg", ""))
        if "session" in emsg.lower() or "invalid" in emsg.lower():
            logger.warning("flattrade_session_rejected_by_broker", emsg=emsg)
            from algomcx.broker.auth import login_and_save

            session = await login_and_save(cfg, force=True)
            user_id = session.user_id or user_id
            self._session_user_id = user_id
            self._access_token = session.access_token
            result = await asyncio.to_thread(_set_session, session.access_token)
            if isinstance(result, dict) and result.get("stat") == "Not_Ok":
                raise ConnectionError(result.get("emsg", "Flattrade re-login failed"))
            limits = await asyncio.to_thread(self._api.get_limits)
            if isinstance(limits, dict) and limits.get("stat") == "Not_Ok":
                raise ConnectionError(
                    limits.get("emsg", "Flattrade session still invalid after re-login")
                )

        self._connected = True
        logger.info("flattrade_connected", user_id=user_id)
        logger.info(
            "flattrade_session_ready",
            user_id=user_id,
            token_source=session.source,
            expires_at=session.expires_at.isoformat(),
        )

    async def disconnect(self) -> None:
        await self.stop_websocket()
        self._connected = False

    async def stop_websocket(self) -> None:
        """Stop market WS only — keep REST session for candles/quotes."""
        sock = self._market_socket
        self._market_socket = None
        if sock is not None:
            await asyncio.to_thread(sock.stop)
            logger.info("flattrade_websocket_stopped")

    async def get_candles(
        self,
        exchange: str,
        token: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        intrv = _INTERVAL_MAP[interval]

        def _fetch() -> Any:
            return self._api.get_time_price_series(
                exchange=exchange,
                token=token,
                starttime=str(int(start.timestamp())),
                endtime=str(int(end.timestamp())),
                interval=intrv,
            )

        raw = await asyncio.to_thread(_fetch)
        if not raw:
            return []

        # Noren APIs return {"stat":"Not_Ok","emsg":"..."} on failure — never treat as bars.
        if isinstance(raw, dict):
            logger.warning(
                "flattrade_candles_error",
                exchange=exchange,
                token=token,
                interval=interval.value,
                stat=raw.get("stat"),
                emsg=raw.get("emsg") or raw.get("message"),
            )
            return []

        candles: list[Candle] = []
        for row in raw:
            if not isinstance(row, dict):
                continue
            if "ssboe" not in row:
                continue
            ts = datetime.fromtimestamp(int(row["ssboe"]), tz=timezone.utc)
            candles.append(
                Candle(
                    instrument_token=token,
                    ts=ts,
                    open=Decimal(str(row.get("into", row.get("intc", 0)))),
                    high=Decimal(str(row.get("inth", 0))),
                    low=Decimal(str(row.get("intl", 0))),
                    close=Decimal(str(row.get("intc", 0))),
                    volume=int(float(row.get("intv", 0) or 0)) or None,
                    interval=interval,
                    vwap=(
                        Decimal(str(row["intvwap"]))
                        if row.get("intvwap") not in (None, "", "0", "0.00")
                        else None
                    ),
                    oi=int(float(row["oi"])) if row.get("oi") not in (None, "") else None,
                )
            )
        # Always chronological — features/scanners use bars[-1] as latest.
        candles.sort(key=lambda c: c.ts)
        return candles

    async def get_quotes(self, exchange: str, token: str) -> dict[str, Any]:
        def _fetch() -> Any:
            return self._api.get_quotes(exchange=exchange, token=token)

        result = await asyncio.to_thread(_fetch)
        return result if isinstance(result, dict) else {}

    async def search_scrip(self, exchange: str, search_text: str) -> list[dict[str, Any]]:
        def _fetch() -> Any:
            return self._api.searchscrip(exchange=exchange, searchtext=search_text)

        result = await asyncio.to_thread(_fetch)
        if isinstance(result, dict) and result.get("stat") == "Ok":
            values = result.get("values", [])
            return values if isinstance(values, list) else []
        return []

    async def get_option_chain(
        self,
        exchange: str,
        tradingsymbol: str,
        strikeprice: float,
        count: int,
    ) -> list[dict[str, Any]]:
        def _fetch() -> Any:
            return self._api.get_option_chain(
                exchange=exchange,
                tradingsymbol=tradingsymbol,
                strikeprice=strikeprice,
                count=count,
            )

        result = await asyncio.to_thread(_fetch)
        if isinstance(result, dict) and result.get("stat") == "Ok":
            values = result.get("values", [])
            return values if isinstance(values, list) else []
        if isinstance(result, list):
            return result
        logger.warning(
            "option_chain_failed",
            tradingsymbol=tradingsymbol,
            strikeprice=strikeprice,
            response=result,
        )
        return []

    def _register_subscription_keys(self, instruments: list[str]) -> None:
        for key in instruments:
            if "|" not in key:
                continue
            exchange, token = key.split("|", 1)
            if exchange and token:
                self._token_exchange[token] = exchange

    def _handle_feed_update(self, message: dict[str, Any]) -> None:
        if self._quote_callback is None or self._loop is None:
            return
        try:
            msg_type = str(message.get("t", ""))
            if msg_type not in ("tk", "tf", "dk", "df"):
                return

            token = str(message.get("tk") or message.get("ft") or "").strip()
            if not token:
                return

            exchange = str(message.get("e") or self._token_exchange.get(token, "")).strip()
            cache_key = f"{exchange}|{token}" if exchange else token
            prev = self._ws_tick_cache.get(cache_key, {})
            merged = dict(prev)
            for key, value in message.items():
                if value is None or value == "":
                    continue
                merged[key] = value
            self._ws_tick_cache[cache_key] = merged

            ltp = self.parse_decimal(merged.get("lp"))
            if ltp is None:
                return

            quote = QuoteUpdate(
                ts=datetime.now(tz=timezone.utc),
                exchange=exchange or str(merged.get("e", "")),
                instrument_token=token,
                tsym=merged.get("ts") if isinstance(merged.get("ts"), str) else message.get("ts"),
                ltp=ltp,
                bid=self.parse_decimal(merged.get("bp1")),
                ask=self.parse_decimal(merged.get("sp1")),
                volume=int(merged["v"]) if merged.get("v") not in (None, "") else None,
                oi=int(merged["oi"]) if merged.get("oi") not in (None, "") else None,
                source="websocket",
            )
            self._loop.call_soon_threadsafe(self._quote_callback, quote)
        except Exception:
            logger.exception("feed_update_parse_failed", message=message)

    async def subscribe(
        self,
        instruments: list[str],
        on_quote: Any,
        on_order: Any | None = None,
    ) -> None:
        """Start Flattrade V2 market WebSocket and subscribe to option/spot tokens."""
        del on_order  # order stream not required for paper option-chain ticker
        async with self._start_lock:
            self._quote_callback = on_quote
            instruments = list(dict.fromkeys(instruments))
            self._register_subscription_keys(instruments)

            if self._market_socket and self._market_socket.is_open:
                await asyncio.to_thread(self._market_socket.subscribe, instruments)
                logger.info(
                    "flattrade_websocket_resubscribe",
                    instruments=len(instruments),
                    bfo=sum(1 for k in instruments if k.upper().startswith("BFO|")),
                )
                return

            if self._market_socket is not None:
                await asyncio.to_thread(self._market_socket.stop)
                self._market_socket = None

            # Refresh token in case session was renewed.
            cfg = await load_flattrade_config()
            session = resolve_session(cfg)
            if session is None or not session.is_valid:
                raise ConnectionError("Flattrade session invalid for WebSocket")
            user_id = session.user_id or self._session_user_id or cfg.user_id
            if not user_id:
                raise ValueError("FLATTRADE_USER_ID required for WebSocket")
            self._access_token = session.access_token
            self._session_user_id = user_id

            opened = asyncio.Event()

            def _open_cb() -> None:
                self._loop.call_soon_threadsafe(opened.set)  # type: ignore[union-attr]

            def _err_cb(error: Any) -> None:
                logger.warning("flattrade_market_socket_error", error=str(error))

            sock = FlattradeMarketSocket(
                user_id=user_id,
                access_token=session.access_token,
                actid=user_id,
                on_quote=self._handle_feed_update,
                on_open=_open_cb,
                on_error=_err_cb,
            )
            self._market_socket = sock
            sock.subscribe(instruments)
            await asyncio.to_thread(sock.start)

            try:
                await asyncio.wait_for(opened.wait(), timeout=15)
                bfo_count = sum(1 for k in instruments if k.upper().startswith("BFO|"))
                logger.info(
                    "flattrade_websocket_ready",
                    instruments=len(instruments),
                    bfo_instruments=bfo_count,
                )
            except asyncio.TimeoutError:
                if sock.auth_failed:
                    logger.warning("flattrade_websocket_auth_failed")
                else:
                    logger.warning("flattrade_websocket_open_timeout")

    async def place_order(self, request: ExecutionRequest) -> OrderUpdate:
        if not self._connected:
            raise ConnectionError("Flattrade session not connected")

        started = time.monotonic()
        buy_or_sell = "B" if request.side.upper() == "BUY" else "S"
        order_type = (request.order_type or "MKT").upper()
        price_type = "MKT" if order_type == "MKT" else "LMT"
        price = 0.0
        if price_type == "LMT":
            price = float(request.limit_price or request.reference_ltp or 0)

        def _place() -> Any:
            return self._api.place_order(
                buy_or_sell=buy_or_sell,
                product_type=request.product,
                exchange=request.exchange,
                tradingsymbol=request.tsym,
                quantity=int(request.quantity),
                discloseqty=0,
                price_type=price_type,
                price=price,
                retention="DAY",
                remarks=(request.client_order_id or "")[:20],
            )

        raw = await asyncio.to_thread(_place)
        latency_ms = int((time.monotonic() - started) * 1000)
        now = datetime.now(tz=timezone.utc)

        if not isinstance(raw, dict) or raw.get("stat") != "Ok":
            emsg = raw.get("emsg", "order rejected") if isinstance(raw, dict) else "invalid response"
            logger.warning(
                "live_order_rejected",
                tsym=request.tsym,
                side=request.side,
                emsg=emsg,
                raw=raw,
            )
            return OrderUpdate(
                ts=now,
                client_order_id=request.client_order_id,
                broker_order_id=None,
                status="REJECTED",
                report_type="Reject",
                fill_price=None,
                filled_qty=0,
                avg_price=None,
                slippage=None,
                latency_ms=latency_ms,
                mode=TradingMode.LIVE,
                rejection_reason=str(emsg),
            )

        broker_id = str(raw.get("norenordno") or raw.get("order_id") or "")
        fill_price, filled_qty, status, reject_reason = await self._resolve_order_fill(
            broker_id=broker_id,
            request=request,
            price_type=price_type,
        )

        if status == "REJECTED":
            logger.warning(
                "live_order_fill_timeout",
                tsym=request.tsym,
                broker_order_id=broker_id,
                price_type=price_type,
                reason=reject_reason,
            )
            return OrderUpdate(
                ts=now,
                client_order_id=request.client_order_id,
                broker_order_id=broker_id or None,
                status="REJECTED",
                report_type="Reject",
                fill_price=None,
                filled_qty=0,
                avg_price=None,
                slippage=None,
                latency_ms=latency_ms,
                mode=TradingMode.LIVE,
                rejection_reason=reject_reason,
            )

        slippage = None
        if fill_price is not None:
            slippage = fill_price - request.reference_ltp

        logger.info(
            "live_order_filled",
            tsym=request.tsym,
            broker_order_id=broker_id,
            status=status,
            fill_price=str(fill_price) if fill_price is not None else None,
            qty=filled_qty,
        )

        return OrderUpdate(
            ts=now,
            client_order_id=request.client_order_id,
            broker_order_id=broker_id or None,
            status=status,
            report_type="Fill" if status == "COMPLETE" else "Ack",
            fill_price=fill_price,
            filled_qty=filled_qty,
            avg_price=fill_price,
            slippage=slippage,
            latency_ms=latency_ms,
            mode=TradingMode.LIVE,
        )

    async def _resolve_order_fill(
        self,
        *,
        broker_id: str,
        request: ExecutionRequest,
        price_type: str,
        max_wait_sec: float | None = None,
    ) -> tuple[Decimal | None, int, str, str | None]:
        wait_sec = max_wait_sec if max_wait_sec is not None else (8.0 if price_type == "MKT" else 6.0)
        deadline = time.monotonic() + wait_sec
        while time.monotonic() < deadline:
            row = await self._find_order_row(broker_id)
            if row is not None:
                status = str(row.get("status") or row.get("ordstatus") or "").upper()
                avg = row.get("avgprc") or row.get("prc")
                fill_qty = row.get("fillshares") or row.get("qty") or request.quantity
                if status in ("COMPLETE", "TRADED", "FILLED"):
                    if avg not in (None, "", "0", "0.00"):
                        return Decimal(str(avg)), int(fill_qty), "COMPLETE", None
                if status in ("REJECTED", "CANCELED", "CANCELLED"):
                    return None, 0, "REJECTED", "broker rejected or canceled order"
            await asyncio.sleep(0.4)

        reason = f"{price_type} order not confirmed filled within {wait_sec:.0f}s"
        return None, 0, "REJECTED", reason

    async def _find_order_row(self, broker_id: str) -> dict[str, Any] | None:
        if not broker_id:
            return None

        def _fetch() -> Any:
            return self._api.get_order_book()

        book = await asyncio.to_thread(_fetch)
        if not isinstance(book, list):
            return None
        for row in book:
            if not isinstance(row, dict):
                continue
            if str(row.get("norenordno") or row.get("order_id") or "") == broker_id:
                return row
        return None

    async def get_account_limits(self) -> AccountLimits:
        if not self._connected:
            await self.connect()

        def _fetch() -> Any:
            return self._api.get_limits()

        raw = await asyncio.to_thread(_fetch)
        if not isinstance(raw, dict) or raw.get("stat") == "Not_Ok":
            emsg = raw.get("emsg", "limits unavailable") if isinstance(raw, dict) else "limits unavailable"
            raise ConnectionError(str(emsg))

        cash = _decimal_field(raw, "cash", "availablecash", "availablelimit")
        available = _decimal_field(raw, "marginavailable", "availablemargin", "cash")
        margin_used = _decimal_field(raw, "marginused", "marginusednrml", "marginusedmis")
        collateral = _decimal_field(raw, "collateral", "brkcollamt", "brkcollateral")

        if available <= 0 and cash > 0:
            available = cash
        if margin_used <= 0:
            margin_used = max(Decimal("0"), cash - available)

        return AccountLimits(
            cash=cash,
            available=available,
            margin_used=margin_used,
            collateral=collateral,
        )
