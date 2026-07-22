-- Paper account capital tracking (default ₹50,000)
ALTER TABLE daily_risk_state
    ADD COLUMN IF NOT EXISTS starting_capital NUMERIC(12, 4) DEFAULT 50000,
    ADD COLUMN IF NOT EXISTS available_capital NUMERIC(12, 4) DEFAULT 50000,
    ADD COLUMN IF NOT EXISTS deployed_capital NUMERIC(12, 4) DEFAULT 0;
