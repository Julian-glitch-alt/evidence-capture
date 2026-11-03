"""T46 (tasks/t46-design.md): CI, the sweeper on a schedule, logged errors, System health."""

import logging
from pathlib import Path

from test_damage_flow import captured_claim, db_rows, send

ROOT = Path(__file__).parents[2]


def health(client):
    r = client.get("/api/admin/health")
    assert r.status_code == 200, r.text
    return r.json()


def execute(client, sql, *args):
    with client.app.state.store.db.tx() as conn:
        conn.execute(sql, args)


# --- CI and the schedule ---


def test_ci_runs_the_server_tests_on_postgres_and_builds_the_app():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "postgres:16" in ci and "pg_isready" in ci
    assert "TEST_DATABASE_URL" in ci and "uv run pytest" in ci
    assert "npm ci" in ci and "npm run build" in ci and "npm run lint" in ci
    assert "secrets." not in ci  # nothing secret, nothing deploys from CI


def test_the_sweeper_runs_every_two_minutes_on_modal():
    deploy = (ROOT / "server/deploy/modal_app.py").read_text()
    assert 'modal.Cron("*/2 * * * *")' in deploy
    assert ".sweep()" in deploy


# --- health ---


def test_health_needs_login(anon):
    assert anon.get("/api/admin/health").status_code == 401


def test_a_quiet_system_reports_zeros_and_no_rate(client):
    h = health(client)
    assert h["window_hours"] == 24
    assert (h["analyses_failed"], h["photo_checks_failed"]) == (0, 0)
    assert h["outbox"] == {"dead": 0, "retrying": 0, "delivered": 0}
    assert h["model_calls"]["total"] == 0 and h["model_calls"]["error_rate"] is None
    assert h["last_sweep"] is None and h["claims"] == []


def test_failures_are_counted_and_point_to_their_claims(client, model, clock):
    model.set("damage", RuntimeError("Meta is down"))
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    h = health(client)
    assert h["analyses_failed"] >= 1
    assert h["model_calls"]["errors"] >= 1 and 0 < h["model_calls"]["error_rate"] <= 1
    assert d["id"] in h["claims"]


def test_dead_and_retrying_messages(client, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    now = clock.now().isoformat()
    execute(client, "INSERT INTO outbox (id, claim_id, event, body_json, created_at, attempts, dead_at)"
            " VALUES ('ob-dead', %s, 'test.dead', '{}', %s, 8, %s)", claim["id"], now, now)
    execute(client, "INSERT INTO outbox (id, claim_id, event, body_json, created_at, attempts)"
            " VALUES ('ob-retry', %s, 'test.retry', '{}', %s, 2)", claim["id"], now)
    h = health(client)
    assert h["outbox"]["dead"] == 1 and h["outbox"]["retrying"] >= 1
    assert claim["id"] in h["claims"]


def test_old_failures_leave_the_window(client, model, clock):
    model.set("damage", RuntimeError("Meta is down"))
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    clock.advance(hours=25)
    h = health(client)
    assert h["analyses_failed"] == 0 and h["model_calls"]["total"] == 0


def test_each_sweep_is_recorded_and_shown(client):
    r = client.post("/api/admin/sweep")
    assert r.status_code == 200, r.text
    last = health(client)["last_sweep"]
    assert last["at"] and last["error"] is None
    assert "delivered" in last["result"]
    assert len(db_rows(client, "SELECT id FROM ops_runs WHERE kind='sweep'")) == 1


# --- errors are logged with the claim, never swallowed ---


def test_a_failing_report_is_logged_with_the_claim(client, clock, caplog, monkeypatch):
    from evidence_server import report

    def broken(*args, **kwargs):
        raise RuntimeError("render broke")

    monkeypatch.setattr(report, "build", broken)
    claim, _ = captured_claim(client, clock)
    with caplog.at_level(logging.ERROR):
        send(client, claim)
    hits = [r for r in caplog.records if r.levelno >= logging.ERROR and claim["id"] in r.getMessage()]
    assert hits and hits[0].exc_info is not None


def test_a_failing_statement_check_is_logged_with_the_claim(client, clock, caplog, monkeypatch):
    from evidence_server import consistency

    def broken(*args, **kwargs):
        raise RuntimeError("story broke")

    monkeypatch.setattr(consistency, "story_block", broken)
    claim, _ = captured_claim(client, clock)
    with caplog.at_level(logging.ERROR):
        d = send(client, claim)
    assert any(r.levelno >= logging.ERROR and claim["id"] in r.getMessage() for r in caplog.records)
    assert d["proposal"]["status"] == "ready"  # still never raises: the claim goes on
