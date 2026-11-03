CREATE TABLE decisions (
    claim_id TEXT PRIMARY KEY REFERENCES claims(id),
    action TEXT NOT NULL,
    lane_before TEXT, lane_after TEXT,
    reserve_before INTEGER, reserve_after INTEGER,
    reason TEXT, slots_json TEXT NOT NULL DEFAULT '[]',
    decided_by TEXT NOT NULL, decided_at TEXT NOT NULL,
    version TEXT NOT NULL
);
