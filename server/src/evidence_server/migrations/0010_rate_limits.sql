-- 0010_rate_limits (track 3): fixed-window request counters per IP, shared by every container.
CREATE TABLE rate_limits (
    bucket TEXT NOT NULL,
    ip TEXT NOT NULL,
    window_start BIGINT NOT NULL,
    hits INTEGER NOT NULL,
    PRIMARY KEY (bucket, ip, window_start)
);
