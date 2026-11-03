"""Postgres access: a connection pool, one transaction per unit of work, a per-claim lock.

Writes to one claim are serialised by locking that claim's row for the whole
transaction (SELECT ... FOR UPDATE); writes to different claims run in parallel.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from . import migrate
from .errors import CaptureError


class Database:
    def __init__(self, url: str, max_size: int = 10):
        # prepare_threshold=None: Neon's pooled endpoint (PgBouncer) does not keep
        # server-side prepared statements across transactions.
        self.pool = ConnectionPool(
            url,
            min_size=1,
            max_size=max_size,
            kwargs={"row_factory": dict_row, "prepare_threshold": None},
            open=True,
        )

    def migrate(self) -> list[str]:
        with self.pool.connection() as conn:
            return migrate.apply(conn)

    @contextmanager
    def tx(self) -> Iterator[psycopg.Connection]:
        """One transaction: commit when the block ends, roll back if it raises."""
        with self.pool.connection() as conn, conn.transaction():
            yield conn

    @contextmanager
    def claim_tx(
        self, *, claim_id: str | None = None, token: str | None = None
    ) -> Iterator[tuple[psycopg.Connection, dict]]:
        """A transaction holding the lock on one claim's row until it ends."""
        if (claim_id is None) == (token is None):
            raise ValueError("Pass exactly one of claim_id or token")
        column, value = ("id", claim_id) if claim_id is not None else ("token", token)
        with self.tx() as conn:
            claim = conn.execute(f"SELECT * FROM claims WHERE {column} = %s FOR UPDATE", (value,)).fetchone()
            if not claim:
                raise CaptureError("Claim not found" if claim_id is not None else "This link is not valid", 404)
            yield conn, claim

    def close(self) -> None:
        self.pool.close()
