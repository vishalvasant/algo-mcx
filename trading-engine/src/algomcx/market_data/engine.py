from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

import structlog

from algomcx.broker.base import BrokerAdapter
from algomcx.bus.event_bus import EventBus
from algomcx.config import AppConfig
from algomcx.market_data.vwap import session_vwap
from algomcx.models.events import Candle, CandleInterval, QuoteUpdate

logger = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_CANDLE_TABLE = {
    CandleInterval.M1: "candles_1m",
    CandleInterval.M3: "candles_3m",
    CandleInterval.M5: "candles_5m",
}

# Max age of latest 1m bar before we treat feed as stale (market hours).
_STALE_M1_SECONDS = 120


def session_start_utc(now: datetime | None = None, market_open: str = "09:15") -> datetime:
    """Today's session open (IST) as UTC."""
    now = now or datetime.now(tz=timezone.utc)
    open_h, open_m = map(int, str(market_open).split(":"))
    return (
        now.astimezone(IST)
        .replace(hour=open_h, minute=open_m, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )


class MarketDataEngine:
    def __init__(self, config: AppConfig, broker: BrokerAdapter, bus: EventBus) -> None:
        self._config = config
        self._broker = broker
        self._bus = bus
        self._spot_token = config.symbols["spot_token"]
        self._exchange = config.symbols["exchange_spot"]
        self._market_open = str(config.market_session.get("market_open", "09:15"))
        self._candles: dict[CandleInterval, list[Candle]] = {i: [] for i in CandleInterval}
        self._spot_ltp: Decimal | None = None
        self._last_quote_ts: datetime | None = None
        self._last_candle_refresh_minute: tuple[int, int] | None = None
        self._last_successful_refresh_ts: datetime | None = None
        self._last_refresh_ok: bool = False

    def set_spot_context(self, *, exchange: str, spot_token: str) -> None:
        if spot_token and spot_token != self._spot_token:
            self._spot_token = spot_token
            self._candles = {i: [] for i in CandleInterval}
            self._last_candle_refresh_minute = None
        self._exchange = exchange
        self._market_open = str(self._config.market_session.get("market_open", "09:15"))

    def _session_start(self, now: datetime | None = None) -> datetime:
        return session_start_utc(now, self._market_open)

    @property
    def spot_ltp(self) -> Decimal | None:
        return self._spot_ltp

    @property
    def session_vwap_value(self) -> Decimal | None:
        return session_vwap(self._candles[CandleInterval.M1])

    @property
    def last_quote_ts(self) -> datetime | None:
        return self._last_quote_ts

    @property
    def last_refresh_ok(self) -> bool:
        return self._last_refresh_ok

    def candles(self, interval: CandleInterval) -> list[Candle]:
        return list(self._candles[interval])

    def latest_candle_ts(self, interval: CandleInterval = CandleInterval.M1) -> datetime | None:
        rows = self._candles.get(interval) or []
        if not rows:
            return None
        return rows[-1].ts

    def candles_stale(self, *, max_age_seconds: int = _STALE_M1_SECONDS) -> bool:
        """True when latest 1m bar is too old relative to now (during session)."""
        latest = self.latest_candle_ts(CandleInterval.M1)
        if latest is None:
            return True
        age = (datetime.now(tz=timezone.utc) - latest).total_seconds()
        return age > max_age_seconds

    async def backfill_today(self) -> None:
        now = datetime.now(tz=timezone.utc)
        start = self._session_start(now)
        for interval in CandleInterval:
            rows = await self._broker.get_candles(
                self._exchange,
                self._spot_token,
                interval,
                start,
                now,
            )
            rows = sorted(rows, key=lambda c: c.ts)
            self._candles[interval] = rows
            logger.info(
                "candles_backfilled",
                interval=interval.value,
                count=len(rows),
                first=rows[0].ts.isoformat() if rows else None,
                last=rows[-1].ts.isoformat() if rows else None,
            )
        self._last_refresh_ok = any(self._candles[i] for i in CandleInterval)
        if self._last_refresh_ok:
            self._last_successful_refresh_ts = now

    async def refresh_session_candles(self, *, force: bool = False) -> bool:
        """Refresh intraday candles so setup/trigger logic sees new bars.

        Returns True when in-memory candles were updated from a successful fetch.
        On empty/failed fetch, keeps prior bars and does NOT mark the IST minute
        as done so the next scan retries within the same minute.
        """
        ist = datetime.now(IST)
        minute_key = (ist.hour, ist.minute)
        if not force and self._last_candle_refresh_minute == minute_key:
            return False

        now = datetime.now(tz=timezone.utc)
        start = self._session_start(now)
        fetched: dict[CandleInterval, list[Candle]] = {}
        any_ok = False
        for interval in CandleInterval:
            try:
                rows = await self._broker.get_candles(
                    self._exchange,
                    self._spot_token,
                    interval,
                    start,
                    now,
                )
            except Exception:
                logger.exception("candle_fetch_failed", interval=interval.value)
                rows = []
            rows = sorted(rows, key=lambda c: c.ts)
            fetched[interval] = rows
            if rows:
                any_ok = True

        if not any_ok:
            # Keep stale bars; allow retry this same minute.
            self._last_refresh_ok = False
            logger.warning(
                "candle_refresh_empty",
                force=force,
                m1_cached=len(self._candles[CandleInterval.M1]),
                stale=self.candles_stale(),
            )
            return False

        for interval, rows in fetched.items():
            if rows:
                self._candles[interval] = rows

        self._last_candle_refresh_minute = minute_key
        self._last_successful_refresh_ts = now
        self._last_refresh_ok = True
        logger.info(
            "candles_refreshed",
            m1=len(self._candles[CandleInterval.M1]),
            m3=len(self._candles[CandleInterval.M3]),
            m5=len(self._candles[CandleInterval.M5]),
            last_m1=self.latest_candle_ts().isoformat() if self.latest_candle_ts() else None,
            stale=self.candles_stale(),
        )
        return True

    async def on_quote(self, quote: QuoteUpdate) -> None:
        self._last_quote_ts = quote.ts
        if quote.instrument_token == self._spot_token and quote.ltp is not None:
            self._spot_ltp = quote.ltp
        await self._bus.publish("quote_update", quote)

    def candle_table(self, interval: CandleInterval) -> str:
        return _CANDLE_TABLE[interval]
