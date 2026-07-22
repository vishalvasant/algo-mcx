-- Algo-Flat initial schema (V1)
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

CREATE TABLE instruments (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    exchange        TEXT NOT NULL,
    token           TEXT NOT NULL,
    tsym            TEXT NOT NULL,
    underlying      TEXT NOT NULL,
    expiry_date     DATE NOT NULL,
    strike          NUMERIC(10,2) NOT NULL,
    option_type     TEXT NOT NULL,
    lot_size        INT NOT NULL,
    tick_size       NUMERIC(10,4),
    is_atm          BOOLEAN DEFAULT FALSE,
    in_band         BOOLEAN DEFAULT FALSE,
    created_at      TIMESTAMPTZ DEFAULT now(),
    UNIQUE (exchange, token)
);
CREATE INDEX idx_instruments_expiry ON instruments (expiry_date, underlying);

CREATE TABLE broker_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         TEXT NOT NULL,
    access_token    TEXT NOT NULL,
    expires_at      TIMESTAMPTZ,
    static_ip       TEXT,
    api_version     TEXT DEFAULT 'v2',
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE candles_1m (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_token TEXT NOT NULL,
    ts               TIMESTAMPTZ NOT NULL,
    open             NUMERIC(12,4) NOT NULL,
    high             NUMERIC(12,4) NOT NULL,
    low              NUMERIC(12,4) NOT NULL,
    close            NUMERIC(12,4) NOT NULL,
    volume           BIGINT,
    UNIQUE (instrument_token, ts)
);
CREATE INDEX idx_candles_1m_token_ts ON candles_1m (instrument_token, ts DESC);

CREATE TABLE candles_3m (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_token TEXT NOT NULL,
    ts               TIMESTAMPTZ NOT NULL,
    open             NUMERIC(12,4) NOT NULL,
    high             NUMERIC(12,4) NOT NULL,
    low              NUMERIC(12,4) NOT NULL,
    close            NUMERIC(12,4) NOT NULL,
    volume           BIGINT,
    UNIQUE (instrument_token, ts)
);
CREATE INDEX idx_candles_3m_token_ts ON candles_3m (instrument_token, ts DESC);

CREATE TABLE candles_5m (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_token TEXT NOT NULL,
    ts               TIMESTAMPTZ NOT NULL,
    open             NUMERIC(12,4) NOT NULL,
    high             NUMERIC(12,4) NOT NULL,
    low              NUMERIC(12,4) NOT NULL,
    close            NUMERIC(12,4) NOT NULL,
    volume           BIGINT,
    UNIQUE (instrument_token, ts)
);
CREATE INDEX idx_candles_5m_token_ts ON candles_5m (instrument_token, ts DESC);

CREATE TABLE option_quotes (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_token TEXT NOT NULL,
    ts               TIMESTAMPTZ NOT NULL,
    ltp              NUMERIC(12,4),
    bid              NUMERIC(12,4),
    ask              NUMERIC(12,4),
    volume           BIGINT,
    oi               BIGINT,
    source           TEXT NOT NULL
);
CREATE INDEX idx_option_quotes_token_ts ON option_quotes (instrument_token, ts DESC);

