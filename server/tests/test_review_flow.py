"""T40 review before sending, the reviewed report and delivery modes, through the API (contract: tasks/t40-design.md)."""

import hashlib
import json

from conftest import GOOD_DAMAGE, GOOD_STORY
from test_damage_flow import captured_claim, db_rows, send
from test_decision_flow import decide
from test_demo_insurer import API, new_claim, view, wired  # noqa: F401  (wired is a fixture)
from test_integration_api import capture
from test_loss_notice import detail


def review(client, claim_id, **body):
    return client.post(f"/api/admin/claims/{claim_id}/review", json=body)


def ready(client, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    return claim


def set_mode(client, mode):
    r = client.put("/api/admin/settings", json={"delivery_mode": mode})
    assert r.status_code == 200, r.text


# --- reviews ---


def test_flags_and_damages_carry_keys(client, model, clock):
    model.set("story", {**GOOD_STORY, "consistent": "no"})
    d = detail(client, ready(client, clock))
    assert all("key" in f for f in d["proposal"]["flags"])
    assert all("key" in x for x in d["damage"]["damages"])
    assert d["review"] == {"flags": {}, "damages": {}, "notes": []} and d["delivery_mode"] == "on_ready"
    assert d["summary"] and d["ai_summary"]["status"] == "verified"


def test_dismissing_a_flag_is_kept_and_logged(client, model, clock):
    model.set("story", {**GOOD_STORY, "consistent": "no"})
    claim = ready(client, clock)
    key = detail(client, claim)["proposal"]["flags"][0]["key"]
    d = review(client, claim["id"], kind="flag", key=key, verdict="false_alarm", reason="The dent is on the rear, as told.").json()
    assert d["review"]["flags"][key]["verdict"] == "false_alarm"
    assert any(f["key"] == key for f in d["proposal"]["flags"])  # still there
    assert [e["type"] for e in d["events"]][-1] == "review.flag"
    assert client.app.state.store.verify_chain(claim["id"])["ok"]


def test_excluded_damage_leaves_the_range(client, model, clock):
    model.set("damage", {**GOOD_DAMAGE, "damages": [GOOD_DAMAGE["damages"][0],
                                                    {**GOOD_DAMAGE["damages"][0], "area": "Hood", "damage_type": "SCRATCH_SCUFF", "severity": "minor"}]})
    claim = ready(client, clock)
    d = detail(client, claim)
    before = d["proposal"]["estimate"]["full"]
    hood = [x for x in d["damage"]["damages"] if x["area"] == "Hood"][0]["key"]
    d = review(client, claim["id"], kind="damage", key=hood, verdict="excluded", reason="Old wear, not this loss.").json()
    assert d["proposal"]["estimate"]["full"][1] < before[1]
    assert any(x["key"] == hood for x in d["damage"]["damages"])  # still listed
    # The hood scratch is in both photos: excluding one listing excludes the physical damage.
    assert d["proposal"]["estimate"]["full"] == [300, 1400]
    assert [p["damage_type"] for p in d["proposal"]["estimate"]["priced"]] == ["BUMPER_DAMAGE"]


def test_reviews_need_a_ready_proposal_and_stop_at_the_decision(client, model, clock):
    claim, _ = captured_claim(client, clock)
    assert review(client, claim["id"], kind="note", text="Too early.").status_code == 409
    send(client, claim)
    assert review(client, claim["id"], kind="note", text="Called the claimant.").status_code == 200
    assert review(client, claim["id"], kind="note", text="hi").status_code == 400
    decide(client, claim["id"], action="approve")
    assert review(client, claim["id"], kind="note", text="After the fact.").status_code == 409


# --- the reviewed report ---


def test_decision_makes_a_reviewed_pdf_and_the_link_serves_it(anon, client, model, clock):
    claim = ready(client, clock)
    review(client, claim["id"], kind="note", text="Called the claimant, car at the shop.")
    d = decide(client, claim["id"], action="approve", by="J. Smith").json()
    url = d["report"]["url"]
    reviewed = anon.get(url)
    draft = anon.get(url + "?version=draft")
    assert reviewed.content.startswith(b"%PDF") and draft.content.startswith(b"%PDF")
    assert reviewed.content != draft.content
    sha = hashlib.sha256(reviewed.content).hexdigest()
    ev = [e for e in d["events"] if e["type"] == "report.reviewed"]
    assert len(ev) == 1 and ev[0]["data"]["sha256"] == sha
    row = db_rows(client, "SELECT * FROM reports WHERE claim_id=%s", claim["id"])[0]
    assert row["reviewed_sha256"] == sha and row["sha256"] == hashlib.sha256(draft.content).hexdigest()


def test_on_ready_decision_message_carries_the_reviewed_pdf(client, model, clock):
    claim = ready(client, clock)
    decide(client, claim["id"], action="approve")
    body = json.loads(db_rows(client, "SELECT body_json FROM outbox WHERE claim_id=%s AND event='decision.recorded'", claim["id"])[0]["body_json"])
    assert body["documents"][0]["name"] == "reviewed-evidence-report.pdf"
    assert body["documents"][0]["sha256"] == db_rows(client, "SELECT reviewed_sha256 FROM reports WHERE claim_id=%s", claim["id"])[0]["reviewed_sha256"]


def test_report_survives_a_failed_or_rejected_ai_summary(client, model, clock):
    model.set("summary", RuntimeError("Meta is down"))
    d = detail(client, ready(client, clock))
    assert d["report"] is not None and d["ai_summary"]["status"] == "failed" and d["summary"]
    model.set("summary", {"sentences": [{"text": "Looks like fraud.", "refs": ["D1"]}]})
    claim, _ = captured_claim(client, clock, photos={"damage_closeup": [b"\xff\xd8\xff\xe0other-1"], "damage_wide": [b"\xff\xd8\xff\xe0other-2"]})
    d = detail(client, (send(client, claim), claim)[1])
    assert d["ai_summary"]["status"] == "rejected" and d["report"] is not None


# --- delivery modes (V4) ---


def test_settings_validate_and_need_login(anon, client):
    s = client.get("/api/admin/settings").json()
    assert (s["delivery_mode"], s["brand_color"]) == ("on_ready", "#1673e6")
    assert client.put("/api/admin/settings", json={"delivery_mode": "sometimes"}).status_code == 400
    set_mode(client, "on_decision")
    s = client.get("/api/admin/settings").json()
    assert (s["delivery_mode"], s["brand_color"], s["updated_by"]) == ("on_decision", "#1673e6", "Administrator")
    client.post("/api/admin/logout")
    assert anon.get("/api/admin/settings").status_code == 401


def test_on_decision_sends_once_after_the_review(wired, model, clock):
    set_mode(wired, "on_decision")
    c = new_claim(wired)
    s = wired.post(f"{API}/claims/{c['claim_number']}/services/verified-capture").json()["services"][0]
    capture(wired, clock, s["capture_url"].rsplit("/", 1)[1])
    case = s["vendor_ref"]
    # Track 3: only the review activity goes out before the decision; no report, no flags.
    rows = db_rows(wired, "SELECT * FROM outbox WHERE claim_id=%s", case)
    assert [r["event"] for r in rows] == ["evidence.pending_review"] and rows[0]["delivered_at"] is not None
    v = view(wired, c["claim_number"])
    assert v["services"][0]["status"] == "Requested" and v["documents"] == [] and v["flags"] == []
    assert [(a["subject"], a["status"]) for a in v["activities"] if a["source"] == "Evidence Capture"] == [
        ("Review verified evidence in Evidence Capture", "Open")]
    review(wired, case, kind="note", text="Checked every photo with the claimant.")
    assert decide(wired, case, action="approve", by="J. Smith").status_code == 200
    rows = db_rows(wired, "SELECT * FROM outbox WHERE claim_id=%s ORDER BY created_at", case)
    assert [r["event"] for r in rows] == ["evidence.pending_review", "evidence.ready"]
    assert all(r["delivered_at"] is not None for r in rows)
    body = json.loads(rows[1]["body_json"])
    assert body["documents"][0]["name"] == "reviewed-evidence-report.pdf" and body["decision"]["action"] == "approve"
    v = view(wired, c["claim_number"])
    assert v["services"][0]["status"] == "Completed"
    assert {d["document_type"] for d in v["documents"]} == {"evidence_report", "sealed_evidence"}
    subjects = [n["subject"] for n in v["notes"] if "vendor" in n["author"]]
    assert subjects == ["Verified evidence capture", "Adjuster decision"] or sorted(subjects) == ["Adjuster decision", "Verified evidence capture"]
    assert [a["subject"] for a in v["activities"]
            if a["source"] == "Evidence Capture" and a["status"] == "Open"] == ["Send to estimating (fast track)"]


def test_pending_review_message_is_minimal_and_links_the_console(wired, model, clock):
    set_mode(wired, "on_decision")
    c = new_claim(wired)
    s = wired.post(f"{API}/claims/{c['claim_number']}/services/verified-capture").json()["services"][0]
    capture(wired, clock, s["capture_url"].rsplit("/", 1)[1])
    case = s["vendor_ref"]
    body = json.loads(db_rows(wired, "SELECT body_json FROM outbox WHERE claim_id=%s", case)[0]["body_json"])
    assert body["event"] == "evidence.pending_review" and body["claim"]["insurer_claim_number"] == c["claim_number"]
    assert (body["documents"], body["flags"], body["notes"]) == ([], [], [])
    assert "recommendation" not in body and "seal" not in body
    assert body["links"] == {"console": f"/claims/{case}"}
    [activity] = body["activities"]
    assert activity["subject"] == "Review verified evidence in Evidence Capture" and activity["url"] == f"/claims/{case}"
    # The mock resolves the relative link against our base URL, like document links.
    v = view(wired, c["claim_number"])
    [mine] = [a for a in v["activities"] if a["source"] == "Evidence Capture"]
    assert mine["description"].endswith(f"https://testserver/claims/{case}")


def test_on_ready_mode_sends_no_pending_review(wired, model, clock):
    c = new_claim(wired)
    s = wired.post(f"{API}/claims/{c['claim_number']}/services/verified-capture").json()["services"][0]
    capture(wired, clock, s["capture_url"].rsplit("/", 1)[1])
    rows = db_rows(wired, "SELECT event FROM outbox WHERE claim_id=%s", s["vendor_ref"])
    assert [r["event"] for r in rows] == ["evidence.ready"]
