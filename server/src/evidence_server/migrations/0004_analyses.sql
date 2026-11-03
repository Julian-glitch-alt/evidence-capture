CREATE TABLE analyses (
    claim_id TEXT NOT NULL REFERENCES claims(id),
    kind TEXT NOT NULL,                 -- 'damage' (T20)
    item_id TEXT NOT NULL DEFAULT '',   -- the photo for per-photo kinds, '' for per-claim kinds
    status TEXT NOT NULL CHECK (status IN ('pending', 'done', 'failed')),
    result_json TEXT,                   -- what code kept (damage.shape output), only when done
    error TEXT,                         -- why it failed: the meta_client error, or "bad_shape"
    version TEXT NOT NULL,              -- damage.VERSION + "+" + damage_prompts.VERSION
    started_at TEXT NOT NULL,
    finished_at TEXT,
    PRIMARY KEY (claim_id, kind, item_id)
);
