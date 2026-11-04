"""Track 3: Send back reopens the claimant link for the requested photos only; the
resubmission is round 2, sealed, analysed, reported and decided again. Through the
API and the mock claims system."""

import threading

from conftest import JPEG, Phone
from test_damage_flow import captured_claim, damage_calls, db_rows, send
from test_decision_flow import decide
from test_demo_insurer import API, new_claim, view, wired  # noqa: F401  (wired is a fixture)
from test_integration_api import capture
from test_loss_notice import detail
from test_review_flow import set_mode

NEW_WIDE = JPEG + b"wide-retaken"


def sent_back(client, clock, slots=("damage_wide",)):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    clock.advance(minutes=5)
    r = decide(client, claim["id"], action="send_back", reason="The wide shot is too dark.", slots=list(slots))
    assert r.status_code == 200, r.text
    return claim


def resubmit(client, clock, claim, slot="damage_wide", data=NEW_WIDE):
    phone = Phone(client, claim["token"])
    clock.advance(seconds=5)
    assert phone.upload(slot, data).status_code == 200
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 200, r.text
    return phone


def test_opening_a_sent_back_link_reopens_it_for_the_requested_photos(client, model, clock):
    claim = sent_back(client, clock)
    assert detail(client, claim)["status"] == "more_evidence"
    state = client.get(f"/api/c/{claim['token']}").json()
    assert state["status"] == "capturing" and state["requested_slots"] == ["damage_wide"]
    d = detail(client, claim)
    assert d["status"] == "capturing" and d["requested_slots"] == ["damage_wide"]
    reopened = [e for e in d["events"] if e["type"] == "claim.capturing"][-1]
    assert reopened["from_status"] == "more_evidence" and reopened["data"] == {"reopened_for": ["damage_wide"], "round": 2}
    assert d["decision"]["action"] == "send_back"  # still round 1 until the claimant resubmits
    phone = Phone(client, claim["token"])
    assert phone.session["slots"] == ["damage_wide"]
    r = client.post(f"/api/c/{claim['token']}/nonce", json={"session_id": phone.session["session_id"], "slot_id": "odometer"})
    assert r.status_code == 403 and "asked for" in r.json()["detail"]
    assert client.app.state.store.verify_chain(claim["id"])["ok"]


def test_resubmit_needs_the_requested_retakes(client, model, clock):
    claim = sent_back(client, clock, slots=("damage_wide", "odometer"))
    Phone(client, claim["token"])
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 400 and r.json()["detail"] == "Please retake: Damage in context, Odometer"
    phone = Phone(client, claim["token"])
    clock.advance(seconds=5)
    assert phone.upload("damage_wide", NEW_WIDE).status_code == 200
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.json()["detail"] == "Please retake: Odometer"


def test_round_two_is_sealed_analysed_reported_and_decided_again(client, model, clock):
    claim = sent_back(client, clock)
    first = detail(client, claim)
    token_before = first["report"]["url"]
    reads_before = len(damage_calls(model))
    resubmit(client, clock, claim)
    d = detail(client, claim)
    assert d["round"] == 2 and d["requested_slots"] == [] and d["decision"] is None
    assert [e["action"] for e in d["earlier_decisions"]] == ["send_back"] and d["earlier_decisions"][0]["round"] == 1
    # Sealed again, naming the first seal; the package verifies.
    seals = db_rows(client, "SELECT round, manifest_json FROM seals WHERE claim_id=%s ORDER BY round", claim["id"])
    assert [s["round"] for s in seals] == [1, 2]
    import hashlib, json
    m2 = json.loads(seals[1]["manifest_json"])
    assert m2["round"] == 2 and m2["previous_manifest_sha256"] == hashlib.sha256(seals[0]["manifest_json"].encode()).hexdigest()
    assert NEW_WIDE != b"" and any(i["slot"] == "damage_wide" for i in m2["items"])
    package = client.get(f"/api/admin/claims/{claim['id']}/package").content
    assert client.post("/api/verify", files={"file": ("p.zip", package, "application/zip")}).json()["valid"] is True
    # Only the new photo was read again; the unchanged close-up kept its reading.
    assert len(damage_calls(model)) == reads_before + 1 and damage_calls(model)[-1]["image"] == NEW_WIDE
    assert d["damage"]["status"] == "done" and d["consistency"]["status"] == "done"
    # A new report behind the same secret link, and a round-2 message.
    assert d["report"]["round"] == 2 and d["report"]["url"] == token_before
    assert d["report"]["sha256"] != first["report"]["sha256"]
    ready = [m for m in d["report"]["outbox"] if m["event"] == "evidence.ready"]
    assert [m["round"] for m in ready] == [1, 2]
    assert ready[1]["body"]["round"] == 2 and ready[1]["body"]["complete_activities"] == ["Follow up on requested photos"]
    # The adjuster decides round 2; round 1's decision stays as history.
    q = client.get("/api/admin/queue").json()
    assert claim["id"] in [c["id"] for g in q["groups"] if g["key"] not in ("decided", "capturing") for c in g["claims"]]
    r = decide(client, claim["id"], action="approve", by="J. Smith")
    assert r.status_code == 200, r.text
    rows = db_rows(client, "SELECT round, action FROM decisions WHERE claim_id=%s ORDER BY round", claim["id"])
    assert [(x["round"], x["action"]) for x in rows] == [(1, "send_back"), (2, "approve")]
    assert r.json()["status"] == "approved" and client.app.state.store.verify_chain(claim["id"])["ok"]
    assert decide(client, claim["id"], action="approve").status_code == 409


