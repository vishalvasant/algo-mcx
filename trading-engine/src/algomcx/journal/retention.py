from __future__ import annotations

import gzip
import json
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog

from algomcx.db.connection import get_pool

logger = structlog.get_logger(__name__)
IST = ZoneInfo("Asia/Kolkata")

TABLE_SPECS: dict[str, dict[str, str]] = {
    "system_events": {
        "select": """
            SELECT id, ts, event_type, severity, message, metadata
            FROM system_events
            WHERE ts < $1
            ORDER BY ts
        """,
        "delete": "DELETE FROM system_events WHERE ts < $1",
    },
    "notifications": {
        "select": """
            SELECT id, ts, type, severity, title, message, read,
                   related_entity, related_id
            FROM notifications
            WHERE ts < $1
            ORDER BY ts
        """,
        "delete": "DELETE FROM notifications WHERE ts < $1",
    },
    "field_availability_log": {
        "select": """
            SELECT id, ts, field_name, source, available, notes
            FROM field_availability_log
            WHERE ts < $1
            ORDER BY ts
        """,
        "delete": "DELETE FROM field_availability_log WHERE ts < $1",
    },
    "option_quotes": {
        "select": """
            SELECT id, instrument_token, ts, ltp, bid, ask, volume, oi, source
            FROM option_quotes
            WHERE ts < $1
            ORDER BY ts
        """,
        "delete": "DELETE FROM option_quotes WHERE ts < $1",
    },
}


def ist_day_start(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=IST)


def retention_cutoff(*, keep_days: int = 1, now: datetime | None = None) -> datetime:
    """Archive/delete rows with ts strictly before this instant (IST day boundary)."""
    if keep_days < 1:
        raise ValueError("keep_days must be >= 1")
    current = (now or datetime.now(tz=IST)).astimezone(IST)
    cutoff_day = current.date() - timedelta(days=keep_days - 1)
    return ist_day_start(cutoff_day)


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    return str(value)


def serialize_row(row: Any) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def group_rows_by_ist_day(rows: list[Any]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        ts = row["ts"]
        if isinstance(ts, datetime):
            day_key = ts.astimezone(IST).date().isoformat()
        else:
            day_key = str(ts)[:10]
        grouped.setdefault(day_key, []).append(serialize_row(row))
    return grouped


def write_archive_records(
    archive_dir: Path,
    table: str,
    day_key: str,
    records: list[dict[str, Any]],
) -> Path:
    out_dir = archive_dir / day_key
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{table}.jsonl.gz"
    payload = "".join(json.dumps(rec, default=_json_default) + "\n" for rec in records)
    with gzip.open(out_path, "ab") as handle:
        handle.write(payload.encode("utf-8"))
    return out_path


async def _archive_table(
    conn: Any,
    table: str,
    cutoff: datetime,
    archive_dir: Path,
) -> int:
    spec = TABLE_SPECS[table]
    rows = await conn.fetch(spec["select"], cutoff)
    if not rows:
        return 0

    for day_key, records in group_rows_by_ist_day(rows).items():
        write_archive_records(archive_dir, table, day_key, records)

    result = await conn.execute(spec["delete"], cutoff)
    try:
        return int(str(result).split()[-1])
    except (ValueError, IndexError):
        return len(rows)


async def run_log_retention(config: dict[str, Any] | None = None) -> dict[str, int]:
    """Export rows older than the retention window, then delete them from Postgres."""
    cfg = config or {}
    if not cfg.get("enabled", True):
        return {}

    keep_days = int(cfg.get("keep_days", 1))
    archive_dir = Path(str(cfg.get("archive_dir", "./data/archives")))
    archive_dir.mkdir(parents=True, exist_ok=True)
    cutoff = retention_cutoff(keep_days=keep_days)

    tables = cfg.get("tables") or list(TABLE_SPECS.keys())
    stats: dict[str, int] = {}
    pool = get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            for table in tables:
                name = str(table)
                if name not in TABLE_SPECS:
                    logger.warning("log_retention_unknown_table", table=name)
                    continue
                try:
                    deleted = await _archive_table(conn, name, cutoff, archive_dir)
                    stats[name] = deleted
                except Exception:
                    logger.exception("log_retention_table_failed", table=name)
                    raise

    logger.info(
        "log_retention_complete",
        cutoff=cutoff.isoformat(),
        archive_dir=str(archive_dir),
        **stats,
    )
    return stats
