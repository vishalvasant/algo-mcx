from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class TradingMode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class CandleInterval(str, Enum):
    M1 = "1m"
    M3 = "3m"
    M5 = "5m"


class Bias(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class Candle(BaseModel):
    instrument_token: str
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None = None
    interval: CandleInterval
    # Optional fields from Flattrade TPSeries (not true ticks).
    vwap: Decimal | None = None
    oi: int | None = None


class Instrument(BaseModel):
    exchange: str
    token: str
    tsym: str
    underlying: str
    expiry_date: datetime | None = None
    strike: Decimal
    option_type: str
    lot_size: int
    tick_size: Decimal | None = None
    is_atm: bool = False
    in_band: bool = False


class QuoteUpdate(BaseModel):
    event_type: str = "quote_update"
    ts: datetime
    exchange: str
    instrument_token: str
    tsym: str | None = None
    ltp: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: int | None = None
    oi: int | None = None
    source: str = "websocket"


class OptionState(BaseModel):
    instrument_token: str
    tsym: str
    ltp: Decimal | None = None
    bid: Decimal | None = None
    ask: Decimal | None = None
    spread_pct: Decimal | None = None
    volume: int | None = None
    oi: int | None = None
    last_update_ts: datetime | None = None
    field_flags: dict[str, bool] = Field(default_factory=dict)


class FeatureSnapshot(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    ts: datetime
    nifty_spot: Decimal | None = None
    session_vwap: Decimal | None = None
    bias_5m: Bias = Bias.NEUTRAL
    setup_3m: str | None = None
    trigger_1m: str | None = None
    option_spread_pct: Decimal | None = None
    option_oi: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class MarketRegime(BaseModel):
    """Rule-based market classification (deterministic)."""

    ts: datetime
    primary: str
    probabilities: dict[str, float] = Field(default_factory=dict)
    trade_allowed: bool = True
    risk_score: int = 0
    reasons: list[str] = Field(default_factory=list)
    health: dict[str, Any] = Field(default_factory=dict)


class StrategyDecision(BaseModel):
    """Router outcome: one strategy or NO_TRADE, with audit trail."""

    event_type: str = "strategy_decision"
    ts: datetime
    selected_strategy: str
    confidence: int = 0
    trade_allowed: bool = False
    position_side: str = "NONE"
    selected_reason: str = ""
    regime: MarketRegime | None = None
    strategy_scores: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)


class CandidateSignal(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    event_type: str = "candidate_signal"
    ts: datetime
    setup_type: str
    side: str
    instrument_token: str
    tsym: str
    strategy_version: str
    feature_snapshot: FeatureSnapshot
    scanner_metadata: dict[str, Any] = Field(default_factory=dict)
    confidence: int | None = None


class ValidationResult(BaseModel):
    event_type: str = "validation_result"
    candidate_signal_id: UUID
    ts: datetime
    passed: bool
    rejection_reasons: list[str] = Field(default_factory=list)
    validator_version: str


class ExecutionRequest(BaseModel):
    event_type: str = "execution_request"
    client_order_id: str
    ts: datetime
    candidate_signal_id: UUID | None = None
    instrument_token: str
    exchange: str
    tsym: str
    side: str
    quantity: int
    order_type: str
    limit_price: Decimal | None = None
    product: str
    reference_ltp: Decimal
    mode: TradingMode


class OrderUpdate(BaseModel):
    event_type: str = "order_update"
    ts: datetime
    client_order_id: str
    broker_order_id: str | None = None
    status: str
    report_type: str | None = None
    fill_price: Decimal | None = None
    filled_qty: int = 0
    avg_price: Decimal | None = None
    slippage: Decimal | None = None
    latency_ms: int | None = None
    mode: TradingMode
    rejection_reason: str | None = None


class PositionUpdate(BaseModel):
    event_type: str = "position_update"
    position_id: UUID
    ts: datetime
    status: str
    instrument_token: str
    tsym: str
    entry_price: Decimal
    current_ltp: Decimal | None = None
    unrealized_pnl: Decimal | None = None
    mfe: Decimal = Decimal("0")
    mae: Decimal = Decimal("0")
    stop_loss: Decimal | None = None
    target: Decimal | None = None
    hold_seconds: int = 0


class TradeCloseSummary(BaseModel):
    event_type: str = "trade_close_summary"
    closed_trade_id: UUID = Field(default_factory=uuid4)
    position_id: UUID
    entry_ts: datetime
    exit_ts: datetime
    entry_price: Decimal
    exit_price: Decimal
    quantity: int
    pnl: Decimal
    pnl_pct: Decimal | None = None
    mfe: Decimal
    mae: Decimal
    exit_reason: str
    hold_seconds: int
    setup_type: str
    mode: TradingMode
    signal_snapshot: dict[str, Any] = Field(default_factory=dict)


class SystemEvent(BaseModel):
    event_type: str
    ts: datetime
    severity: str
    message: str
    metadata: dict[str, Any] = Field(default_factory=dict)
