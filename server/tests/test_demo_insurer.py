"""The mock claims system talking to us over HTTP only (contract: tasks/t16-t29-design.md)."""

import json
from pathlib import Path

import pytest

from evidence_server import report
from test_damage_flow import db_rows
from test_integration_api import capture, route

API = "/api/demo-insurer"


@pytest.fixture
def wired(client):
    """Both sides send their HTTP calls back into the same app, as two real systems would over the internet."""
    client.app.state.store.http = route(client)
    client.app.state.demo.http = route(client)
    return client


def new_claim(client, **kw):
    body = {"policy_number": "DEMO-AUTO-1001", "loss_date": "2026-10-27", "loss_cause": "collision",
            "description": "Backed into a bollard leaving a parking space.", "claimant_phone": "+1 415 555 0101", **kw}
    r = client.post(f"{API}/claims", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def view(client, number):
    r = client.get(f"{API}/claims/{number}")
    assert r.status_code == 200, r.text
    return r.json()


def secret(client):
    return db_rows(client, "SELECT webhook_secret FROM mock_config")[0]["webhook_secret"].encode()


def webhook(client, body: dict, key=None, ts=None):
    raw = json.dumps(body).encode()
    ts = ts if ts is not None else int(client.app.state.store.clock.now().timestamp())
    sig = report.sign(raw, key or secret(client), ts)
    return client.post(f"{API}/webhook", content=raw, headers={"X-Evidence-Signature": sig, "Content-Type": "application/json"})


# --- opening a claim (FNOL) ---


def test_new_claim_gets_a_number_an_exposure_and_a_first_activity(client):
    c = new_claim(client)
    assert c["claim_number"] == "DM-2026-0415" and c["status"] == "Open"
    assert c["insured_name"] == "Jordan Avery"
    assert [(e["type"], e["coverage"], e["reserve"]) for e in c["exposures"]] == [("Vehicle damage", "Collision", 0)]
    assert [a["subject"] for a in c["activities"]] == ["Make initial contact with the insured"]
    assert c["activities"][0]["due_date"] == "2026-10-29"  # Wed 28 Oct + 1 business day
    assert c["financials"] == {"reserves": 0, "paid": 0, "recovered": 0}
    assert new_claim(client, loss_cause="comprehensive")["claim_number"] == "DM-2026-0416"


def test_unknown_policy_is_refused(client):
    r = client.post(f"{API}/claims", json={"policy_number": "NOPE-1", "loss_date": "2026-10-27", "loss_cause": "collision",
                                           "description": "x", "claimant_phone": None})
    assert r.status_code == 400


def test_mock_needs_login_except_the_webhook(anon, client):
    client.post("/api/admin/logout")
    assert anon.get(f"{API}/desktop").status_code == 401
    assert anon.post(f"{API}/webhook", content=b"{}").status_code == 401  # reached, refused for the signature


# --- requesting the verified capture (T16 from the insurer's side) ---


def test_request_capture_calls_our_api_and_shows_the_link(wired):
    c = new_claim(wired)
    v = wired.post(f"{API}/claims/{c['claim_number']}/services/verified-capture").json()
    s = v["services"][0]
    assert (s["type"], s["vendor"], s["status"]) == ("Verified evidence capture", "Evidence Capture", "Requested")
    assert s["capture_url"].startswith("https://testserver/c/") and s["vendor_ref"].startswith("CLM-")
    ours = wired.get(f"/api/admin/claims/{s['vendor_ref']}").json()
    assert ours["insurer_claim_number"] == c["claim_number"]
    assert ours["callback_url"] == "https://testserver/api/demo-insurer/webhook"
    assert any("SMS simulated" in n["body"] for n in v["notes"])
    again = wired.post(f"{API}/claims/{c['claim_number']}/services/verified-capture").json()
    assert len(again["services"]) == 1


def test_vendor_error_writes_nothing(client):
    def refuses(method, url, body, headers):
        return 401, b'{"detail": "bad key"}'
    client.app.state.demo.http = refuses
    c = new_claim(client)
    r = client.post(f"{API}/claims/{c['claim_number']}/services/verified-capture")
    assert r.status_code == 502
    assert view(client, c["claim_number"])["services"] == []


# --- end to end (I6) ---


def test_results_land_in_the_claim_file(wired, model, clock):
    c = new_claim(wired)
    s = wired.post(f"{API}/claims/{c['claim_number']}/services/verified-capture").json()["services"][0]
    capture(wired, clock, s["capture_url"].rsplit("/", 1)[1])
    v = view(wired, c["claim_number"])
    assert v["services"][0]["status"] == "Completed" and v["services"][0]["completed_at"]
    docs = {d["document_type"]: d for d in v["documents"]}
    assert set(docs) == {"evidence_report", "sealed_evidence"}
    assert docs["evidence_report"]["url"].startswith("https://testserver/api/r/")
    assert wired.get(docs["evidence_report"]["url"][len("https://testserver"):]).content.startswith(b"%PDF")
    vendor_notes = [n for n in v["notes"] if n["author"] == "Evidence Capture (vendor)"]
    assert len(vendor_notes) == 1 and vendor_notes[0]["body"].startswith("Verified capture complete")
    review = [a for a in v["activities"] if a["source"] == "Evidence Capture"]
    assert len(review) == 1 and review[0]["subject"].startswith("Review verified evidence") and review[0]["status"] == "Open"
    desk = wired.get(f"{API}/desktop").json()
    assert any(a["claim_number"] == c["claim_number"] and a["subject"].startswith("Review verified evidence")
               for a in desk["activities"])


def test_flags_become_high_risk_indicators(wired, model, clock):
    model.set("plate", {"is_plate": True, "text": "8ABC123", "state": "CA", "unsure": False, "problem": "none"})
    c = new_claim(wired)
    s = wired.post(f"{API}/claims/{c['claim_number']}/services/verified-capture").json()["services"][0]
    capture(wired, clock, s["capture_url"].rsplit("/", 1)[1])
    flags = view(wired, c["claim_number"])["flags"]
    assert flags and all(f["source"] == "Evidence Capture" for f in flags)
    assert any(f["level"] == "conflict" for f in flags)


# --- webhook security (I5) ---


def test_webhook_refuses_bad_or_old_signatures(client):
    new_claim(client)
    body = {"event": "evidence.ready", "event_id": "e1", "claim": {"insurer_claim_number": "DM-2026-0415"},
            "documents": [], "notes": [{"subject": "s", "body": "b"}], "activities": [], "flags": []}
    assert webhook(client, body, key=b"wrong").status_code == 401
    old = int(client.app.state.store.clock.now().timestamp()) - 301
    assert webhook(client, body, ts=old).status_code == 401
    assert all(n["author"] != "Evidence Capture (vendor)" for n in view(client, "DM-2026-0415")["notes"])


def test_same_event_twice_writes_once(client):
    new_claim(client)
    body = {"event": "decision.recorded", "event_id": "e2", "claim": {"insurer_claim_number": "DM-2026-0415"},
            "documents": [], "notes": [{"subject": "Decision", "body": "Approved lane."}], "activities": [], "flags": []}
    assert webhook(client, body).status_code == 200
    second = webhook(client, body)
    assert second.status_code == 200 and second.json().get("duplicate") is True
    assert len([n for n in view(client, "DM-2026-0415")["notes"] if n["subject"] == "Decision"]) == 1


def test_unknown_claim_number_is_404(client):
    body = {"event": "evidence.ready", "event_id": "e3", "claim": {"insurer_claim_number": "DM-1999-0001"},
            "documents": [], "notes": [], "activities": [], "flags": []}
    assert webhook(client, body).status_code == 404


# --- adjuster actions in the mock ---


def test_adjuster_completes_activities_adds_notes_and_sets_reserve(client):
    c = new_claim(client)
    act = c["activities"][0]
    assert client.post(f"{API}/activities/{act['id']}/complete").status_code == 200
    client.post(f"{API}/claims/{c['claim_number']}/notes", json={"subject": "Call", "body": "Left a voicemail."})
    exp = c["exposures"][0]
    client.post(f"{API}/exposures/{exp['id']}/reserve", json={"amount": 1200})
    v = view(client, c["claim_number"])
    assert v["activities"][0]["status"] == "Done"
    assert any(n["subject"] == "Call" and n["author"] == "Adjuster" for n in v["notes"])
    assert v["exposures"][0]["reserve"] == 1200 and v["financials"]["reserves"] == 1200
    assert any(n["subject"] == "Reserve set" for n in v["notes"])
    assert client.post(f"{API}/exposures/{exp['id']}/reserve", json={"amount": -5}).status_code == 400


# --- separation (I7) ---


def test_mock_never_reaches_into_the_store():
    src = (Path(__file__).parents[1] / "src" / "evidence_server" / "demo_insurer.py").read_text()
    assert "from .store" not in src and "import store" not in src and "Store" not in src
