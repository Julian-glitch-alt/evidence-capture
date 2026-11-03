-- 0019_retention (T50): media removed 90 days after the decision or on request.
-- Blobs go; rows stay (items, seals, answers, decision). The receipt keeps the seal verifying.
ALTER TABLE org_settings ADD COLUMN retention_days INTEGER NOT NULL DEFAULT 90;
ALTER TABLE items ADD COLUMN removed_at TEXT;
ALTER TABLE items ADD COLUMN removed_reason TEXT;
ALTER TABLE reports ADD COLUMN removed_at TEXT;
ALTER TABLE claims ADD COLUMN removal_json TEXT;
ALTER TABLE claims ADD COLUMN removal_signature TEXT;
ALTER TABLE claims ADD COLUMN removal_done_at TEXT;
