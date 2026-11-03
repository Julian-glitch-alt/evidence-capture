ALTER TABLE claims ADD COLUMN insurer_claim_number TEXT;
ALTER TABLE claims ADD COLUMN callback_url TEXT;
CREATE UNIQUE INDEX claims_insurer_claim_number ON claims (org_id, insurer_claim_number) WHERE insurer_claim_number IS NOT NULL;

CREATE TABLE integration_credentials (      -- our side
    org_id TEXT PRIMARY KEY REFERENCES orgs(id),
    api_key_sha256 TEXT NOT NULL,
    webhook_secret TEXT NOT NULL,
    created_at TEXT NOT NULL
);
-- the mock insurer's side (its own config: what its IT was given)
CREATE TABLE mock_config (id INTEGER PRIMARY KEY CHECK (id = 1), api_key TEXT NOT NULL, webhook_secret TEXT NOT NULL, next_claim_seq INTEGER NOT NULL);
CREATE TABLE mock_claims (id TEXT PRIMARY KEY, claim_number TEXT UNIQUE NOT NULL, policy_number TEXT NOT NULL, insured_name TEXT, vehicle_json TEXT NOT NULL, loss_date TEXT NOT NULL, loss_cause TEXT NOT NULL, description TEXT NOT NULL, claimant_phone TEXT, status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE mock_exposures (id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES mock_claims(id), type TEXT NOT NULL, coverage TEXT NOT NULL, claimant TEXT, reserve INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL);
CREATE TABLE mock_services (id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES mock_claims(id), type TEXT NOT NULL, vendor TEXT NOT NULL, status TEXT NOT NULL, vendor_ref TEXT, capture_url TEXT, vendor_base_url TEXT, requested_at TEXT NOT NULL, completed_at TEXT);
CREATE TABLE mock_activities (id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES mock_claims(id), subject TEXT NOT NULL, description TEXT, priority TEXT NOT NULL, due_date TEXT NOT NULL, status TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL, completed_at TEXT);
CREATE TABLE mock_notes (id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES mock_claims(id), subject TEXT NOT NULL, body TEXT NOT NULL, author TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE mock_documents (id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES mock_claims(id), name TEXT NOT NULL, document_type TEXT NOT NULL, url TEXT NOT NULL, sha256 TEXT, source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE mock_flags (id TEXT PRIMARY KEY, claim_id TEXT NOT NULL REFERENCES mock_claims(id), level TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL, source TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE mock_webhook_log (event_id TEXT PRIMARY KEY, event TEXT NOT NULL, claim_number TEXT, received_at TEXT NOT NULL);
