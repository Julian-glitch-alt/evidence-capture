-- 0009_durable_work (track 3): the sweeper re-runs stale analyses a limited number
-- of times and retries undelivered outbox messages with exponential backoff.
ALTER TABLE analyses ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1;
ALTER TABLE photo_checks ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1;
-- next_attempt_at is also the lease: a worker moves it forward before posting, so
-- two workers never post the same message at once. dead_at: gave up (manual Resend still works).
ALTER TABLE outbox ADD COLUMN next_attempt_at TEXT;
ALTER TABLE outbox ADD COLUMN dead_at TEXT;
CREATE INDEX outbox_pending ON outbox (next_attempt_at) WHERE delivered_at IS NULL AND dead_at IS NULL;
