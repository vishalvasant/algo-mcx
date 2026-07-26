-- Flattrade broker credentials (portal-managed; single row)
CREATE TABLE IF NOT EXISTS flattrade_credentials (
    id SMALLINT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    user_id TEXT,
    api_key TEXT,
    api_secret TEXT,
    password TEXT,
    totp_secret TEXT,
    redirect_url TEXT NOT NULL DEFAULT 'http://127.0.0.1:8000/callback',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
