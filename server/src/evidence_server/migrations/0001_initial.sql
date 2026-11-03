-- 0001_initial: first Postgres schema, ported from the SQLite SCHEMA in store.py.
-- Types keep their meaning: 0/1 flags stay INTEGER, timestamps stay TEXT
-- (ISO-8601), JSON stays TEXT, so event hashes and seals are byte-identical.

CREATE TABLE orgs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    default_tz TEXT NOT NULL
);

CREATE TABLE policies (
    org_id TEXT NOT NULL REFERENCES orgs(id),
    policy_no TEXT NOT NULL,
    state TEXT NOT NULL,
    data_json TEXT NOT NULL,
    PRIMARY KEY (org_id, policy_no)
);

CREATE TABLE policy_snapshots (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    policy_no TEXT NOT NULL,
    verified INTEGER NOT NULL,
    data_json TEXT,
    sha256 TEXT NOT NULL
);

CREATE TABLE claims (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    token TEXT UNIQUE NOT NULL,
    template_id TEXT NOT NULL,
    policy_snapshot_id TEXT NOT NULL REFERENCES policy_snapshots(id),
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    submitted_at TEXT,
    form_json TEXT,
    draft_json TEXT NOT NULL DEFAULT '{}',
    facts_json TEXT NOT NULL DEFAULT '{}',
    reason_codes_json TEXT NOT NULL DEFAULT '[]',
    missing_info_json TEXT NOT NULL DEFAULT '[]',
    liability TEXT NOT NULL DEFAULT 'UNDETERMINED',
    coverage_eligibility TEXT NOT NULL DEFAULT 'UNDETERMINED',
    damage_severity TEXT NOT NULL DEFAULT 'UNDETERMINED',
    payment_approval TEXT NOT NULL DEFAULT 'UNDETERMINED'
);
CREATE INDEX claims_status ON claims(org_id, status);

CREATE TABLE claim_events (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    seq INTEGER NOT NULL,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    data_json TEXT NOT NULL,
    prev_hash TEXT NOT NULL,
    hash TEXT NOT NULL,
    UNIQUE (claim_id, seq)
);
-- The two SQLite triggers become one function + one trigger: the log itself
-- refuses UPDATE and DELETE, so a tampered row breaks loudly instead of quietly.
CREATE OR REPLACE FUNCTION claim_events_append_only() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'claim_events is append-only';
END;
$$ LANGUAGE plpgsql;
CREATE TRIGGER claim_events_no_write
    BEFORE UPDATE OR DELETE ON claim_events
    FOR EACH ROW EXECUTE FUNCTION claim_events_append_only();

CREATE TABLE capture_sessions (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    code TEXT NOT NULL,
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX capture_sessions_claim ON capture_sessions(claim_id);

CREATE TABLE slot_nonces (
    nonce TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    session_id TEXT NOT NULL REFERENCES capture_sessions(id),
    slot_id TEXT NOT NULL,
    issued_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    item_id TEXT
);
CREATE INDEX slot_nonces_claim ON slot_nonces(claim_id);

CREATE TABLE seals (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id),
    manifest_json TEXT NOT NULL,
    signature TEXT NOT NULL,
    key_id TEXT NOT NULL,
    sealed_at TEXT NOT NULL
);

CREATE TABLE items (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    step_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    mime TEXT NOT NULL,
    path TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    received_at TEXT NOT NULL,
    client_meta_json TEXT NOT NULL,
    superseded INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX items_claim_step ON items(claim_id, step_id);

CREATE TABLE login_failures (
    ip TEXT NOT NULL,
    at DOUBLE PRECISION NOT NULL
);
CREATE INDEX login_failures_ip_at ON login_failures(ip, at);
