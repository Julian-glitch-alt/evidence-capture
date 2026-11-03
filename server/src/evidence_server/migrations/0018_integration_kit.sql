-- 0018_integration_kit (T48): API keys per org, test mode on claims.
CREATE TABLE api_keys (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL REFERENCES orgs(id),
    name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('live', 'test')),
    prefix TEXT NOT NULL,
    key_sha256 TEXT NOT NULL UNIQUE,
    seq BIGSERIAL NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT,
    last_used_at TEXT,
    expires_at TEXT,
    revoked_at TEXT
);
CREATE INDEX api_keys_org ON api_keys (org_id);
ALTER TABLE integration_credentials ADD COLUMN rotated_at TEXT;
ALTER TABLE claims ADD COLUMN test_mode BOOLEAN NOT NULL DEFAULT FALSE;
DROP INDEX claims_insurer_claim_number;
CREATE UNIQUE INDEX claims_insurer_claim_number ON claims (org_id, test_mode, insurer_claim_number) WHERE insurer_claim_number IS NOT NULL;
-- Today's key becomes the seed row (fresh databases copy nothing here:
-- ensure_credentials writes the mock's key into api_keys at first start-up).
INSERT INTO api_keys (id, org_id, name, mode, prefix, key_sha256, created_at, created_by)
SELECT 'seed-' || c.org_id, c.org_id, 'Demo insurer (mock)', 'live',
       substr(m.api_key, 1, 12), c.api_key_sha256, c.created_at, NULL
FROM integration_credentials c JOIN mock_config m ON m.id = 1
ON CONFLICT DO NOTHING;
