CREATE TABLE reports (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id),
    token TEXT UNIQUE NOT NULL,
    path TEXT NOT NULL,            -- blob key, reports/{claim_id}/verified-evidence-report.pdf
    sha256 TEXT NOT NULL,
    version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE outbox (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    event TEXT NOT NULL,           -- evidence.ready | decision.recorded (T27)
    body_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    delivered_at TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    UNIQUE (claim_id, event)
);
