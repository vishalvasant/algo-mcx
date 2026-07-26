from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from algomcx.broker.base import BrokerAdapter
from algomcx.bus.event_bus import EventBus
from algomcx.config import AppConfig
from algomcx.market_data.vwap import session_vwap
from algomcx.models.events import Candle, CandleInterval, QuoteUpdate
from algomcx.symbols_util import is_sane_nifty_spot

logger = structlog.get_logger(__name__)

IST = ZoneInfo("Asia/Kolkata")

_CANDLE_TABLE = {
    CandleInterval.M1: "candles_1m",
    CandleInterval.M3: "candles_3m",
    CandleInterval.M5: "candles_5m",
}

# Max age of latest 1m bar before we treat feed as stale (market hours).
_STALE_M1_SECONDS = 120

# Fresh install: expect at least a week of history before skipping auto-fetch.
FRESH_SETUP_LOOKBACK_DAYS = 7
FRESH_SETUP_MIN_DISTINCT_DAYS = 3


def needs_fresh_setup_backfill(coverage: dict[str, Any]) -> tuple[bool, str]:
    """True when DB lacks enough multi-day candle history for chart/strategy."""
    if int(coverage.get("count", 0)) == 0:
        return True, "no_candles_in_lookback"
    if int(coverage.get("distinct_days", 0)) < FRESH_SETUP_MIN_DISTINCT_DAYS:
        return True, "sparse_history"
    return False, "ok"


def session_start_utc(now: datetime | None = None) -> datetime:
    """Today's MCX session open 09:00 IST as UTC."""
    now = now or datetime.now(tz=timezone.utc)
    return (
        now.astimezone(IST)
        .replace(hour=9, minute=0, second=0, microsecond=0)
        .astimezone(timezone.utc)
    )


def _empty_candle_store() -> dict[CandleInterval, list[Candle]]:
    return {i: [] for i in CandleInterval}


