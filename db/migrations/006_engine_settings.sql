-- Runtime engine settings (execution mode, etc.)
CREATE TABLE IF NOT EXISTS engine_settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
