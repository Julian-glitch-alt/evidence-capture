"""Migration runner checks, against the throwaway test Postgres."""

import psycopg
import pytest

from evidence_server import migrate
from pg import TEST_DATABASE_URL, reset

EXPECTED_TABLES = {
    "orgs",
    "policies",
    "policy_snapshots",
    "claims",
    "claim_events",
    "capture_sessions",
    "slot_nonces",
    "seals",
    "items",
    "login_failures",
    "checks",
    "model_calls",
    "photo_checks",
    "analyses",
    "schema_migrations",
}


@pytest.fixture
def conn():
    db = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    try:
        migrate.apply(db)
        reset(db)
        yield db
    finally:
        db.close()


def seed_claim(db: psycopg.Connection) -> None:
    db.execute("INSERT INTO orgs (id, name, default_tz) VALUES ('org_demo', 'Demo', 'America/New_York')")
    db.execute(
        "INSERT INTO policy_snapshots (id, org_id, policy_no, verified, sha256)"
        " VALUES ('snap1', 'org_demo', 'P1', 1, 'abc')"
    )
    db.execute(
        "INSERT INTO claims (id, org_id, token, template_id, policy_snapshot_id, status,"
        " created_at, expires_at)"
        " VALUES ('CLM-1', 'org_demo', 'tok1', 'loss_notice', 'snap1', 'opened',"
        " '2026-10-28T15:00:00+00:00', '2026-11-04T15:00:00+00:00')"
    )
    db.execute(
        "INSERT INTO claim_events (id, claim_id, seq, at, actor, type, data_json, prev_hash, hash)"
        " VALUES ('ev1', 'CLM-1', 1, '2026-10-28T15:00:00+00:00', 'user', 'claim.opened',"
        " '{}', '0000', 'ffff')"
    )


def test_apply_twice_applies_nothing_second_time(conn):
    assert migrate.apply(conn) == []


def test_expected_tables_exist(conn):
    rows = conn.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public'").fetchall()
    assert EXPECTED_TABLES <= {row[0] for row in rows}


def test_insert_into_claim_events_works(conn):
    seed_claim(conn)
    row = conn.execute("SELECT actor, type FROM claim_events WHERE id = 'ev1'").fetchone()
    assert row == ("user", "claim.opened")


def test_claim_events_rejects_update(conn):
    seed_claim(conn)
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("UPDATE claim_events SET actor = 'x' WHERE id = 'ev1'")


def test_claim_events_rejects_delete(conn):
    seed_claim(conn)
    with pytest.raises(psycopg.Error, match="append-only"):
        conn.execute("DELETE FROM claim_events WHERE id = 'ev1'")


def test_reset_empties_tables_but_keeps_migrations(conn):
    seed_claim(conn)
    reset(conn)
    assert conn.execute("SELECT count(*) FROM claims").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM claim_events").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM schema_migrations").fetchone()[0] >= 1
