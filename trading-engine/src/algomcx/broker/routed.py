from __future__ import annotations

from datetime import datetime
from typing import Any

from algomcx.broker.base import BrokerAdapter
from algomcx.broker.flattrade import FlattradeAdapter
from algomcx.broker.paper import PaperBrokerAdapter
from algomcx.models.events import Candle, CandleInterval, ExecutionRequest, OrderUpdate, QuoteUpdate
from algomcx.runtime.trading_mode import is_live_execution


class RoutedBrokerAdapter(BrokerAdapter):
    """Market data via paper-wrapped Flattrade; orders routed by execution mode."""

    def __init__(
        self,
        live: FlattradeAdapter,
        paper: PaperBrokerAdapter,
    ) -> None:
        self._live = live
        self._paper = paper

    @property
    def live_broker(self) -> FlattradeAdapter:
        return self._live

    @property
    def is_connected(self) -> bool:
        return self._paper.is_connected

    @property
    def websocket_open(self) -> bool:
        return self._paper.websocket_open

    async def connect(self) -> None:
        await self._paper.connect()

    async def disconnect(self) -> None:
        await self._paper.disconnect()

    async def stop_websocket(self) -> None:
        await self._paper.stop_websocket()

    async def get_candles(
        self,
        exchange: str,
        token: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        return await self._paper.get_candles(exchange, token, interval, start, end)

    async def get_quotes(self, exchange: str, token: str) -> dict[str, Any]:
        return await self._paper.get_quotes(exchange, token)

    async def search_scrip(self, exchange: str, search_text: str) -> list[dict[str, Any]]:
        return await self._paper.search_scrip(exchange, search_text)

    async def get_option_chain(
        self,
        exchange: str,
        tradingsymbol: str,
        strikeprice: float,
        count: int,
    ) -> list[dict[str, Any]]:
        return await self._paper.get_option_chain(exchange, tradingsymbol, strikeprice, count)

    async def subscribe(
        self,
        instruments: list[str],
        on_quote: Any,
        on_order: Any | None = None,
    ) -> None:
        await self._paper.subscribe(instruments, on_quote, on_order)

    async def place_order(self, request: ExecutionRequest) -> OrderUpdate:
        if is_live_execution():
            return await self._live.place_order(request)
        return await self._paper.place_order(request)
