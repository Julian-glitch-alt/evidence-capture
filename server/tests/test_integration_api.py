"""T16 inbound capture API and T29 signed delivery (contract: tasks/t16-t29-design.md), with a fake model."""

import hashlib
import json

import pytest

from conftest import JPEG, Phone
from evidence_server import report
from test_damage_flow import CLOSEUP, WIDE, db_rows
from test_loss_notice import DAMAGE_ANSWERS, MEDIA, detail, save

BASE = "https://testserver"


def route(client, record=None):
    """An Http transport that sends absolute testserver URLs back into the app; other hosts are recorded."""
    def http(method, url, body, headers):
        if record is not None and not url.startswith(BASE):
            record.append({"method": method, "url": url, "body": body, "headers": headers})
            return record_status[0], b'{"ok": true}'
        r = client.request(method, url[len(BASE):], content=body, headers=headers)
        return r.status_code, r.content
    record_status = [200]
    http.status = record_status
    return http


def api_key(client):
    return db_rows(client, "SELECT api_key FROM mock_config")[0]["api_key"]


def auth(client):
    return {"Authorization": f"Bearer {api_key(client)}"}


def order(client, number="DM-2026-0500", callback="https://insurer.example/hook", policy="DEMO-AUTO-1001", **extra):
    body = {"insurer_claim_number": number, "policy_number": policy, "callback_url": callback,
            "claimant": {"name": "Jordan Avery", "phone": "+1 415 555 0101"},
            "loss": {"date": "2026-10-27", "description": "Backed into a bollard."}, **extra}
    return client.post("/api/v1/capture-requests", json=body, headers=auth(client))


def capture(client, clock, token):
    """Every required shot taken on an existing claim, then submitted."""
    layout = save(client, token, DAMAGE_ANSWERS)
    phone = Phone(client, token)
    photos = {"damage_closeup": CLOSEUP, "damage_wide": WIDE}
    for section in layout["sections"]:
        for slot in section["slots"]:
            if slot["id"] in photos or slot["required"]:
                clock.advance(seconds=1)
                data, mime = (photos[slot["id"]], "image/jpeg") if slot["id"] in photos else MEDIA.get(slot["kind"], (JPEG, "image/jpeg"))
                assert phone.upload(slot["id"], data, mime).status_code == 200
    r = client.post(f"/api/c/{token}/submit", json={})
    assert r.status_code == 200, r.text


@pytest.fixture
def sent(client):
    """Outgoing calls to hosts other than testserver."""
    calls = []
    client.app.state.store.http = route(client, calls)
    return calls


# --- credentials and auth ---


