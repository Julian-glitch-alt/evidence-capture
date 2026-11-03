"""Per-IP request limits on the public surface (track 3).

Fixed one-minute windows counted in Postgres, so every container enforces the same
limit (the login limit in auth.py works the same way). One statement per request.
The limits are generous: a claimant's phone polls every 2 s while a photo check runs
and uploads about ten shots, and mobile carriers put many phones behind one IP.
If the database cannot be reached the request goes through (availability first);
the error is logged.
"""

import logging
from collections.abc import Callable

from .db import Database

logger = logging.getLogger(__name__)

WINDOW_S = 60
# bucket -> requests per IP per window
LIMITS: dict[str, int] = {
    "capture": 300,   # /api/c/*: the claimant's capture link
    "classify": 20,   # POST /api/c/*/classify: the loss-type suggestion
    "verify": 10,     # POST /api/verify: reads a package of up to 200 MB
    "api": 120,       # /api/v1/*: the insurer's integration API
    "webhook": 120,   # the mock claims system's webhook
}
KEEP_WINDOWS_S = 3600  # the sweeper deletes older counters


def bucket_for(method: str, path: str) -> str | None:
    """Which limit applies to a request, or None for routes without one."""
    if method == "POST" and path.startswith("/api/c/") and path.endswith("/classify"):
        return "classify"
    if path.startswith("/api/c/"):
        return "capture"
    if path == "/api/verify" and method == "POST":
        return "verify"
    if path.startswith("/api/v1/"):
        return "api"
    if path == "/api/demo-insurer/webhook":
        return "webhook"
    return None


class RateLimiter:
    def __init__(self, db: Database, now: Callable[[], float], limits: dict[str, int] | None = None):
        self.db = db
        self.now = now
        self.limits = dict(LIMITS if limits is None else limits)

    def hit(self, bucket: str, ip: str) -> int | None:
        """Count one request; -> seconds to wait when over the limit, else None."""
        limit = self.limits.get(bucket)
        if not limit:
            return None
        now = self.now()
        window = int(now // WINDOW_S) * WINDOW_S
        try:
            with self.db.tx() as conn:
                row = conn.execute(
                    "INSERT INTO rate_limits (bucket, ip, window_start, hits) VALUES (%s, %s, %s, 1)"
                    " ON CONFLICT (bucket, ip, window_start) DO UPDATE SET hits = rate_limits.hits + 1"
                    " RETURNING hits",
                    (bucket, ip, window),
                ).fetchone()
        except Exception:
            logger.exception("rate limit check failed; letting the request through")
            return None
        if row["hits"] > limit:
            return max(1, int(window + WINDOW_S - now))
        return None

    def prune(self) -> int:
        """Delete counters older than KEEP_WINDOWS_S; -> rows deleted."""
        with self.db.tx() as conn:
            return conn.execute(
                "DELETE FROM rate_limits WHERE window_start < %s", (int(self.now()) - KEEP_WINDOWS_S,)
            ).rowcount
