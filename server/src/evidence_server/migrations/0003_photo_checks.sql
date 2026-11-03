CREATE TABLE model_calls (
    id TEXT PRIMARY KEY,
    cache_key TEXT NOT NULL,
    purpose TEXT NOT NULL,          -- e.g. "photo.vin_photo#2"
    model TEXT NOT NULL,
    claim_id TEXT,
    item_id TEXT,
    status TEXT NOT NULL CHECK (status IN ('ok', 'error', 'timeout', 'bad_json')),
    response_json TEXT,             -- the parsed JSON object (canonical), only when ok
    error TEXT,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cost_usd DOUBLE PRECISION,
    latency_ms INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX model_calls_cache ON model_calls (cache_key) WHERE status = 'ok';
CREATE INDEX model_calls_claim ON model_calls (claim_id);

CREATE TABLE photo_checks (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    claim_id TEXT NOT NULL REFERENCES claims(id),
    slot_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed')),
    readings_json TEXT,             -- JSON list, one model answer per read; only when done
    error TEXT,                     -- why it failed, e.g. "timeout", "license_check_off", "model_not_configured"
    version TEXT NOT NULL,          -- photo_check.VERSION + "+" + photo_prompts.VERSION
    started_at TEXT NOT NULL,
    finished_at TEXT
);
CREATE INDEX photo_checks_claim ON photo_checks (claim_id);
