-- 0014_damage_outlines (track 6): SAM 3.1's outline of each damage Muse Spark boxed.
-- One row per damage listing (item_id + its number in that photo's reading, the
-- damage key f"{item_id}:{number}"). Written with the damage reading; never read
-- by the proposal, the estimate or routing.
CREATE TABLE damage_outlines (
    claim_id TEXT NOT NULL REFERENCES claims(id),
    item_id TEXT NOT NULL,
    number INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('done', 'not_run', 'failed')),
    result_json TEXT NOT NULL,          -- outline.py result: concept, sam_agrees, regions, area, call_id
    error TEXT,                         -- why SAM did not run or failed (capped, no_box, image_unreadable, ...)
    version TEXT NOT NULL,              -- outline.VERSION
    created_at TEXT NOT NULL,
    PRIMARY KEY (claim_id, item_id, number)
);
