"""T48 (tasks/t48-design.md): API keys per org, the webhook secret, the test webhook, test mode, the developers page."""

import hashlib
import json
import re
import shutil
import subprocess

import pytest

from evidence_server import report
from test_damage_flow import db_rows
from test_integration_api import route
from test_users import signed_in

V1 = "/api/v1/capture-requests"
HOOK = "https://insurer.example/hook"  # on the test deployment's callback allowlist (conftest)


def body(number):
    return {"insurer_claim_number": number, "policy_number": "DEMO-AUTO-1001", "callback_url": HOOK,
            "claimant": {"name": "Jordan Avery"}, "loss": {"date": "2026-10-27", "description": "Backed into a bollard."}}


def bearer(key):
    return {"Authorization": f"Bearer {key}"}


def new_key(client, name="Claims system", mode="live"):
    r = client.post("/api/admin/api-keys", json={"name": name, "mode": mode})
    assert r.status_code == 201, r.text
    return r.json()


def call(client, key, number="DM-2026-0700"):
    return client.post(V1, json=body(number), headers=bearer(key))


def keys(client):
    return {k["id"]: k for k in client.get("/api/admin/api-keys").json()}


# --- API keys ---


def test_a_new_key_is_shown_once_and_stored_as_a_hash(client):
    made = new_key(client)
    key = made["key"]
    assert re.fullmatch(r"ek_live_[A-Za-z0-9_-]{40,}", key)
    assert (made["prefix"], made["status"], made["created_by"], made["mode"]) == (key[:12], "active", "Administrator", "live")
    assert all("key" not in k and "key_sha256" not in k for k in keys(client).values())
    [row] = db_rows(client, "SELECT key_sha256 FROM api_keys WHERE id=%s", made["id"])
    assert row["key_sha256"] == hashlib.sha256(key.encode()).hexdigest()
    assert keys(client)[made["id"]]["last_used_at"] is None
    assert call(client, key).status_code == 201
    assert keys(client)[made["id"]]["last_used_at"]


def test_the_mock_insurer_key_is_listed_and_still_works(client):
    mock = [k for k in keys(client).values() if k["name"] == "Demo insurer (mock)"]
    assert len(mock) == 1 and (mock[0]["mode"], mock[0]["status"]) == ("live", "active")
    [cfg] = db_rows(client, "SELECT api_key FROM mock_config")
    assert call(client, cfg["api_key"], "DM-2026-0701").status_code == 201


def test_new_keys_are_validated(client):
    for b in ({"name": "", "mode": "live"}, {"name": "x" * 61, "mode": "live"}, {"name": "Claims", "mode": "prod"}, {}):
        assert client.post("/api/admin/api-keys", json=b).status_code == 400, b


def test_a_revoked_key_is_refused(client):
    made = new_key(client)
    r = client.post(f"/api/admin/api-keys/{made['id']}/revoke")
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert call(client, made["key"]).status_code == 401
    r = client.post(f"/api/admin/api-keys/{made['id']}/revoke")
    assert (r.status_code, r.json()["detail"]) == (409, "This key is already revoked")
    assert client.post("/api/admin/api-keys/nope/revoke").status_code == 404


def test_rotation_keeps_the_old_key_for_an_hour_by_default(client, clock):
    old = new_key(client)
    r = client.post(f"/api/admin/api-keys/{old['id']}/rotate", json={})
    assert r.status_code == 201, r.text
    new = r.json()
    assert new["key"] != old["key"] and (new["name"], new["mode"], new["status"]) == (old["name"], "live", "active")
    assert keys(client)[old["id"]]["status"] == "expiring"
    assert call(client, old["key"], "DM-2026-0702").status_code == 201
    clock.advance(seconds=3601)
    assert call(client, old["key"], "DM-2026-0703").status_code == 401
    assert call(client, new["key"], "DM-2026-0704").status_code == 201
    assert keys(client)[old["id"]]["status"] == "expired"
    assert client.post(f"/api/admin/api-keys/{old['id']}/rotate", json={}).status_code == 409


