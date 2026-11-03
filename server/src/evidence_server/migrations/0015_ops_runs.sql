-- 0015_ops_runs (t46): one row per sweeper run; the System health page shows the latest.
CREATE TABLE ops_runs (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,             -- 'sweep'
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    result_json TEXT NOT NULL,      -- the sweep's counts
    error TEXT                      -- "step: error" when a sweep step raised, else NULL
);
CREATE INDEX ops_runs_kind_finished ON ops_runs (kind, finished_at DESC);
