-- Speed up daily log archive + purge (ts range scans)
CREATE INDEX IF NOT EXISTS idx_system_events_ts ON system_events (ts);
CREATE INDEX IF NOT EXISTS idx_notifications_ts ON notifications (ts);
CREATE INDEX IF NOT EXISTS idx_field_availability_log_ts ON field_availability_log (ts);
