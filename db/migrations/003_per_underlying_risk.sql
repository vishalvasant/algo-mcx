-- Per-index paper capital (NIFTY + SENSEX)
ALTER TABLE daily_risk_state
    ADD COLUMN IF NOT EXISTS underlying TEXT NOT NULL DEFAULT 'NIFTY';

-- Allow one risk row per index per day
ALTER TABLE daily_risk_state DROP CONSTRAINT IF EXISTS daily_risk_state_trade_date_key;
DROP INDEX IF EXISTS idx_daily_risk_trade_underlying;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_class t ON c.conrelid = t.oid
    WHERE t.relname = 'daily_risk_state'
      AND c.conname = 'daily_risk_state_trade_date_underlying_key'
  ) THEN
    ALTER TABLE daily_risk_state
      ADD CONSTRAINT daily_risk_state_trade_date_underlying_key
      UNIQUE (trade_date, underlying);
  END IF;
END $$;