class MarketDataEngine:
    def __init__(self, config: AppConfig, broker: BrokerAdapter, bus: EventBus) -> None:
        self._config = config
        self._broker = broker
        self._bus = bus
        self._spot_token = config.symbols["spot_token"]
        self._exchange = config.symbols["exchange_spot"]
        self._candles: dict[CandleInterval, list[Candle]] = _empty_candle_store()
        # Per-token candle cache for futures rollovers.
        self._candles_by_token: dict[str, dict[CandleInterval, list[Candle]]] = {}
        self._refresh_minute_by_token: dict[str, tuple[int, int]] = {}
        self._spot_ltp: Decimal | None = None
        self._last_quote_ts: datetime | None = None
        self._last_candle_refresh_minute: tuple[int, int] | None = None
        self._last_successful_refresh_ts: datetime | None = None
        self._last_refresh_ok: bool = False

    def _persist_active_candles(self) -> None:
        if not self._spot_token:
            return
        self._candles_by_token[self._spot_token] = {
            interval: list(rows) for interval, rows in self._candles.items()
        }
        if self._last_candle_refresh_minute is not None:
            self._refresh_minute_by_token[self._spot_token] = self._last_candle_refresh_minute

    def _load_candles_for_token(self, spot_token: str) -> None:
        cached = self._candles_by_token.get(spot_token)
        if cached is None:
            self._candles = _empty_candle_store()
        else:
            self._candles = {interval: list(rows) for interval, rows in cached.items()}
        self._last_candle_refresh_minute = self._refresh_minute_by_token.get(spot_token)
        self._last_refresh_ok = bool(self._candles.get(CandleInterval.M1))

    async def hydrate_from_db(self, conn: Any, spot_token: str) -> dict[str, int]:
        """Load today's session candles from Postgres into memory."""
        start = session_start_utc()
        loaded: dict[str, int] = {}
        for interval in CandleInterval:
            table = _CANDLE_TABLE[interval]
            rows = await conn.fetch(
                f"""
                SELECT ts, open, high, low, close, volume
                FROM {table}
                WHERE instrument_token = $1 AND ts >= $2
                ORDER BY ts
                """,
                spot_token,
                start,
            )
            candles = [
                Candle(
                    instrument_token=spot_token,
                    ts=r["ts"],
                    open=r["open"],
                    high=r["high"],
                    low=r["low"],
                    close=r["close"],
                    volume=r["volume"],
                    interval=interval,
                )
                for r in rows
            ]
            if candles:
                self._candles[interval] = candles
            loaded[interval.value] = len(candles)
        if any(loaded.values()):
            self._persist_active_candles()
            self._last_refresh_ok = bool(self._candles[CandleInterval.M1])
        return loaded

    async def candles_from_db(
        self,
        conn: Any,
        spot_token: str,
        interval: CandleInterval,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Load candles for a token/interval from Postgres (multi-day chart history)."""
        return await self.candles_from_db_for_tokens(
            conn, [spot_token], interval, start, end
        )

    async def candles_from_db_for_tokens(
        self,
        conn: Any,
        tokens: list[str],
        interval: CandleInterval,
        start: datetime,
        end: datetime | None = None,
    ) -> list[Candle]:
        """Load candles for one or more tokens (stitched chart history across rollovers)."""
        token_list = [t for t in dict.fromkeys(tokens) if t]
        if not token_list:
            return []
        table = _CANDLE_TABLE[interval]
        if end is None:
            rows = await conn.fetch(
                f"""
                SELECT instrument_token, ts, open, high, low, close, volume
                FROM {table}
                WHERE instrument_token = ANY($1::text[]) AND ts >= $2
                ORDER BY ts
                """,
                token_list,
                start,
            )
        else:
            rows = await conn.fetch(
                f"""
                SELECT instrument_token, ts, open, high, low, close, volume
                FROM {table}
                WHERE instrument_token = ANY($1::text[]) AND ts >= $2 AND ts <= $3
                ORDER BY ts
                """,
                token_list,
                start,
                end,
            )
        return [
            Candle(
                instrument_token=r["instrument_token"],
                ts=r["ts"],
                open=r["open"],
                high=r["high"],
                low=r["low"],
                close=r["close"],
                volume=r["volume"],
                interval=interval,
            )
            for r in rows
        ]

    @staticmethod
    def merge_candles(*sources: list[Candle]) -> list[Candle]:
        """Merge candle lists by timestamp; later sources override earlier ones."""
        by_ts: dict[datetime, Candle] = {}
        for rows in sources:
            for candle in rows:
                by_ts[candle.ts] = candle
        return sorted(by_ts.values(), key=lambda c: c.ts)

    @staticmethod
    def stitch_contract_candles(candles_by_token: dict[str, list[Candle]]) -> list[Candle]:
        """Stitch rolled futures contracts oldest→newest; newer contract wins on overlap."""
        if not candles_by_token:
            return []
        if len(candles_by_token) == 1:
            return sorted(next(iter(candles_by_token.values())), key=lambda c: c.ts)
        token_order = sorted(
            candles_by_token.keys(),
            key=lambda t: min(c.ts for c in candles_by_token[t]),
        )
        by_ts: dict[datetime, Candle] = {}
        for token in token_order:
            for candle in sorted(candles_by_token[token], key=lambda c: c.ts):
                by_ts[candle.ts] = candle
        return sorted(by_ts.values(), key=lambda c: c.ts)

    async def chart_tokens_from_db(
        self,
        conn: Any,
        *,
        seed_tokens: list[str],
        start: datetime,
        ref_token: str,
    ) -> list[str]:
        """Discover expired futures tokens in DB that match the reference price scale."""
        rows = await conn.fetch(
            """
            SELECT instrument_token, AVG(close)::float AS avg_close
            FROM candles_1m
            WHERE ts >= $1
            GROUP BY instrument_token
            """,
            start,
        )
        tokens = list(dict.fromkeys([t for t in seed_tokens if t]))
        ref_avg: float | None = None
        for row in rows:
            if row["instrument_token"] == ref_token:
                ref_avg = float(row["avg_close"] or 0)
                break
        if ref_avg is None or ref_avg <= 0:
            return tokens
        for row in rows:
            tok = str(row["instrument_token"])
            if tok in tokens:
                continue
            avg = float(row["avg_close"] or 0)
            if avg <= 0:
                continue
            if abs(avg - ref_avg) / ref_avg <= 0.15:
                tokens.append(tok)
        return tokens

    async def session_candle_stats_from_db(
        self, conn: Any, spot_token: str
    ) -> dict[str, dict[str, Any]]:
        """Per-interval row count and latest ts for today's session (IST open)."""
        start = session_start_utc()
        stats: dict[str, dict[str, Any]] = {}
        for interval in CandleInterval:
            table = _CANDLE_TABLE[interval]
            row = await conn.fetchrow(
                f"""
                SELECT COUNT(*)::int AS cnt, MAX(ts) AS latest
                FROM {table}
                WHERE instrument_token = $1 AND ts >= $2
                """,
                spot_token,
                start,
            )
            stats[interval.value] = {
                "count": int(row["cnt"] or 0),
                "latest": row["latest"],
            }
        return stats

    async def historical_coverage_from_db(
        self,
        conn: Any,
        spot_token: str,
        *,
        lookback_days: int = FRESH_SETUP_LOOKBACK_DAYS,
    ) -> dict[str, Any]:
        """Row count and trading-day coverage for multi-day history checks."""
        start = datetime.now(tz=timezone.utc) - timedelta(days=max(1, lookback_days))
        row = await conn.fetchrow(
            """
            SELECT
                COUNT(*)::int AS cnt,
                COUNT(DISTINCT (ts AT TIME ZONE 'Asia/Kolkata')::date)::int AS distinct_days,
                MIN(ts) AS earliest,
                MAX(ts) AS latest
            FROM candles_1m
            WHERE instrument_token = $1 AND ts >= $2
            """,
            spot_token,
            start,
        )
        return {
            "lookback_days": lookback_days,
            "count": int(row["cnt"] or 0),
            "distinct_days": int(row["distinct_days"] or 0),
            "earliest": row["earliest"],
            "latest": row["latest"],
        }

    def needs_broker_candle_sync(
        self,
        db_stats: dict[str, dict[str, Any]],
        *,
        market_open: bool,
    ) -> tuple[bool, str]:
        """Decide if Flattrade candle fetch is required (call after hydrate_from_db)."""
        if not self.candles(CandleInterval.M1):
            if db_stats.get("1m", {}).get("count", 0) > 0:
                return True, "db_hydrate_failed"
            return True, "empty"

        if self.candles_stale() and market_open:
            return True, "memory_stale"

        latest_mem = self.latest_candle_ts(CandleInterval.M1)
        latest_db = db_stats.get("1m", {}).get("latest")
        if latest_db and latest_mem and latest_db > latest_mem:
            return True, "db_ahead_of_memory"

        if market_open and latest_mem:
            age = (datetime.now(tz=timezone.utc) - latest_mem).total_seconds()
            if age > _STALE_M1_SECONDS:
                return True, "bar_stale"

        return False, "ok"

    def set_spot_context(self, *, exchange: str, spot_token: str) -> None:
        self._exchange = exchange
        if spot_token and spot_token != self._spot_token:
            self._persist_active_candles()
            self._spot_token = spot_token
            self._load_candles_for_token(spot_token)
            self._spot_ltp = None
            logger.info(
                "spot_context_switched",
                spot_token=spot_token,
                m1_cached=len(self._candles.get(CandleInterval.M1) or []),
            )

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

    def candles_for_token(self, spot_token: str, interval: CandleInterval) -> list[Candle]:
        """Cached session bars for a futures/index token (dual-index chart)."""
        cached = self._candles_by_token.get(spot_token) or {}
        return list(cached.get(interval) or [])

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

    async def backfill_contract_history(
        self,
        *,
        exchange: str,
        tokens: list[str],
        days: int = 30,
    ) -> tuple[dict[str, int], list[Candle]]:
        """Fetch multi-day candles from broker for rolled contracts."""
        now = datetime.now(tz=timezone.utc)
        start = now - timedelta(days=max(1, days))
        added: dict[str, int] = {i.value: 0 for i in CandleInterval}
        all_rows: list[Candle] = []
        for spot_token in dict.fromkeys(t for t in tokens if t):
            for interval in CandleInterval:
                try:
                    rows = await self._broker.get_candles(
                        exchange, spot_token, interval, start, now
                    )
                except Exception:
                    logger.exception(
                        "history_backfill_failed",
                        token=spot_token,
                        interval=interval.value,
                    )
                    continue
                rows = sorted(rows, key=lambda c: c.ts)
                if not rows:
                    continue
                all_rows.extend(rows)
                added[interval.value] += len(rows)
                if spot_token == self._spot_token:
                    merged = self.merge_candles(self._candles.get(interval, []), rows)
                    self._candles[interval] = merged
                    self._persist_active_candles()
        if all_rows:
            self._last_refresh_ok = bool(self._candles.get(CandleInterval.M1))
        return added, all_rows

    async def backfill_today(self) -> None:
        now = datetime.now(tz=timezone.utc)
        start = session_start_utc(now)
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
        self._persist_active_candles()
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
        start = session_start_utc(now)
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
        self._persist_active_candles()
        logger.info(
            "candles_refreshed",
            m1=len(self._candles[CandleInterval.M1]),
            m3=len(self._candles[CandleInterval.M3]),
            m5=len(self._candles[CandleInterval.M5]),
            last_m1=self.latest_candle_ts().isoformat() if self.latest_candle_ts() else None,
            stale=self.candles_stale(),
        )
        return True

    def _reference_spot_from_candles(self) -> Decimal | None:
        m1 = self._candles.get(CandleInterval.M1) or []
        if m1:
            return m1[-1].close
        return None

    def _spot_quote_trusted(self, ltp: Decimal) -> bool:
        if not is_sane_nifty_spot(ltp):
            return False
        ref = self._reference_spot_from_candles()
        if ref is None or ref <= 0:
            return True
        if abs(ltp - ref) / ref > Decimal("0.025"):
            logger.warning(
                "spot_quote_rejected_vs_candles",
                ltp=str(ltp),
                candle_close=str(ref),
            )
            return False
        return True

    def seed_spot_from_candles(self) -> Decimal | None:
        ref = self._reference_spot_from_candles()
        if ref is not None:
            self._spot_ltp = ref
        return ref

    async def on_quote(self, quote: QuoteUpdate) -> None:
        self._last_quote_ts = quote.ts
        if quote.instrument_token == self._spot_token and quote.ltp is not None:
            if self._spot_quote_trusted(quote.ltp):
                self._spot_ltp = quote.ltp
        await self._bus.publish("quote_update", quote)

    def candle_table(self, interval: CandleInterval) -> str:
        return _CANDLE_TABLE[interval]
