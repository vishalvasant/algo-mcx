from __future__ import annotations

import json
from uuid import UUID

import structlog

from algomcx.db.connection import get_pool
from algomcx.models.events import (
  CandidateSignal,
  Candle,
  CandleInterval,
  ExecutionRequest,
  OrderUpdate,
  QuoteUpdate,
  SystemEvent,
  ValidationResult,
)

logger = structlog.get_logger(__name__)


class JournalWriter:
    async def write_candles(self, candles: list[Candle]) -> None:
        if not candles:
            return
        table = {
            CandleInterval.M1: "candles_1m",
            CandleInterval.M3: "candles_3m",
            CandleInterval.M5: "candles_5m",
        }[candles[0].interval]
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.executemany(
                f"""
                INSERT INTO {table}
                    (instrument_token, ts, open, high, low, close, volume)
                VALUES ($1, $2, $3, $4, $5, $6, $7)
                ON CONFLICT (instrument_token, ts) DO UPDATE SET
                    open = EXCLUDED.open,
                    high = EXCLUDED.high,
                    low = EXCLUDED.low,
                    close = EXCLUDED.close,
                    volume = EXCLUDED.volume
                """,
                [
                    (
                        c.instrument_token,
                        c.ts,
                        c.open,
                        c.high,
                        c.low,
                        c.close,
                        c.volume,
                    )
                    for c in candles
                ],
            )

    async def write_quote(self, quote: QuoteUpdate) -> None:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO option_quotes
                    (instrument_token, ts, ltp, bid, ask, volume, oi, source)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                """,
                quote.instrument_token,
                quote.ts,
                quote.ltp,
                quote.bid,
                quote.ask,
                quote.volume,
                quote.oi,
                quote.source,
            )

    async def write_system_event(self, event: SystemEvent) -> None:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO system_events (ts, event_type, severity, message, metadata)
                VALUES ($1, $2, $3, $4, $5::jsonb)
                """,
                event.ts,
                event.event_type,
                event.severity,
                event.message,
                json.dumps(event.metadata),
            )

    async def write_notification(
        self,
        type_: str,
        severity: str,
        title: str,
        message: str,
        related_entity: str | None = None,
        related_id: str | None = None,
    ) -> None:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO notifications
                    (type, severity, title, message, related_entity, related_id)
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                type_,
                severity,
                title,
                message,
                related_entity,
                related_id,
            )

    async def log_field_availability(
        self, field_name: str, source: str, available: bool, notes: str | None = None
    ) -> None:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO field_availability_log (field_name, source, available, notes)
                VALUES ($1, $2, $3, $4)
                """,
                field_name,
                source,
                available,
                notes,
            )

    async def write_candidate_signal(self, signal: CandidateSignal) -> None:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO candidate_signals (
                    id, ts, setup_type, side, instrument_token, tsym,
                    feature_snapshot, strategy_version, scanner_metadata
                ) VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8, $9::jsonb)
                """,
                signal.id,
                signal.ts,
                signal.setup_type,
                signal.side,
                signal.instrument_token,
                signal.tsym,
                json.dumps(signal.feature_snapshot.model_dump(mode="json"), default=str),
                signal.strategy_version,
                json.dumps(signal.scanner_metadata, default=str),
            )

    async def write_validation(self, result: ValidationResult) -> None:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO validation_results (
                    candidate_signal_id, ts, passed, rejection_reasons, validator_version
                ) VALUES ($1, $2, $3, $4, $5)
                """,
                result.candidate_signal_id,
                result.ts,
                result.passed,
                result.rejection_reasons,
                result.validator_version,
            )

    async def write_order_created(
        self, request: ExecutionRequest, candidate_id: UUID | None
    ) -> UUID:
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO orders (
                    client_order_id, candidate_signal_id, ts_created, ts_sent,
                    exchange, tsym, side, quantity, order_type, limit_price,
                    status, mode
                ) VALUES ($1, $2, $3, $3, $4, $5, $6, $7, $8, $9, 'SENT', $10)
                RETURNING id
                """,
                request.client_order_id,
                candidate_id,
                request.ts,
                request.exchange,
                request.tsym,
                request.side,
                request.quantity,
                request.order_type,
                request.limit_price,
                request.mode.value,
            )
        return row["id"]

    async def write_order_filled(self, order_id: UUID, update: OrderUpdate) -> None:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE orders SET
                    broker_order_id = $2,
                    ts_filled = $3,
                    fill_price = $4,
                    filled_qty = $5,
                    status = $6,
                    slippage = $7,
                    latency_ms = $8
                WHERE id = $1
                """,
                order_id,
                update.broker_order_id,
                update.ts,
                update.fill_price,
                update.filled_qty,
                update.status,
                update.slippage,
                update.latency_ms,
            )

    async def write_position_opened(
        self,
        *,
        order_id: UUID,
        signal: CandidateSignal,
        fill_price,
        quantity: int,
        stop_loss,
        target,
        mode: str,
    ) -> UUID:
        pool = get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                INSERT INTO positions (
                    order_id, instrument_token, tsym, side, quantity,
                    entry_price, entry_ts, stop_loss, target, status, mode
                ) VALUES ($1, $2, $3, 'BUY', $4, $5, now(), $6, $7, 'OPEN', $8)
                RETURNING id
                """,
                order_id,
                signal.instrument_token,
                signal.tsym,
                quantity,
                fill_price,
                stop_loss,
                target,
                mode,
            )
        return row["id"]

    async def write_position_closed(self, position_id: UUID, mfe, mae) -> None:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE positions SET status = 'CLOSED', mfe = $2, mae = $3
                WHERE id = $1
                """,
                position_id,
                mfe,
                mae,
            )

    async def write_closed_trade(
        self,
        *,
        position,
        exit_ts,
        exit_price,
        pnl,
        exit_reason: str,
        hold_seconds: int,
    ) -> None:
        entry = position.entry_price
        pnl_pct = (pnl / (entry * position.quantity) * 100) if entry > 0 else None
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO closed_trades (
                    position_id, candidate_signal_id, entry_ts, exit_ts,
                    entry_price, exit_price, quantity, pnl, pnl_pct,
                    mfe, mae, exit_reason, hold_seconds, signal_snapshot,
                    setup_type, mode
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14::jsonb, $15, $16)
                """,
                position.position_id,
                position.candidate_signal_id,
                position.entry_ts,
                exit_ts,
                position.entry_price,
                exit_price,
                position.quantity,
                pnl,
                pnl_pct,
                position.mfe,
                position.mae,
                exit_reason,
                hold_seconds,
                json.dumps(position.signal_snapshot, default=str),
                position.setup_type,
                "paper",
            )
