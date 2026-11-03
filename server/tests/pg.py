"""Postgres helpers for tests: which database, and how to empty it."""

import os

import psycopg

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql://postgres:test@127.0.0.1:55432/claims_test"
)


def reset(conn: psycopg.Connection) -> None:
    """Empty every table except `schema_migrations`; restart owned sequences."""
    rows = conn.execute(
        "SELECT table_name FROM information_schema.tables"
        " WHERE table_schema = 'public'"
        " AND table_type = 'BASE TABLE'"
        " AND table_name <> 'schema_migrations'"
    ).fetchall()
    tables = ", ".join(f'"{row[0]}"' for row in rows)
    if tables:
        conn.execute(f"TRUNCATE {tables} RESTART IDENTITY CASCADE")
    conn.commit()
