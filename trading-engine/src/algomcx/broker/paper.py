from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog

from algomcx.broker.base import BrokerAdapter
from algomcx.config import AppConfig
from algomcx.models.events import Candle, CandleInterval, ExecutionRequest, OrderUpdate, QuoteUpdate, TradingMode

logger = structlog.get_logger(__name__)


class PaperBrokerAdapter(BrokerAdapter):
    """Uses a real data adapter for quotes; simulates fills at LTP."""

    def __init__(self, config: AppConfig, data_adapter: BrokerAdapter) -> None:
        self._config = config
        self._data = data_adapter
        self._ltp_cache: dict[str, Decimal] = {}

    @property
    def is_connected(self) -> bool:
        return self._data.is_connected

    @property
    def websocket_open(self) -> bool:
        return bool(getattr(self._data, "websocket_open", False))

    async def connect(self) -> None:
        await self._data.connect()

    async def disconnect(self) -> None:
        await self._data.disconnect()

    async def stop_websocket(self) -> None:
        stop = getattr(self._data, "stop_websocket", None)
        if callable(stop):
            await stop()
        else:
            await self._data.disconnect()

    async def get_candles(
        self,
        exchange: str,
        token: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        return await self._data.get_candles(exchange, token, interval, start, end)

    async def get_quotes(self, exchange: str, token: str) -> dict[str, Any]:
        return await self._data.get_quotes(exchange, token)

    async def search_scrip(self, exchange: str, search_text: str) -> list[dict[str, Any]]:
        search = getattr(self._data, "search_scrip", None)
        if search is None:
            return []
        return await search(exchange, search_text)

    async def get_option_chain(
        self,
        exchange: str,
        tradingsymbol: str,
        strikeprice: float,
        count: int,
    ) -> list[dict[str, Any]]:
        return await self._data.get_option_chain(exchange, tradingsymbol, strikeprice, count)

    async def subscribe(
        self,
        instruments: list[str],
        on_quote: Any,
        on_order: Any | None = None,
    ) -> None:
        def _wrapped(quote: QuoteUpdate) -> None:
            if quote.ltp is not None:
                self._ltp_cache[quote.instrument_token] = quote.ltp
            on_quote(quote)

        await self._data.subscribe(instruments, _wrapped, on_order)

    async def place_order(self, request: ExecutionRequest) -> OrderUpdate:
        fill_price = request.reference_ltp
        if request.instrument_token in self._ltp_cache:
            fill_price = self._ltp_cache[request.instrument_token]
        now = datetime.now(tz=timezone.utc)
        slippage = fill_price - request.reference_ltp
        logger.info(
            "paper_order_filled",
            client_order_id=request.client_order_id,
            fill_price=str(fill_price),
            slippage=str(slippage),
        )
        return OrderUpdate(
            ts=now,
            client_order_id=request.client_order_id,
            broker_order_id=f"PAPER-{request.client_order_id[-8:]}",
            status="COMPLETE",
            report_type="Fill",
            fill_price=fill_price,
            filled_qty=request.quantity,
            avg_price=fill_price,
            slippage=slippage,
            latency_ms=self._config.paper_trading.get("simulate_latency_ms", 0),
            mode=TradingMode.PAPER,
        )
