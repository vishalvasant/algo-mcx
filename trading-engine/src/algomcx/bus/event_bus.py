from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import structlog

logger = structlog.get_logger(__name__)

T = TypeVar("T")
EventHandler = Callable[[Any], Awaitable[None]]


class EventBus:
    def __init__(self, max_size: int = 10_000) -> None:
        self._queues: dict[str, asyncio.Queue[Any]] = defaultdict(
            lambda: asyncio.Queue(maxsize=max_size)
        )
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: EventHandler) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event_type: str, event: Any) -> None:
        queue = self._queues[event_type]
        if queue.full():
            logger.warning("event_queue_full", event_type=event_type)
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        await queue.put(event)
        for handler in self._handlers[event_type]:
            try:
                await handler(event)
            except Exception:
                logger.exception("event_handler_failed", event_type=event_type)

    async def drain(self, event_type: str) -> Any | None:
        queue = self._queues[event_type]
        try:
            return queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
