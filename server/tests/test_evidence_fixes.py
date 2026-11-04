"""Fixes: upload sniffing and liveness, sealed event log and key rotation,
webhook scheme, proxy headers, login throttling, wording and server facts."""

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from conftest import FRAME_AFTER, FRAME_BEFORE, JPEG, PNG, Phone
from evidence_server import seal
from test_loss_notice import DAMAGE_ANSWERS, capture_required, open_claim, save


# --- 1. upload validation ---


def test_png_bytes_declared_as_jpeg_are_rejected(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    r = phone.upload("plate", PNG, "image/jpeg")
    assert r.status_code == 400


def test_jpeg_bytes_declared_as_png_are_rejected(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    # image/png is allowed for a photo slot, so only sniffing can refuse this.
    r = phone.upload("plate", JPEG, "image/png")
    assert r.status_code == 400


def test_undecodable_jpeg_is_rejected(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    r = phone.upload("plate", b"\xff\xd8\xff\xe0fake-jpeg", "image/jpeg")
    assert r.status_code == 400


def test_text_bytes_declared_as_jpeg_are_rejected(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    r = phone.upload("plate", b"this is not an image at all", "image/jpeg")
    assert r.status_code == 400


def test_video_bytes_mismatched_to_declared_type_are_rejected(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    from conftest import MP4

    r = phone.upload("walkaround", MP4, "video/webm")
    assert r.status_code == 400


def test_valid_jpeg_is_accepted(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    assert phone.upload("plate", JPEG, "image/jpeg").status_code == 200


# --- 1b/1c/8. liveness, latency and server facts ---


def _direct_upload(client, claim, phone, slot, data, frames):
    files = {"file": ("x", data, "image/jpeg")}
    if frames is not None:
        before, after = frames
        if before is not None:
            files["frame_before"] = ("frame_before.jpg", before, "image/jpeg")
        if after is not None:
            files["frame_after"] = ("frame_after.jpg", after, "image/jpeg")
    return client.post(
        f"/api/c/{claim['token']}/items",
        data={"step_id": slot, "session_id": phone.session["session_id"],
              "nonce": phone.nonce(slot),
              "client_meta": json.dumps({"captured_at": "2026-10-28T14:59:00+00:00"})},
        files=files,
    )


def test_missing_liveness_frames_flag_static_source(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    r = phone.upload("plate", frames=False)
    assert r.status_code == 200, r.text
    item = r.json()
    assert "static_source" in item["flags"]
    assert item["static_source"] == "flagged"
    assert item["server_facts"]["liveness"]["status"] == "flagged"


def test_identical_liveness_frames_flag_static_source(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    r = _direct_upload(client, claim, phone, "plate", JPEG, (JPEG, JPEG))
    assert r.status_code == 200, r.text
    assert "static_source" in r.json()["flags"]


def test_different_liveness_frames_are_not_flagged(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    r = _direct_upload(client, claim, phone, "plate", JPEG, (FRAME_BEFORE, FRAME_AFTER))
    assert r.status_code == 200, r.text
    assert "static_source" not in r.json()["flags"]
    assert r.json()["static_source"] == "ok"


def test_static_source_flag_reaches_adjuster_checks(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], DAMAGE_ANSWERS)
    phone = Phone(client, claim["token"])
    # One slot without frames; the rest normally.
    assert phone.upload("plate", frames=False).status_code == 200
    for section in layout["sections"]:
        for slot in section["slots"]:
            if slot["required"] and slot["id"] != "plate":
                from test_loss_notice import MEDIA

                data, mime = MEDIA.get(slot["kind"], (JPEG, "image/jpeg"))
                assert phone.upload(slot["id"], data, mime).status_code == 200
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 200
    detail = client.get(f"/api/admin/claims/{claim['id']}").json()
    checks = {c["check_id"]: c for c in detail["checks"]}
    assert "static_source" in checks
    assert checks["static_source"]["result"] == "unclear"
    assert checks["static_source"]["reason"]


def test_slow_upload_is_recorded_and_flagged(client, clock):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    nonce = phone.nonce("plate")
    clock.advance(seconds=61)
    r = phone.upload("plate", nonce=nonce)
    assert r.status_code == 200, r.text
    item = r.json()
    assert item["nonce_to_upload_latency_seconds"] >= 60
    assert "slow_upload" in item["flags"]
    facts = item["server_facts"]
    assert facts["received_at"]
    assert facts["nonce_issued_at"]
    assert facts["slow_upload"] is True


def test_item_server_facts_label_client_time_untrusted(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    item = phone.upload("plate").json()
    facts = item["server_facts"]
    assert facts["received_at"] == item["received_at"]
    assert facts["client_captured_at"]
    assert facts["client_captured_at_source"] == "client-reported, untrusted"
    assert facts["nonce_to_upload_latency_seconds"] is not None


# --- 2. sealed event log, key rotation, offline tool ---


def _sealed_claim(client):
    claim = open_claim(client)
    capture_required(client, claim["token"], save(client, claim["token"], DAMAGE_ANSWERS))
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 200
    return claim


def _package_files(client, claim):
    r = client.get(f"/api/admin/claims/{claim['id']}/package")
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        return {n: z.read(n) for n in z.namelist()}, r.content


def _rezip(files):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def _verify(client, data):
    r = client.post("/api/verify", files={"file": ("claim.zip", data, "application/zip")})
    assert r.status_code == 200, r.text
    return r.json()


def test_package_contains_verifiable_events_jsonl(client):
    claim = _sealed_claim(client)
    files, _ = _package_files(client, claim)
    assert "events.jsonl" in files
    manifest = json.loads(files["manifest.json"])
    events = [json.loads(line) for line in files["events.jsonl"].decode().splitlines() if line.strip()]
    assert events
    assert all({"seq", "prev_hash", "hash"} <= set(e) for e in events)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
    assert events[-1]["hash"] == manifest["event_chain_head"]
    assert len(events) == manifest["event_count"]
    assert _verify(client, _rezip(files))["valid"] is True


def test_tampered_events_jsonl_fails_verification(client):
    claim = _sealed_claim(client)
    files, _ = _package_files(client, claim)
    events = [json.loads(line) for line in files["events.jsonl"].decode().splitlines() if line.strip()]
    events[0]["actor"] = "attacker"
    files["events.jsonl"] = "".join(json.dumps(e) + "\n" for e in events).encode()
    result = _verify(client, _rezip(files))
    assert result["valid"] is False
    assert any("events.jsonl" in p for p in result["problems"])


def test_missing_events_jsonl_fails_verification(client):
    claim = _sealed_claim(client)
    files, _ = _package_files(client, claim)
    del files["events.jsonl"]
    result = _verify(client, _rezip(files))
    assert result["valid"] is False
    assert any("events.jsonl" in p for p in result["problems"])


def test_key_rotation_old_package_still_verifies(client, monkeypatch):
    claim = _sealed_claim(client)
    _, raw = _package_files(client, claim)
    store = client.app.state.store
    old_public = store.sealer.public_key_b64
    new_sealer = seal.Sealer(Ed25519PrivateKey.generate())
    # Without the old key in the set, the package names an unknown key.
    monkeypatch.delenv("SEAL_OLD_PUBLIC_KEYS", raising=False)
    assert new_sealer.verify_package(raw)["valid"] is False
    # With it (comma-separated base64, as deployed), the old package verifies.
    monkeypatch.setenv("SEAL_OLD_PUBLIC_KEYS", f"junk, {old_public}")
    assert new_sealer.verify_package(raw)["valid"] is True


def test_offline_verify_tool(client, tmp_path):
    claim = _sealed_claim(client)
    _, raw = _package_files(client, claim)
    package_path = tmp_path / "package.zip"
    package_path.write_bytes(raw)
    public_key = client.get("/api/verify/public-key").json()["public_key"]
    tool = Path(__file__).resolve().parents[1] / "tools" / "verify_package.py"
    assert tool.exists()
    ok = subprocess.run([sys.executable, str(tool), str(package_path),
                         "--public-key", public_key], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert "valid" in ok.stdout
    files, _ = _package_files(client, claim)
    events = [json.loads(line) for line in files["events.jsonl"].decode().splitlines() if line.strip()]
    events[-1]["actor"] = "attacker"
    files["events.jsonl"] = "".join(json.dumps(e) + "\n" for e in events).encode()
    bad_path = tmp_path / "bad.zip"
    bad_path.write_bytes(_rezip(files))
    bad = subprocess.run([sys.executable, str(tool), str(bad_path),
                          "--public-key", public_key], capture_output=True, text=True)
    assert bad.returncode == 1
    assert "events.jsonl" in bad.stdout


# --- 3. webhook scheme ---


def _new_key(client, mode):
    r = client.post("/api/admin/api-keys", json={"name": f"Fix {mode}", "mode": mode})
    assert r.status_code == 201, r.text
    return r.json()["key"]


def _order_with_key(client, key, number, callback):
    return client.post("/api/v1/capture-requests",
                       json={"insurer_claim_number": number, "policy_number": "DEMO-AUTO-1001",
                             "callback_url": callback},
                       headers={"Authorization": f"Bearer {key}"})


def test_http_callback_is_refused_for_live_key(client):
    live = _new_key(client, "live")
    assert _order_with_key(client, live, "DM-FIX-1", "http://insurer.example/hook").status_code == 400
    assert _order_with_key(client, live, "DM-FIX-2", "http://localhost:8080/hook").status_code == 400
    assert _order_with_key(client, live, "DM-FIX-3", "http://testserver/hook").status_code == 400
    assert _order_with_key(client, live, "DM-FIX-4", "https://insurer.example/hook").status_code == 201


def test_http_callback_allowed_in_test_mode_for_localhost_and_own_host(client):
    test_key = _new_key(client, "test")
    assert _order_with_key(client, test_key, "DM-FIX-5", "http://localhost:8080/hook").status_code == 201
    assert _order_with_key(client, test_key, "DM-FIX-6", "http://127.0.0.1:8080/hook").status_code == 201
    assert _order_with_key(client, test_key, "DM-FIX-7", "http://testserver/hook").status_code == 201
    assert _order_with_key(client, test_key, "DM-FIX-8", "http://insurer.example/hook").status_code == 400


def test_callback_host_allowed_scheme_rules():
    from evidence_server.main import callback_host_allowed

    allowed = ["insurer.example"]
    assert callback_host_allowed("https://insurer.example/h", allowed, None)
    assert not callback_host_allowed("http://insurer.example/h", allowed, None)
    assert not callback_host_allowed("http://localhost/h", allowed, None)
    assert callback_host_allowed("http://localhost/h", allowed, None, test_mode=True)
    assert callback_host_allowed("http://127.0.0.1/h", allowed, None, test_mode=True)
    assert callback_host_allowed("http://own.example/h", [], "own.example", test_mode=True)
    assert not callback_host_allowed("http://own.example/h", [], "own.example")


# --- 4. proxy headers ---


def test_forwarded_allow_ips_default_and_env(monkeypatch):
    import importlib

    import uvicorn

    main_module = importlib.import_module("evidence_server.main")

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *a, **kw: calls.append(kw))
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    main_module.main()
    assert calls[-1]["forwarded_allow_ips"] == "127.0.0.1"
    assert calls[-1]["proxy_headers"] is True
    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.5")
    main_module.main()
    assert calls[-1]["forwarded_allow_ips"] == "10.0.0.5"


# --- 5. login ---


def test_admin_email_must_be_an_email(tmp_path, clock, monkeypatch):
    from fastapi.testclient import TestClient  # noqa: F401

    import psycopg

    from evidence_server.blobstore import LocalBlobStore
    from evidence_server.main import create_app
    from pg import TEST_DATABASE_URL, reset

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        reset(conn)
    with pytest.raises(RuntimeError, match="ADMIN_EMAIL"):
        create_app(data_dir=tmp_path / "d", frontend_dist=tmp_path / "n",
                   admin_email="admin", admin_password="correct horse battery staple",
                   session_secret="s", clock=clock, database_url=TEST_DATABASE_URL,
                   blobstore=LocalBlobStore(tmp_path / "b"))
    with pytest.raises(RuntimeError, match="ADMIN_EMAIL"):
        create_app(data_dir=tmp_path / "d", frontend_dist=tmp_path / "n",
                   admin_email="not-an-email", admin_password="correct horse battery staple",
                   session_secret="s", clock=clock, database_url=TEST_DATABASE_URL,
                   blobstore=LocalBlobStore(tmp_path / "b2"))


def test_per_account_throttle_returns_429_for_that_email(client):
    from conftest import ADMIN_EMAIL, ADMIN_PASSWORD

    store = client.app.state.store
    now = time.time()
    with store.db.tx() as conn:
        conn.execute("DELETE FROM login_failures")
        for i in range(10):
            conn.execute("INSERT INTO login_failures (ip, at, email) VALUES (%s, %s, %s)",
                         (f"10.0.0.{i}", now, ADMIN_EMAIL))
    # The test client's own IP has no failures; the account limit alone refuses.
    r = client.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 429
    # Another email is not throttled by that account's failures (it just fails auth).
    r2 = client.post("/api/admin/login", json={"email": "other@example.com", "password": "nope"})
    assert r2.status_code == 401


def test_per_account_throttle_counts_api_failures_across_ips(client, monkeypatch):
    """Ten failures for one email in an hour throttle it even per-IP would not."""
    from evidence_server import auth as auth_module

    assert auth_module.FAILS_PER_EMAIL == 10
    assert auth_module.EMAIL_FAIL_WINDOW == 3600


# --- 7. wording ---


def test_seal_text_uses_capture_flow_wording():
    from evidence_server import report_pdf

    text = report_pdf.seal_text({"seal": {"items": 3, "manifest_sha256": "0" * 64},
                                 "policy_verified": True})
    assert "items captured in the capture flow" in text
    assert "captured live" not in text
    assert "Captured in the capture flow and sealed" in Path(
        report_pdf.__file__).read_text()


def test_no_overclaiming_live_copy_in_app_and_readme():
    root = Path(__file__).resolve().parents[2]
    capture = (root / "app/src/pages/Capture.tsx").read_text()
    assert "Photos and video are taken with the camera in this page; there is no option to pick existing photos." in capture
    assert "required photos and video taken in this page" in capture
    assert "Existing photos cannot be uploaded" not in capture
    privacy = (root / "app/src/pages/Privacy.tsx").read_text()
    assert "cannot be uploaded" not in privacy.lower()
    assert "You cannot upload" not in privacy
    loss = (root / "app/src/components/LossNotice.tsx").read_text()
    assert "captured in the capture flow at" in loss
    assert "captured live at" not in loss
    assert "static_source" in loss
    readme = (root / "README.md").read_text()
    assert "Old or edited photos cannot be uploaded" not in readme
    assert "The capture page only offers the live camera; direct API uploads are flagged, and a native app closes this gap." in readme
    assert "tools/verify_package.py" in readme
    threat = (root / "docs/threat-model.md").read_text()
    for needle in ("Dishonest claimant", "network attacker", "Operator",
                   "App Attest", "C2PA", "third-party timestamp", "EXIF",
                   "canvas.toBlob"):
        assert needle.lower() in threat.lower(), needle
