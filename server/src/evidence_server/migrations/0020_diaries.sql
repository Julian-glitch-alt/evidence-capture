-- 0020_diaries (T52): follow-up diaries per claim; due diaries show on Today.
CREATE TABLE diaries (
    id TEXT PRIMARY KEY,
    claim_id TEXT NOT NULL REFERENCES claims(id),
    due_on TEXT NOT NULL,             -- "YYYY-MM-DD"
    reason TEXT NOT NULL,
    created_by TEXT,                  -- the signed-in user's name
    created_at TEXT NOT NULL,
    done_at TEXT,                     -- NULL while open
    done_by TEXT                      -- the signed-in user's name
);
CREATE INDEX diaries_claim ON diaries (claim_id);
