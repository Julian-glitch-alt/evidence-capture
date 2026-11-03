"""Regression tests for the security review of 28 Oct (one test per finding)."""

import json

import pytest

from conftest import JPEG, Phone


@pytest.fixture
def claim(client):
    return client.post("/api/admin/claims", json={"policy_number": "DEMO-AUTO-1001"}).json()


def test_oversized_client_meta_is_refused_and_claim_stays_readable(client, claim):  # M1
    phone = Phone(client, claim["token"])
    r = client.post(
        f"/api/c/{claim['token']}/items",
        data={
            "step_id": "plate",
            "session_id": phone.session["session_id"],
            "nonce": phone.nonce("plate"),
            "client_meta": json.dumps({"x": "a" * 5000}),
        },
        files={"file": ("x", JPEG, "image/jpeg")},
    )
    assert r.status_code == 413
    assert client.get(f"/api/admin/claims/{claim['id']}").status_code == 200


def test_huge_upload_is_refused_from_its_declared_size(anon):  # M2
    r = anon.post(
        "/api/c/anything/items",
        content=b"x",
        headers={"content-length": str(200 * 1024 * 1024), "content-type": "multipart/form-data; boundary=b"},
    )
    assert r.status_code == 413


def test_upload_to_unknown_link_is_refused_before_reading_the_file(anon):  # M2
    r = anon.post("/api/c/not-a-token/items", data={"step_id": "plate"}, files={"file": ("x", JPEG, "image/jpeg")})
    assert r.status_code == 404


def test_file_larger_than_slot_limit_is_refused(client, claim, monkeypatch):  # M2
    from evidence_server import store as store_module

    monkeypatch.setitem(store_module.MAX_BYTES, "photo", 100)
    r = Phone(client, claim["token"]).upload("plate", data=JPEG + b"x" * 200)
    assert r.status_code == 413


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", b'"text"'])
def test_submit_with_malformed_body_is_400_not_500(client, claim, body):  # L7
    r = client.post(f"/api/c/{claim['token']}/submit", content=body, headers={"content-type": "application/json"})
    assert r.status_code == 400


def test_multiselect_with_wrong_type_is_cleaned_not_500():  # L7
    from evidence_server.loss_notice import clean_answers

    assert clean_answers({"damaged_areas": 5})["damaged_areas"] == []


def test_non_ascii_cookie_is_401_not_500(anon):  # L7
    anon.cookies.set("adj_session", "9999999999.éé", domain="testserver")
    assert anon.get("/api/admin/me").status_code == 401


def test_security_headers(anon):  # L5
    h = anon.get("/api/health").headers
    assert "max-age" in h["strict-transport-security"]
    assert "frame-ancestors 'none'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY"


# --- code review of 28 Oct ---


def test_retried_upload_after_success_returns_the_same_item(client, claim):  # review 1
    phone = Phone(client, claim["token"])
    nonce = phone.nonce("plate")
    first = phone.upload("plate", nonce=nonce)
    retry = phone.upload("plate", nonce=nonce)  # response was lost, phone retries
    assert retry.status_code == 200
    assert retry.json()["id"] == first.json()["id"]
    different = phone.upload("plate", data=JPEG + b"other", nonce=nonce)
    assert different.status_code == 403  # a used nonce never carries a new file


def test_remove_needs_live_session_and_is_logged(client, claim):  # review 2
    phone = Phone(client, claim["token"])
    item = phone.upload("damage_closeup").json()
    url = f"/api/c/{claim['token']}/items/{item['id']}"

    assert client.delete(url).status_code == 403  # no session
    r = client.delete(url, params={"session_id": phone.session["session_id"]})
    assert r.status_code == 200
    events = client.get(f"/api/admin/claims/{claim['id']}").json()["events"]
    assert events[-1]["type"] == "item.removed" and events[-1]["data"]["item_id"] == item["id"]


def test_single_shot_slot_cannot_be_removed(client, claim):  # review 2
    phone = Phone(client, claim["token"])
    item = phone.upload("plate").json()
    r = client.delete(f"/api/c/{claim['token']}/items/{item['id']}", params={"session_id": phone.session["session_id"]})
    assert r.status_code == 400


def test_coverage_cannot_be_verified_from_unverified_snapshot(client):  # review 4
    from evidence_server.store import CaptureError

    store = client.app.state.store
    claim = store.open_claim("NOPE-1")
    snapshot_id = store.claim_detail(claim["id"])["policy"]["id"]
    with pytest.raises(CaptureError):
        store.set_fact(claim["id"], "coverage_type", "COLLISION", "VERIFIED", [f"policy_snapshot:{snapshot_id}"])


def test_submitted_event_carries_form_hash(client, claim):  # review optional
    from test_loss_notice import DAMAGE_ANSWERS, capture_required, save

    capture_required(client, claim["token"], save(client, claim["token"], DAMAGE_ANSWERS))
    client.post(f"/api/c/{claim['token']}/submit", json={})
    detail = client.get(f"/api/admin/claims/{claim['id']}").json()
    submitted = next(e for e in detail["events"] if e["type"] == "claim.submitted")
    assert len(submitted["data"]["form_sha256"]) == 64


def test_verify_chain_catches_status_changed_behind_the_log(client):  # review optional
    store = client.app.state.store
    claim = store.open_claim("DEMO-AUTO-1001")
    with store.db.tx() as conn:
        conn.execute("UPDATE claims SET status='approved' WHERE id=%s", (claim["id"],))
    assert store.verify_chain(claim["id"])["ok"] is False