CREATE TABLE option_snapshots (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    instrument_token TEXT NOT NULL,
    ts               TIMESTAMPTZ NOT NULL,
    ltp              NUMERIC(12,4),
    bid              NUMERIC(12,4),
    ask              NUMERIC(12,4),
    spread_pct       NUMERIC(8,4),
    volume           BIGINT,
    oi               BIGINT,
    field_flags      JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE option_features (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    snapshot_id      UUID,
    instrument_token TEXT NOT NULL,
    ts               TIMESTAMPTZ NOT NULL,
    features         JSONB NOT NULL
);

CREATE TABLE candidate_signals (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts               TIMESTAMPTZ NOT NULL,
    setup_type       TEXT NOT NULL,
    side             TEXT NOT NULL,
    instrument_token TEXT NOT NULL,
    tsym             TEXT NOT NULL,
    feature_snapshot JSONB NOT NULL,
    strategy_version TEXT NOT NULL,
    scanner_metadata JSONB DEFAULT '{}'
);
CREATE INDEX idx_candidate_signals_ts ON candidate_signals (ts DESC);

CREATE TABLE validation_results (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_signal_id UUID NOT NULL REFERENCES candidate_signals(id),
    ts                  TIMESTAMPTZ NOT NULL,
    passed              BOOLEAN NOT NULL,
    rejection_reasons   TEXT[] NOT NULL DEFAULT '{}',
    validator_version   TEXT NOT NULL
);

CREATE TABLE ml_scores (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    candidate_signal_id UUID NOT NULL REFERENCES candidate_signals(id),
    ts                  TIMESTAMPTZ NOT NULL,
    model_version       TEXT NOT NULL,
    score               NUMERIC(8,6),
    confidence          NUMERIC(8,6),
    recommendation      TEXT NOT NULL,
    mode                TEXT NOT NULL
);

CREATE TABLE orders (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_order_id     TEXT NOT NULL UNIQUE,
    broker_order_id     TEXT,
    candidate_signal_id UUID REFERENCES candidate_signals(id),
    ts_created          TIMESTAMPTZ NOT NULL,
    ts_sent             TIMESTAMPTZ,
    ts_ack              TIMESTAMPTZ,
    ts_filled           TIMESTAMPTZ,
    exchange            TEXT NOT NULL,
    tsym                TEXT NOT NULL,
    side                TEXT NOT NULL,
    quantity            INT NOT NULL,
    order_type          TEXT NOT NULL,
    limit_price         NUMERIC(12,4),
    fill_price          NUMERIC(12,4),
    filled_qty          INT DEFAULT 0,
    status              TEXT NOT NULL,
    slippage            NUMERIC(12,4),
    latency_ms          INT,
    mode                TEXT NOT NULL,
    rejection_reason    TEXT
);

CREATE TABLE positions (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id         UUID NOT NULL REFERENCES orders(id),
    instrument_token TEXT NOT NULL,
    tsym             TEXT NOT NULL,
    side             TEXT NOT NULL,
    quantity         INT NOT NULL,
    entry_price      NUMERIC(12,4) NOT NULL,
    entry_ts         TIMESTAMPTZ NOT NULL,
    stop_loss        NUMERIC(12,4),
    target           NUMERIC(12,4),
    mfe              NUMERIC(12,4) DEFAULT 0,
    mae              NUMERIC(12,4) DEFAULT 0,
    status           TEXT NOT NULL,
    mode             TEXT NOT NULL
);

CREATE TABLE closed_trades (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    position_id         UUID NOT NULL REFERENCES positions(id),
    candidate_signal_id UUID REFERENCES candidate_signals(id),
    entry_ts            TIMESTAMPTZ NOT NULL,
    exit_ts             TIMESTAMPTZ NOT NULL,
    entry_price         NUMERIC(12,4) NOT NULL,
    exit_price          NUMERIC(12,4) NOT NULL,
    quantity            INT NOT NULL,
    pnl                 NUMERIC(12,4) NOT NULL,
    pnl_pct             NUMERIC(8,4),
    mfe                 NUMERIC(12,4),
    mae                 NUMERIC(12,4),
    exit_reason         TEXT NOT NULL,
    hold_seconds        INT,
    signal_snapshot     JSONB NOT NULL,
    setup_type          TEXT NOT NULL,
    mode                TEXT NOT NULL
);

CREATE TABLE daily_risk_state (
    id                   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_date           DATE NOT NULL UNIQUE,
    realized_pnl         NUMERIC(12,4) DEFAULT 0,
    trade_count          INT DEFAULT 0,
    consecutive_losses   INT DEFAULT 0,
    kill_switch          BOOLEAN DEFAULT FALSE,
    entries_blocked      BOOLEAN DEFAULT FALSE,
    block_reason         TEXT,
    starting_capital     NUMERIC(12,4) DEFAULT 50000,
    available_capital    NUMERIC(12,4) DEFAULT 50000,
    deployed_capital     NUMERIC(12,4) DEFAULT 0,
    updated_at           TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE system_events (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    event_type  TEXT NOT NULL,
    severity    TEXT NOT NULL,
    message     TEXT NOT NULL,
    metadata    JSONB DEFAULT '{}'
);

CREATE TABLE field_availability_log (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts          TIMESTAMPTZ NOT NULL DEFAULT now(),
    field_name  TEXT NOT NULL,
    source      TEXT NOT NULL,
    available   BOOLEAN NOT NULL,
    notes       TEXT
);

CREATE TABLE notifications (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    ts             TIMESTAMPTZ NOT NULL DEFAULT now(),
    type           TEXT NOT NULL,
    severity       TEXT NOT NULL,
    title          TEXT NOT NULL,
    message        TEXT NOT NULL,
    read           BOOLEAN DEFAULT FALSE,
    related_entity TEXT,
    related_id     UUID
);
CREATE INDEX idx_notifications_unread ON notifications (read, ts DESC);