def test_rotation_can_stop_the_old_key_now_or_tomorrow(client, clock):
    now_key, day_key = new_key(client, "A"), new_key(client, "B")
    client.post(f"/api/admin/api-keys/{now_key['id']}/rotate", json={"old_key_expires_in": "now"})
    assert call(client, now_key["key"]).status_code == 401
    client.post(f"/api/admin/api-keys/{day_key['id']}/rotate", json={"old_key_expires_in": "24h"})
    clock.advance(hours=23)
    assert call(client, day_key["key"], "DM-2026-0705").status_code == 201
    fresh = new_key(client, "C")
    assert client.post(f"/api/admin/api-keys/{fresh['id']}/rotate", json={"old_key_expires_in": "soon"}).status_code == 400


def test_only_a_supervisor_manages_keys_and_webhooks(anon, client):
    maria, _ = signed_in(anon, client)
    for method, url, b in (
        ("GET", "/api/admin/api-keys", None),
        ("POST", "/api/admin/api-keys", {"name": "X", "mode": "live"}),
        ("GET", "/api/admin/webhook-secret", None),
        ("POST", "/api/admin/webhook-secret/rotate", None),
        ("POST", "/api/admin/webhooks/test", {"url": HOOK}),
    ):
        r = maria.request(method, url, json=b)
        assert (r.status_code, r.json()["detail"]) == (403, "Supervisors only"), url


# --- the webhook secret and the test webhook ---


def test_the_test_webhook_sends_a_signed_ping_and_reports_the_answer(client):
    sent = []
    http = route(client, sent)
    client.app.state.store.http = http
    out = client.post("/api/admin/webhooks/test", json={"url": HOOK}).json()
    assert (out["ok"], out["status_code"]) == (True, 200) and isinstance(out["ms"], int)
    [req] = sent
    payload = json.loads(req["body"])
    assert payload["event"] == "ping" and payload["id"].startswith("evt_") and payload["org"] == "Demo Mutual Insurance"
    [cred] = db_rows(client, "SELECT webhook_secret FROM integration_credentials")
    assert report.verify_signature(req["body"], cred["webhook_secret"].encode(), req["headers"]["X-Evidence-Signature"])
    http.status[0] = 500
    assert client.post("/api/admin/webhooks/test", json={"url": HOOK}).json()["ok"] is False
    assert db_rows(client, "SELECT COUNT(*) AS n FROM outbox")[0]["n"] == 0  # never queued


def test_the_test_webhook_refuses_urls_a_capture_request_could_not_use(client):
    for url in ("https://evil.example/hook", "ftp://insurer.example/hook", "not a url"):
        assert client.post("/api/admin/webhooks/test", json={"url": url}).status_code == 400, url


def test_a_network_error_is_reported_not_raised(client):
    def down(method, url, body, headers):
        raise OSError("connection refused")

    client.app.state.store.http = down
    out = client.post("/api/admin/webhooks/test", json={"url": HOOK}).json()
    assert out["ok"] is False and out["error"]


def test_rotating_the_webhook_secret_signs_what_follows_and_updates_the_mock(client):
    before = client.get("/api/admin/webhook-secret").json()
    secret = client.post("/api/admin/webhook-secret/rotate").json()["secret"]
    after = client.get("/api/admin/webhook-secret").json()
    assert after["last4"] == secret[-4:] != before["last4"] and after["rotated_at"]
    assert "secret" not in after
    [cfg] = db_rows(client, "SELECT webhook_secret FROM mock_config")
    assert cfg["webhook_secret"] == secret
    sent = []
    client.app.state.store.http = route(client, sent)
    client.post("/api/admin/webhooks/test", json={"url": HOOK})
    assert report.verify_signature(sent[0]["body"], secret.encode(), sent[0]["headers"]["X-Evidence-Signature"])


# --- test mode ---


