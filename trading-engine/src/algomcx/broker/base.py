from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any

from algomcx.models.events import Candle, CandleInterval, ExecutionRequest, Instrument, OrderUpdate, QuoteUpdate


class BrokerAdapter(ABC):
    @abstractmethod
    async def connect(self) -> None:
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        ...

    @abstractmethod
    async def get_candles(
        self,
        exchange: str,
        token: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime,
    ) -> list[Candle]:
        ...

    @abstractmethod
    async def get_quotes(self, exchange: str, token: str) -> dict[str, Any]:
        ...

    @abstractmethod
    async def get_option_chain(
        self,
        exchange: str,
        tradingsymbol: str,
        strikeprice: float,
        count: int,
    ) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    async def subscribe(
        self,
        instruments: list[str],
        on_quote: Any,
        on_order: Any | None = None,
    ) -> None:
        ...

    @abstractmethod
    async def place_order(self, request: ExecutionRequest) -> OrderUpdate:
        ...

    @property
    @abstractmethod
    def is_connected(self) -> bool:
        ...

    @staticmethod
    def format_instrument(exchange: str, token: str) -> str:
        return f"{exchange}|{token}"

    @staticmethod
    def parse_decimal(value: Any) -> Decimal | None:
        if value is None or value == "":
            return None
        return Decimal(str(value))
