"""Rich Telegram copy for trade entry / exit alerts."""

from __future__ import annotations

from decimal import Decimal

from algomcx.risk.engine import DailyRiskSnapshot, RiskEngine
from algomcx.runtime.trading_mode import get_execution_mode


def _inr(amount: Decimal) -> str:
    return f"₹{amount:,.2f}"


def _pnl_text(pnl: Decimal) -> str:
    prefix = "+" if pnl > 0 else ""
    return f"{prefix}{_inr(pnl)}"


def _account_lines(snap: DailyRiskSnapshot) -> str:
    return "\n".join(
        [
            f"Available: {_inr(snap.available_capital)}",
            f"Deployed: {_inr(snap.deployed_capital)}",
            f"Day P&L: {_pnl_text(snap.realized_pnl)}",
            f"Balance: {_inr(snap.equity)}",
        ]
    )


async def build_trade_entry_message(
    risk: RiskEngine,
    *,
    tsym: str,
    fill: Decimal,
    quantity: int,
    setup_type: str | None = None,
) -> str:
    snap = await risk.ensure_daily_state()
    premium = (fill or Decimal("0")) * quantity
    mode = get_execution_mode().upper()
    lines = [
        f"Mode: {mode}",
        f"BUY {tsym}",
        f"Fill @ {_inr(fill)} × {quantity} qty = {_inr(premium)}",
    ]
    if setup_type:
        lines.append(f"Setup: {setup_type}")
    lines.extend(["", _account_lines(snap)])
    return "\n".join(lines)


async def build_trade_exit_message(
    risk: RiskEngine,
    *,
    tsym: str,
    fill: Decimal,
    trade_pnl: Decimal,
    quantity: int,
    exit_reason: str | None = None,
) -> str:
    snap = await risk.ensure_daily_state()
    mode = get_execution_mode().upper()
    outcome = "Profit" if trade_pnl >= 0 else "Loss"
    lines = [
        f"Mode: {mode} · {outcome}",
        f"SELL {tsym}",
        f"Fill @ {_inr(fill)} × {quantity} qty",
        f"Trade P&L: {_pnl_text(trade_pnl)}",
    ]
    if exit_reason:
        lines.append(f"Reason: {exit_reason}")
    lines.extend(["", _account_lines(snap)])
    return "\n".join(lines)
