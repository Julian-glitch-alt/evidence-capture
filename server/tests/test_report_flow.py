"""T29a report, secret link and outbox through the API (contract: tasks/t29a-design.md), with a fake model."""

import hashlib
import threading

from evidence_server import report
from test_damage_flow import captured_claim, db_rows, send
from test_loss_notice import detail

PRIVATE = {"x-robots-tag": "noindex, nofollow", "cache-control": "private, no-store", "referrer-policy": "no-referrer"}


def token_of(d):
    return d["report"]["url"].rsplit("/", 1)[1]


def test_sent_claim_gets_one_report_and_one_ready_message(client, model, clock):
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    r = d["report"]
    assert r["url"].startswith("/api/r/") and r["version"] == report.VERSION
    assert len(token_of(d)) >= 43
    assert [m["event"] for m in r["outbox"]] == ["evidence.ready"]
    body = r["outbox"][0]["body"]
    assert (body["schema"], body["event"], body["claim"]["case_id"]) == (report.PACKAGE_SCHEMA, "evidence.ready", claim["id"])
    assert body["recommendation"]["lane"] == "fast_track"
    assert body["notes"][0]["body"].startswith("Verified capture complete")
    assert body["activities"][0]["subject"] == "Review verified evidence: fast track suggested"
    assert r["outbox"][0]["delivered_at"] is None


def test_pdf_bytes_match_the_package_hash_and_the_logged_event(anon, client, model, clock):
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    pdf = anon.get(d["report"]["url"])
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert pdf.headers["content-type"] == "application/pdf"
    sha = hashlib.sha256(pdf.content).hexdigest()
    assert d["report"]["sha256"] == sha
    assert d["report"]["outbox"][0]["body"]["documents"][0]["sha256"] == sha
    ready = [e for e in d["events"] if e["type"] == "report.ready"]
    assert len(ready) == 1 and ready[0]["data"]["sha256"] == sha and ready[0]["actor"] == "system"
    assert client.app.state.store.verify_chain(claim["id"])["ok"]


def test_no_report_before_submit_or_while_the_analysis_runs(client, model, clock):
    claim, _ = captured_claim(client, clock)
    assert detail(client, claim)["report"] is None
    client.app.state.store.submit(claim["token"])  # without the background run
    client.app.state.store.make_report(claim["id"])  # proposal still pending
    assert detail(client, claim)["report"] is None
    assert db_rows(client, "SELECT * FROM outbox WHERE claim_id=%s", claim["id"]) == []


def test_report_is_made_once_even_when_both_jobs_finish_together(client, model, clock):
    claim, _ = captured_claim(client, clock)
    store = client.app.state.store
    store.submit(claim["token"])
    jobs = [threading.Thread(target=store.run_damage, args=(claim["id"],)),
            threading.Thread(target=store.run_consistency, args=(claim["id"],))]
    for j in jobs:
        j.start()
    for j in jobs:
        j.join()
    store.make_report(claim["id"])
    store.make_report(claim["id"])
    assert len(db_rows(client, "SELECT * FROM reports WHERE claim_id=%s", claim["id"])) == 1
    assert len(db_rows(client, "SELECT * FROM outbox WHERE claim_id=%s", claim["id"])) == 1
    assert len([e for e in detail(client, claim)["events"] if e["type"] == "report.ready"]) == 1


def test_model_down_still_reports_for_review(client, model, clock):
    model.set("damage", RuntimeError("Meta is down"))
    claim, _ = captured_claim(client, clock)
    body = send(client, claim)["report"]["outbox"][0]["body"]
    assert body["recommendation"]["lane"] == "adjuster_review"
    assert body["recommendation"]["estimate"]["status"] == "none"


# --- the secret link ---


def test_secret_link_needs_no_login_and_is_private(anon, client, model, clock):
    claim, _ = captured_claim(client, clock)
    url = send(client, claim)["report"]["url"]
    r = anon.get(url)
    assert r.status_code == 200
    assert 'filename="verified-evidence-report.pdf"' in r.headers["content-disposition"]
    assert r.headers["content-disposition"].startswith("inline")
    for k, v in PRIVATE.items():
        assert r.headers[k] == v


def test_secret_link_serves_the_sealed_package(anon, client, model, clock):
    claim, _ = captured_claim(client, clock)
    url = send(client, claim)["report"]["url"]
    r = anon.get(url + "/package")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    assert f'{claim["id"]}-sealed.zip' in r.headers["content-disposition"]
    for k, v in PRIVATE.items():
        assert r.headers[k] == v


def test_unknown_or_claimant_token_opens_nothing(anon, client, model, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    assert anon.get("/api/r/" + "A" * 43).status_code == 404
    assert anon.get(f"/api/r/{claim['token']}").status_code == 404
    assert anon.get(f"/api/r/{claim['token']}/package").status_code == 404


def test_link_expires_after_thirty_days(anon, client, model, clock):
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    clock.advance(days=report.LINK_DAYS, minutes=1)
    assert anon.get(d["report"]["url"]).status_code == 410
    assert anon.get(d["report"]["url"] + "/package").status_code == 410
    assert client.get(f"/api/admin/claims/{claim['id']}/report").status_code == 200  # the adjuster still has it


def test_admin_report_needs_login(anon, client, model, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    r = client.get(f"/api/admin/claims/{claim['id']}/report")
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    client.post("/api/admin/logout")
    assert anon.get(f"/api/admin/claims/{claim['id']}/report").status_code == 401


def test_admin_report_is_404_before_it_exists(client, model, clock):
    claim, _ = captured_claim(client, clock)
    assert client.get(f"/api/admin/claims/{claim['id']}/report").status_code == 404


def test_the_claimant_never_sees_the_report(client, model, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    state = client.get(f"/api/c/{claim['token']}").json()
    assert "report" not in state and "outbox" not in str(state)
