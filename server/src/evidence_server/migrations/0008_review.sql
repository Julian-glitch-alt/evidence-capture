CREATE TABLE org_settings (org_id TEXT PRIMARY KEY REFERENCES orgs(id), delivery_mode TEXT NOT NULL DEFAULT 'on_ready');
CREATE TABLE reviews (
    id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES claims(id),
    kind TEXT NOT NULL,          -- flag | damage | note
    target TEXT,                 -- flag key or damage key; NULL for notes
    verdict TEXT,                -- confirmed | false_alarm (flag), excluded | included (damage); NULL for notes
    reason TEXT, text TEXT, decided_by TEXT NOT NULL, at TEXT NOT NULL
);
ALTER TABLE reports ADD COLUMN ai_summary_json TEXT;
ALTER TABLE reports ADD COLUMN reviewed_path TEXT;
ALTER TABLE reports ADD COLUMN reviewed_sha256 TEXT;
ALTER TABLE reports ADD COLUMN reviewed_at TEXT;
