"""Track 3: durable background work. The sweeper re-runs stale analyses, rebuilds missing
reports and retries undelivered outbox messages with backoff; all on the fake clock."""

from evidence_server import report_pdf, store as store_mod
from test_damage_flow import captured_claim, damage_calls, db_rows
from test_integration_api import BASE, capture, order, route  # noqa: F401
from test_loss_notice import detail


def submit_without_background(client, claim):
    """The claim is submitted and sealed, but the container dies before the background jobs run."""
    client.app.state.store.submit(claim["token"])


def sweep(client):
    r = client.post("/api/admin/sweep")
    assert r.status_code == 200, r.text
    return r.json()


def outbox(client, claim_id):
    return db_rows(client, "SELECT * FROM outbox WHERE claim_id=%s ORDER BY created_at", claim_id)


# --- stale analyses ---


def test_fresh_pending_work_is_left_alone(client, model, clock):
    claim, _ = captured_claim(client, clock)
    submit_without_background(client, claim)
    clock.advance(minutes=4)
    out = sweep(client)
    assert out["analyses_rerun"] == 0 and out["reports_built"] == 0
    assert damage_calls(model) == []


def test_stale_analysis_is_rerun_and_the_report_built(client, model, clock):
    claim, _ = captured_claim(client, clock)
    submit_without_background(client, claim)
    assert detail(client, claim)["report"] is None
    clock.advance(minutes=6)
    out = sweep(client)
    assert out["analyses_rerun"] == 3  # two damage photos + the consistency check
    assert len(damage_calls(model)) == 2
    d = detail(client, claim)
    assert d["damage"]["status"] == "done" and d["consistency"]["status"] == "done"
    assert d["report"] is not None and d["proposal"]["status"] == "ready"
    rows = db_rows(client, "SELECT attempts FROM analyses WHERE claim_id=%s", claim["id"])
    assert {r["attempts"] for r in rows} == {2}
    # Nothing left to do: a second sweep is a no-op.
    assert sweep(client) == {k: 0 for k in out}


def test_analysis_gives_up_after_the_last_attempt(client, model, clock, monkeypatch):
    claim, _ = captured_claim(client, clock)
    submit_without_background(client, claim)
    store = client.app.state.store
    # Every re-run dies too: the rows stay pending.
    monkeypatch.setattr(store, "_run_damage", lambda claim_id: None)
    monkeypatch.setattr(store, "_run_consistency", lambda claim_id: None)
    for _ in range(store_mod.ANALYSIS_MAX_ATTEMPTS - 1):
        clock.advance(minutes=6)
        assert sweep(client)["analyses_rerun"] == 3
    clock.advance(minutes=6)
    out = sweep(client)
    assert out["analyses_rerun"] == 0 and out["analyses_given_up"] == 3
    rows = db_rows(client, "SELECT status, error FROM analyses WHERE claim_id=%s", claim["id"])
    assert {(r["status"], r["error"]) for r in rows} == {("failed", "stale")}
    d = detail(client, claim)
    # Missing signals are shown as failed, never as a zero; the report still goes out.
    assert d["damage"]["status"] == "failed" and d["consistency"]["status"] == "failed"
    assert d["report"] is not None


def test_two_sweepers_never_run_the_same_row_twice(client, model, clock):
    claim, _ = captured_claim(client, clock)
    submit_without_background(client, claim)
    clock.advance(minutes=6)
    store = client.app.state.store
    started = db_rows(client, "SELECT started_at FROM analyses WHERE claim_id=%s AND kind='damage'", claim["id"])
    sweep(client)
    # A second worker that read the same stale rows cannot claim them any more.
    with store.db.tx() as conn:
        claimed = conn.execute(
            "UPDATE analyses SET attempts=attempts+1 WHERE claim_id=%s AND kind='damage' AND started_at=%s",
            (claim["id"], started[0]["started_at"]),
        ).rowcount
    assert claimed == 0


def test_stale_photo_check_is_rerun(client, model, clock):
    claim, items = captured_claim(client, clock)
    item_id = items["damage_closeup"][0]["id"]
    store = client.app.state.store
    with store.db.tx() as conn:  # as if the container died mid-check
        conn.execute("UPDATE photo_checks SET status='pending', readings_json=NULL, finished_at=NULL WHERE item_id=%s",
                     (item_id,))
    clock.advance(minutes=6)
    out = sweep(client)
    assert out["photo_checks_rerun"] == 1  # the cached model answer is reused: no new spend
    status = db_rows(client, "SELECT status, attempts FROM photo_checks WHERE item_id=%s", item_id)[0]
    assert (status["status"], status["attempts"]) == ("done", 2)


# --- missing reports ---