def test_api_needs_the_key(client):
    assert client.post("/api/v1/capture-requests", json={}).status_code == 401
    bad = client.post("/api/v1/capture-requests", json={}, headers={"Authorization": "Bearer ek_wrong"})
    assert bad.status_code == 401
    assert client.get("/api/v1/capture-requests/CLM-x", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_key_is_stored_only_as_a_hash(client):
    key = api_key(client)
    assert key.startswith("ek_") and len(key) >= 40
    row = db_rows(client, "SELECT * FROM integration_credentials")[0]
    assert row["api_key_sha256"] == hashlib.sha256(key.encode()).hexdigest()
    assert key not in json.dumps(row, default=str)


# --- capture requests (T16) ---


def test_order_opens_a_case_with_a_capture_link(client):
    r = order(client)
    assert r.status_code == 201, r.text
    c = r.json()
    assert c["insurer_claim_number"] == "DM-2026-0500" and c["status"] == "opened" and c["report_url"] is None
    assert c["capture_url"].startswith(f"{BASE}/c/")
    token = c["capture_url"].rsplit("/", 1)[1]
    assert client.get(f"/api/c/{token}").status_code == 200
    d = client.get(f"/api/admin/claims/{c['case_id']}").json()
    assert (d["insurer_claim_number"], d["callback_url"]) == ("DM-2026-0500", "https://insurer.example/hook")
    opened = [e for e in d["events"] if e["type"] == "claim.opened"][0]
    assert opened["actor"] == "system" and opened["data"]["insurer_claim_number"] == "DM-2026-0500"


def test_the_same_claim_number_returns_the_same_case(client):
    first = order(client).json()
    again = order(client, callback="https://elsewhere.example/hook")
    assert again.status_code == 200 and again.json()["case_id"] == first["case_id"]
    assert len(db_rows(client, "SELECT id FROM claims WHERE insurer_claim_number=%s", "DM-2026-0500")) == 1
    assert detail(client, {"id": first["case_id"]})["callback_url"] == "https://insurer.example/hook"


@pytest.mark.parametrize("change", [
    {"insurer_claim_number": ""}, {"insurer_claim_number": "bad number!"}, {"insurer_claim_number": "x" * 65},
    {"policy_number": ""}, {"callback_url": "ftp://x"}, {"callback_url": ""},
])
def test_invalid_orders_are_refused(client, change):
    body = {"insurer_claim_number": "DM-1", "policy_number": "DEMO-AUTO-1001", "callback_url": "https://x.example/h", **change}
    r = client.post("/api/v1/capture-requests", json=body, headers=auth(client))
    assert r.status_code == 400


def test_status_by_case_id(client):
    c = order(client).json()
    r = client.get(f"/api/v1/capture-requests/{c['case_id']}", headers=auth(client))
    assert r.status_code == 200 and r.json()["case_id"] == c["case_id"]
    assert client.get("/api/v1/capture-requests/CLM-nope", headers=auth(client)).status_code == 404


def test_claimant_never_sees_insurer_fields(client):
    # T43a (Julian, 2 Nov): the claimant sees the insurer's claim number they already know; never the callback.
    c = order(client).json()
    state = client.get(f"/api/c/{c['capture_url'].rsplit('/', 1)[1]}").json()
    assert "insurer.example" not in json.dumps(state) and "callback" not in json.dumps(state)
    assert state["insurer_claim_number"] == "DM-2026-0500"


# --- delivery (T29) ---


def test_ready_report_is_delivered_signed(client, model, clock, sent):
    c = order(client).json()
    capture(client, clock, c["capture_url"].rsplit("/", 1)[1])
    assert len(sent) == 1
    call = sent[0]
    assert (call["method"], call["url"]) == ("POST", "https://insurer.example/hook")
    secret = db_rows(client, "SELECT webhook_secret FROM integration_credentials")[0]["webhook_secret"].encode()
    assert report.verify_signature(call["body"], secret, call["headers"]["X-Evidence-Signature"])
    assert call["headers"]["X-Evidence-Event"] == "evidence.ready"
    body = json.loads(call["body"])
    assert body["claim"]["insurer_claim_number"] == "DM-2026-0500"  # I3
    d = detail(client, {"id": c["case_id"]})
    msg = d["report"]["outbox"][0]
    assert msg["delivered_at"] is not None and call["headers"]["X-Evidence-Delivery"] == msg["id"]
    status = client.get(f"/api/v1/capture-requests/{c['case_id']}", headers=auth(client)).json()
    assert status["report_url"].startswith(f"{BASE}/api/r/")


def test_failed_delivery_is_recorded_and_can_be_resent(client, model, clock, sent):
    client.app.state.store.http.status[0] = 500
    c = order(client).json()
    capture(client, clock, c["capture_url"].rsplit("/", 1)[1])
    row = db_rows(client, "SELECT * FROM outbox WHERE claim_id=%s", c["case_id"])[0]
    assert row["delivered_at"] is None and row["attempts"] == 1 and "500" in row["last_error"]
    client.app.state.store.http.status[0] = 200
    assert client.post(f"/api/admin/claims/{c['case_id']}/deliver").json() == {"ok": True}
    row = db_rows(client, "SELECT * FROM outbox WHERE claim_id=%s", c["case_id"])[0]
    assert row["delivered_at"] is not None and len(sent) == 2


def test_network_error_never_breaks_the_report(client, model, clock):
    def down(method, url, body, headers):
        raise ConnectionError("insurer is down")
    client.app.state.store.http = down
    c = order(client).json()
    capture(client, clock, c["capture_url"].rsplit("/", 1)[1])
    row = db_rows(client, "SELECT * FROM outbox WHERE claim_id=%s", c["case_id"])[0]
    assert row["delivered_at"] is None and row["attempts"] == 1 and "insurer is down" in row["last_error"]
    assert detail(client, {"id": c["case_id"]})["report"] is not None


def test_claim_without_callback_is_never_posted(client, model, clock, sent):
    from test_damage_flow import captured_claim, send
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    assert sent == []
    assert db_rows(client, "SELECT attempts FROM outbox WHERE claim_id=%s", claim["id"])[0]["attempts"] == 0


def test_resend_needs_login(anon, client):
    client.post("/api/admin/logout")
    assert anon.post("/api/admin/claims/CLM-x/deliver").status_code == 401