def test_answers_stay_as_sealed_in_round_two(client, model, clock):
    claim = sent_back(client, clock)
    before = detail(client, claim)["answers"]
    state = client.get(f"/api/c/{claim['token']}").json()
    changed = {**state["answers"], "description": "Something else entirely."}
    r = client.put(f"/api/c/{claim['token']}/draft", json={"answers": changed})
    assert r.status_code == 200 and r.json()["answers"] == before
    resubmit(client, clock, claim)
    assert detail(client, claim)["answers"] == before


def test_queue_shows_a_sent_back_claim_as_waiting_for_the_claimant(client, model, clock):
    claim = sent_back(client, clock)
    q = client.get("/api/admin/queue").json()
    groups = {g["key"]: [c["id"] for c in g["claims"]] for g in q["groups"]}
    assert groups["capturing"] == [claim["id"]] and groups["decided"] == []


def test_send_back_gives_the_link_a_fresh_lifetime(client, model, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    clock.advance(days=6)
    assert decide(client, claim["id"], action="send_back", reason="Odometer glare.", slots=["odometer"]).status_code == 200
    clock.advance(days=4)  # past the original 7 days
    assert client.get(f"/api/c/{claim['token']}").json()["status"] == "capturing"


def test_reopen_happens_once_under_concurrent_opens(client, model, clock):
    claim = sent_back(client, clock)
    codes = []
    ts = [threading.Thread(target=lambda: codes.append(client.get(f"/api/c/{claim['token']}").status_code))
          for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert codes == [200] * 4
    events = [e for e in detail(client, claim)["events"] if e["type"] == "claim.capturing"]
    assert len(events) == 2  # the first capture and one reopen


def test_round_two_review_starts_empty(client, model, clock):
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    key = d["damage"]["damages"][0]["key"]
    r = client.post(f"/api/admin/claims/{claim['id']}/review",
                    json={"kind": "damage", "key": key, "verdict": "excluded", "reason": "Old scratch, not this loss."})
    assert r.status_code == 200
    assert decide(client, claim["id"], action="send_back", reason="Need a wider shot.", slots=["damage_wide"]).status_code == 200
    resubmit(client, clock, claim)
    d = detail(client, claim)
    assert d["review"] == {"flags": {}, "damages": {}, "notes": []}
    assert len(db_rows(client, "SELECT * FROM reviews WHERE claim_id=%s AND round=1", claim["id"])) == 1


def test_mock_claims_system_follows_both_rounds(wired, model, clock):
    c = new_claim(wired)
    s = wired.post(f"{API}/claims/{c['claim_number']}/services/verified-capture").json()["services"][0]
    token = s["capture_url"].rsplit("/", 1)[1]
    capture(wired, clock, token)
    case = s["vendor_ref"]
    clock.advance(minutes=1)  # the fake clock otherwise stamps the report and the decision alike
    assert decide(wired, case, action="send_back", reason="The wide shot is too dark.", slots=["damage_wide"]).status_code == 200
    v = view(wired, c["claim_number"])
    open_ = [a["subject"] for a in v["activities"] if a["source"] == "Evidence Capture" and a["status"] == "Open"]
    assert open_ == ["Follow up on requested photos"]
    resubmit(wired, clock, {"token": token, "id": case})
    v = view(wired, c["claim_number"])
    mine = [(a["subject"], a["status"]) for a in v["activities"] if a["source"] == "Evidence Capture"]
    assert ("Follow up on requested photos", "Done") in mine
    review_rounds = [st for subject, st in mine if subject.startswith("Review verified evidence")]
    assert sorted(review_rounds) == ["Done", "Open"]  # round 1 closed by its decision, round 2 waiting
    # Round 1 draft, round 1 reviewed (with the Send back decision), round 2 draft.
    assert sum(1 for d in v["documents"] if d["document_type"] == "evidence_report") == 3
    rows = db_rows(wired, "SELECT event, round, delivered_at FROM outbox WHERE claim_id=%s ORDER BY created_at", case)
    assert [(r["event"], r["round"]) for r in rows] == [
        ("evidence.ready", 1), ("decision.recorded", 1), ("evidence.ready", 2)]
    assert all(r["delivered_at"] is not None for r in rows)


def test_on_decision_round_two_sends_a_new_pending_review(wired, model, clock):
    set_mode(wired, "on_decision")
    c = new_claim(wired)
    s = wired.post(f"{API}/claims/{c['claim_number']}/services/verified-capture").json()["services"][0]
    token = s["capture_url"].rsplit("/", 1)[1]
    capture(wired, clock, token)
    case = s["vendor_ref"]
    clock.advance(minutes=1)  # the fake clock otherwise stamps the report and the decision alike
    assert decide(wired, case, action="send_back", reason="The wide shot is too dark.", slots=["damage_wide"]).status_code == 200
    resubmit(wired, clock, {"token": token, "id": case})
    assert decide(wired, case, action="approve", by="J. Smith").status_code == 200
    rows = db_rows(wired, "SELECT event, round FROM outbox WHERE claim_id=%s ORDER BY created_at", case)
    assert [(r["event"], r["round"]) for r in rows] == [
        ("evidence.pending_review", 1), ("evidence.ready", 1), ("evidence.pending_review", 2), ("evidence.ready", 2)]