def test_a_test_key_makes_test_claims_that_never_meet_live_ones(client):
    live, test = new_key(client, "Live", "live")["key"], new_key(client, "Sandbox", "test")["key"]
    assert test.startswith("ek_test_")
    r_live, r_test = call(client, live, "DM-2026-0800"), call(client, test, "DM-2026-0800")
    assert (r_live.status_code, r_test.status_code) == (201, 201)  # same number, other mode: a case of its own
    live_case, test_case = r_live.json(), r_test.json()
    assert live_case["case_id"] != test_case["case_id"]
    assert (live_case["test_mode"], test_case["test_mode"]) == (False, True)
    assert call(client, test, "DM-2026-0800").json()["case_id"] == test_case["case_id"]  # idempotent within the mode
    assert client.get(f"{V1}/{test_case['case_id']}", headers=bearer(live)).status_code == 404
    assert client.get(f"{V1}/{live_case['case_id']}", headers=bearer(test)).status_code == 404
    assert client.get(f"{V1}/{test_case['case_id']}", headers=bearer(test)).json()["test_mode"] is True
    groups = client.get("/api/admin/queue").json()["groups"]
    assert groups[-1]["key"] == "test" and groups[-1]["label"] == "Test claims"
    assert [c["id"] for c in groups[-1]["claims"]] == [test_case["case_id"]]
    others = [c["id"] for g in groups[:-1] for c in g["claims"]]
    assert test_case["case_id"] not in others and live_case["case_id"] in others
    assert client.get(f"/api/admin/claims/{test_case['case_id']}").json()["test_mode"] is True


def test_no_test_lane_without_test_claims(client):
    assert all(g["key"] != "test" for g in client.get("/api/admin/queue").json()["groups"])


# --- the developers page ---


def test_the_developers_api_is_public_and_built_from_the_real_payloads(anon):
    d = anon.get("/api/developers").json()
    assert set(d["events"]) == {"evidence.ready", "decision.recorded", "ping"}
    assert d["events"]["evidence.ready"]["event"] == "evidence.ready"
    assert d["events"]["ping"]["event"] == "ping"
    from evidence_server import ratelimit, store as store_mod
    assert d["limits"] == {
        "api_per_minute": ratelimit.LIMITS["api"],
        "retries": store_mod.OUTBOX_MAX_ATTEMPTS,
        "first_retry_seconds": int(store_mod.OUTBOX_BACKOFF_BASE.total_seconds()),
        "max_retry_wait_seconds": int(store_mod.OUTBOX_BACKOFF_CAP.total_seconds()),
    }
    for secretish in ("ek_live_", "ek_test_", "webhook_secret"):
        assert secretish not in json.dumps(d["events"])


def signed(body: bytes, secret: str, age_seconds: int = 0) -> str:
    import time

    return report.sign(body, secret.encode(), int(time.time()) - age_seconds)


def test_the_python_verify_sample_works_as_printed(anon):
    code = anon.get("/api/developers").json()["verify"]["python"]
    scope: dict = {}
    exec(compile(code, "<developers python sample>", "exec"), scope)  # noqa: S102 - our own published sample
    verify = scope["verify"]
    payload, secret = b'{"event":"ping"}', "whsec_test_secret"
    assert verify(payload, signed(payload, secret), secret) is True
    assert verify(payload + b" ", signed(payload, secret), secret) is False
    assert verify(payload, signed(payload, "other"), secret) is False
    assert verify(payload, signed(payload, secret, age_seconds=600), secret) is False  # older than 5 minutes
    assert verify(payload, "garbage", secret) is False


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_node_verify_sample_works_as_printed(anon, tmp_path):
    code = anon.get("/api/developers").json()["verify"]["node"]
    payload, secret = '{"event":"ping"}', "whsec_test_secret"
    good, stale = signed(payload.encode(), secret), signed(payload.encode(), secret, age_seconds=600)
    script = tmp_path / "check.js"
    script.write_text(code + f"""
const body = Buffer.from({json.dumps(payload)});
console.log(JSON.stringify([
  verify(body, {json.dumps(good)}, {json.dumps(secret)}),
  verify(Buffer.from({json.dumps(payload + " ")}), {json.dumps(good)}, {json.dumps(secret)}),
  verify(body, {json.dumps(stale)}, {json.dumps(secret)}),
  verify(body, "garbage", {json.dumps(secret)}),
]));
""")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [True, False, False, False]