def test_missing_report_is_rebuilt(client, model, clock, monkeypatch):
    claim, _ = captured_claim(client, clock)
    real = report_pdf.render
    monkeypatch.setattr(report_pdf, "render", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("font missing")))
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 200
    assert detail(client, claim)["report"] is None  # both background jobs failed to report
    monkeypatch.setattr(report_pdf, "render", real)
    out = sweep(client)
    assert out["reports_built"] == 1 and out["analyses_rerun"] == 0
    assert detail(client, claim)["report"] is not None
    assert sweep(client)["reports_built"] == 0


# --- outbox retries ---


def test_failed_delivery_backs_off_then_goes_dead(client, model, clock):
    sent = []
    client.app.state.store.http = route(client, sent)
    client.app.state.store.http.status[0] = 503
    c = order(client).json()
    capture(client, clock, c["capture_url"].rsplit("/", 1)[1])
    row = outbox(client, c["case_id"])[0]
    assert row["attempts"] == 1 and row["dead_at"] is None
    waits = []
    for attempt in range(2, store_mod.OUTBOX_MAX_ATTEMPTS + 1):
        wait = store_mod.outbox_backoff(attempt - 1)
        waits.append(int(wait.total_seconds()))
        clock.advance(seconds=wait.total_seconds() - 1)
        assert sweep(client)["delivery_failed"] == 0  # not due yet
        clock.advance(seconds=1)
        assert sweep(client)["delivery_failed"] == 1
        assert outbox(client, c["case_id"])[0]["attempts"] == attempt
    assert waits == [30, 60, 120, 240, 480, 960, 1920]
    row = outbox(client, c["case_id"])[0]
    assert row["dead_at"] is not None and row["delivered_at"] is None and "503" in row["last_error"]
    clock.advance(hours=5)
    assert sweep(client)["delivery_failed"] == 0 and len(sent) == store_mod.OUTBOX_MAX_ATTEMPTS
    # The adjuster's Resend still works on a dead message.
    client.app.state.store.http.status[0] = 200
    assert client.post(f"/api/admin/claims/{c['case_id']}/deliver").json() == {"ok": True}
    row = outbox(client, c["case_id"])[0]
    assert row["delivered_at"] is not None and row["dead_at"] is None


def test_sweeper_delivers_once_the_insurer_is_back(client, model, clock):
    sent = []
    client.app.state.store.http = route(client, sent)
    client.app.state.store.http.status[0] = 500
    c = order(client).json()
    capture(client, clock, c["capture_url"].rsplit("/", 1)[1])
    client.app.state.store.http.status[0] = 200
    clock.advance(seconds=31)
    out = sweep(client)
    assert out["delivered"] == 1
    assert outbox(client, c["case_id"])[0]["delivered_at"] is not None
    assert [s["headers"]["X-Evidence-Delivery"] for s in sent] == [outbox(client, c["case_id"])[0]["id"]] * 2


def test_leased_message_is_not_posted_twice(client, model, clock):
    sent = []
    client.app.state.store.http = route(client, sent)
    client.app.state.store.http.status[0] = 500
    c = order(client).json()
    capture(client, clock, c["capture_url"].rsplit("/", 1)[1])
    clock.advance(seconds=31)
    store = client.app.state.store
    # Another worker has just leased the message (moved next_attempt_at forward).
    with store.db.tx() as conn:
        conn.execute("UPDATE outbox SET next_attempt_at=%s WHERE claim_id=%s",
                     ((clock.now() + store_mod.OUTBOX_LEASE).isoformat(), c["case_id"]))
    before = len(sent)
    assert store.deliver(c["case_id"]) == {"delivered": 0, "failed": 0}
    assert len(sent) == before


def test_later_message_waits_for_an_earlier_failed_one(client, model, clock):
    sent = []
    client.app.state.store.http = route(client, sent)
    client.app.state.store.http.status[0] = 500
    c = order(client).json()
    capture(client, clock, c["capture_url"].rsplit("/", 1)[1])
    client.app.state.store.http.status[0] = 200
    clock.advance(seconds=5)
    r = client.post(f"/api/admin/claims/{c['case_id']}/decision", json={"action": "approve", "by": "J. Smith"})
    assert r.status_code == 200, r.text
    rows = outbox(client, c["case_id"])
    assert [(m["event"], m["delivered_at"] is None) for m in rows] == [
        ("evidence.ready", True), ("decision.recorded", True)]  # held back, in order
    clock.advance(seconds=30)
    assert sweep(client)["delivered"] == 2
    events = [s["headers"]["X-Evidence-Event"] for s in sent]
    assert events == ["evidence.ready", "evidence.ready", "decision.recorded"]


def test_sweep_needs_login(anon):
    assert anon.post("/api/admin/sweep").status_code == 401


def test_backoff_is_capped():
    assert store_mod.outbox_backoff(1).total_seconds() == 30
    assert store_mod.outbox_backoff(30) == store_mod.OUTBOX_BACKOFF_CAP
