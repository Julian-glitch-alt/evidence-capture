CREATE TABLE checks (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    check_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK (result IN ('pass', 'unclear', 'fail', 'value')),
    reason_code TEXT,
    reason TEXT NOT NULL,
    value_json TEXT,
    inputs_json TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (claim_id, check_id)
);
CREATE INDEX checks_claim ON checks (claim_id);
