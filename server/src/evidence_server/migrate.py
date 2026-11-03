"""Postgres schema migrations for the evidence server.

Every `migrations/NNNN_*.sql` file runs once, in name order. A `schema_migrations`
table records what already ran. All of it runs in one transaction holding an
advisory lock, so two containers starting together cannot race, and the lock is
released at commit (safe behind Neon's pooled, transaction-mode endpoint).
"""

from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg.rows import tuple_row

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
LOCK_KEY = "evidence_server_migrations"


def apply(conn: psycopg.Connection) -> list[str]:
    """Apply pending migrations in order; return the versions applied."""
    done: list[str] = []
    with conn.transaction(), conn.cursor(row_factory=tuple_row) as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (LOCK_KEY,))
        cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT)")
        cur.execute("SELECT version FROM schema_migrations")
        have = {row[0] for row in cur.fetchall()}
        for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
            if path.stem in have:
                continue
            cur.execute(path.read_text())
            cur.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (%s, %s)",
                (path.stem, datetime.now(UTC).isoformat()),
            )
            done.append(path.stem)
    return done
