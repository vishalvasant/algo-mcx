"""Tests for paper capital carry-forward across IST days."""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from algomcx.db.paper_account import previous_day_ending_equity


@pytest.mark.asyncio
async def test_previous_day_ending_equity_prefers_cash_plus_deployed() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "equity": Decimal("51000"),
            "cash_plus_deployed": Decimal("52500"),
        }
    )
    got = await previous_day_ending_equity(conn, Decimal("50000"))
    assert got == Decimal("52500")


@pytest.mark.asyncio
async def test_previous_day_ending_equity_falls_back_to_equity() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(
        return_value={
            "equity": Decimal("51234.50"),
            "cash_plus_deployed": Decimal("0"),
        }
    )
    got = await previous_day_ending_equity(conn, Decimal("50000"))
    assert got == Decimal("51234.50")


@pytest.mark.asyncio
async def test_previous_day_ending_equity_uses_config_when_no_prior() -> None:
    conn = AsyncMock()
    conn.fetchrow = AsyncMock(return_value=None)
    got = await previous_day_ending_equity(conn, Decimal("50000"))
    assert got == Decimal("50000")


@pytest.mark.asyncio
async def test_ensure_paper_account_seeds_from_prior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from algomcx.db import paper_account as pa

    executed: list[tuple] = []

    class FakeConn:
        async def execute(self, sql, *args):
            executed.append((sql, args))

        async def fetchrow(self, sql, *args):
            if "ORDER BY trade_date DESC" in sql:
                return {
                    "equity": Decimal("5481.45") + Decimal("50000"),
                    "cash_plus_deployed": Decimal("55481.45"),
                }
            return None

    class FakeAcquire:
        async def __aenter__(self):
            return FakeConn()

        async def __aexit__(self, *args):
            return None

    class FakePool:
        def acquire(self):
            return FakeAcquire()

    monkeypatch.setattr(pa, "get_pool", lambda: FakePool())
    await pa.ensure_paper_account(50000)

    # Bootstrap INSERT should use carried-forward equity, not raw 50000.
    bootstrap_calls = [c for c in executed if "INSERT INTO daily_risk_state" in c[0]]
    assert bootstrap_calls
    assert bootstrap_calls[0][1][0] == Decimal("55481.45")
