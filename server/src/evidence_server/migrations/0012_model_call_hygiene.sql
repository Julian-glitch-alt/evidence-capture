-- Track 1 (model call hygiene): retries, budget refusals, and day totals for the budget cap.
ALTER TABLE model_calls ADD COLUMN attempts INTEGER NOT NULL DEFAULT 1;  -- transport tries for this read
ALTER TABLE model_calls DROP CONSTRAINT model_calls_status_check;
ALTER TABLE model_calls ADD CONSTRAINT model_calls_status_check
    CHECK (status IN ('ok', 'error', 'timeout', 'bad_json', 'budget'));  -- 'budget': refused by the cap, nothing sent
CREATE INDEX model_calls_created ON model_calls (created_at);
