"""T27 decision, T26 queue and T32 clock through the API and into the mock (contract: tasks/t27-t26-t32-design.md)."""

import threading

from conftest import GOOD_DAMAGE, GOOD_STORY
from test_damage_flow import CLOSEUP, WIDE, captured_claim, db_rows, send
from test_demo_insurer import API, new_claim, view, wired  # noqa: F401  (wired is a fixture)
from test_integration_api import capture
from test_loss_notice import detail


def decide(client, claim_id, **body):
    return client.post(f"/api/admin/claims/{claim_id}/decision", json=body)


def ready_claim(client, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    return claim


# --- rules through the API ---


def test_decision_needs_a_ready_proposal(client, model, clock):
    claim, _ = captured_claim(client, clock)
    assert decide(client, claim["id"], action="approve").status_code == 409
    client.app.state.store.submit(claim["token"])  # analysis not run: proposal pending
    assert decide(client, claim["id"], action="approve").status_code == 409


def test_approve_locks_the_claim_and_moves_its_status(client, model, clock):
    claim = ready_claim(client, clock)
    r = decide(client, claim["id"], action="approve", by="J. Smith")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["status"] == "approved"
    # T47: the name comes from the session, never from the body
    assert (d["decision"]["action"], d["decision"]["decided_by"]) == ("approve", "Administrator")
    types = [e["type"] for e in d["events"]]
    assert types[-2:] == ["decision.recorded", "report.reviewed"]  # T40 stores the reviewed report right after
    assert "claim.in_review" in types and "claim.approved" in types
    assert client.app.state.store.verify_chain(claim["id"])["ok"]
    assert decide(client, claim["id"], action="refer_siu", reason="Second thoughts.").status_code == 409


def test_bad_input_is_400(client, model, clock):
    claim = ready_claim(client, clock)
    assert decide(client, claim["id"], action="refer_siu").status_code == 400
    assert decide(client, claim["id"], action="send_back", reason="Need more.", slots=["windshield"]).status_code == 400
    assert db_rows(client, "SELECT * FROM decisions WHERE claim_id=%s", claim["id"]) == []


def test_flagged_claim_needs_a_reason_to_approve(client, model, clock):
    model.set("story", {**GOOD_STORY, "consistent": "no"})
    claim = ready_claim(client, clock)
    assert decide(client, claim["id"], action="approve").status_code == 400
    assert decide(client, claim["id"], action="approve", reason="Viewed Wide #1; damage fits.").status_code == 200


def test_send_back_records_the_photos(client, model, clock):
    claim = ready_claim(client, clock)
    d = decide(client, claim["id"], action="send_back", reason="Odometer glare.", slots=["odometer"]).json()
    assert d["status"] == "more_evidence" and d["decision"]["slots"] == ["odometer"]


def test_concurrent_decisions_store_one(client, model, clock):
    claim = ready_claim(client, clock)
    codes = []
    def go(action, reason):
        codes.append(decide(client, claim["id"], action=action, reason=reason).status_code)
    ts = [threading.Thread(target=go, args=("refer_siu", "Looks staged.")),
          threading.Thread(target=go, args=("field_inspection", "Need to see it."))]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(codes) == [200, 409]
    assert len(db_rows(client, "SELECT * FROM decisions WHERE claim_id=%s", claim["id"])) == 1
    assert len(db_rows(client, "SELECT * FROM outbox WHERE claim_id=%s AND event='decision.recorded'", claim["id"])) == 1


def test_decision_message_is_in_the_outbox(client, model, clock):
    claim = ready_claim(client, clock)
    decide(client, claim["id"], action="refer_siu", reason="Plate does not match.")
    row = db_rows(client, "SELECT * FROM outbox WHERE claim_id=%s AND event='decision.recorded'", claim["id"])[0]
    import json
    body = json.loads(row["body_json"])
    assert body["decision"]["action"] == "refer_siu" and body["complete_activities"] == ["Review verified evidence"]
    assert body["notes"][0]["body"].startswith("Decision by Administrator: Referred to SIU.")
    assert (body["activities"][0]["subject"], body["activities"][0]["priority"]) == ("SIU referral review", "high")
    assert body["activities"][0]["due_date"] == "2026-10-29"


# --- end to end into the mock (D5) ---


def test_decision_lands_in_the_mock_and_closes_the_review(wired, model, clock):
    c = new_claim(wired)
    s = wired.post(f"{API}/claims/{c['claim_number']}/services/verified-capture").json()["services"][0]
    capture(wired, clock, s["capture_url"].rsplit("/", 1)[1])
    r = decide(wired, s["vendor_ref"], action="approve", by="J. Smith")
    assert r.status_code == 200, r.text
    v = view(wired, c["claim_number"])
    review = [a for a in v["activities"] if a["subject"].startswith("Review verified evidence")]
    assert review and review[0]["status"] == "Done"
    assert any(a["subject"] == "Send to estimating (fast track)" and a["status"] == "Open" for a in v["activities"])
    assert any(n["subject"] == "Adjuster decision" and n["body"].startswith("Decision by Administrator: Approved.") for n in v["notes"])
    out = detail(wired, {"id": s["vendor_ref"]})
    msg = [m for m in out["report"]["outbox"] if m["event"] == "decision.recorded"][0]
    assert msg["delivered_at"] is not None


# --- queue (T26) and clock (T32) ---


def test_queue_groups_claims_by_lane(client, model, clock):
    fast = ready_claim(client, clock)
    # other photos and another reading, so the replay cache does not hand back the first claim's answers
    model.set("damage", {**GOOD_DAMAGE, "damages": [{**GOOD_DAMAGE["damages"][0], "severity": "severe"}]})
    model.set("story", {**GOOD_STORY, "consistent": "no"})
    review, _ = captured_claim(client, clock, photos={"damage_closeup": [CLOSEUP + b"-2"], "damage_wide": [WIDE + b"-2"]})
    send(client, review)
    waiting, _ = captured_claim(client, clock)
    q = client.get("/api/admin/queue").json()
    groups = {g["key"]: g for g in q["groups"]}
    assert [g["key"] for g in q["groups"]] == ["siu_referral", "adjuster_review", "fast_track", "standard",
                                              "analysing", "capturing", "decided"]
    assert [c["id"] for c in groups["fast_track"]["claims"]] == [fast["id"]]
    assert [c["id"] for c in groups["adjuster_review"]["claims"]] == [review["id"]]
    assert waiting["id"] in [c["id"] for c in groups["capturing"]["claims"]]
    assert groups["fast_track"]["count"] == 1 and groups["siu_referral"]["count"] == 0
    decide(client, fast["id"], action="approve")
    q = client.get("/api/admin/queue").json()
    groups = {g["key"]: g for g in q["groups"]}
    assert [c["id"] for c in groups["decided"]["claims"]] == [fast["id"]] and groups["fast_track"]["count"] == 0


def test_cycle_on_the_claim_and_averages_on_the_queue(client, model, clock):
    claim, _ = captured_claim(client, clock)
    clock.advance(minutes=3)
    send(client, claim)
    clock.advance(minutes=5)
    d = decide(client, claim["id"], action="approve").json()
    cyc = d["cycle"]
    assert cyc["link_to_submit"] >= 180 and cyc["report_to_decision"] == 300
    assert cyc["link_to_decision"] == cyc["link_to_submit"] + cyc["submit_to_report"] + cyc["report_to_decision"]
    avg = client.get("/api/admin/queue").json()["averages"]
    assert avg["link_to_decision"] == {"mean_s": cyc["link_to_decision"], "n": 1}


def test_queue_needs_login(anon, client):
    client.post("/api/admin/logout")
    assert anon.get("/api/admin/queue").status_code == 401
    assert anon.post("/api/admin/claims/CLM-x/decision", json={"action": "approve"}).status_code == 401
