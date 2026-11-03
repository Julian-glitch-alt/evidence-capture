"""Review fixes: add_item without the claim lock during upload, package 500, open_claim retry."""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import JPEG
from evidence_server.errors import CaptureError


def hold_claim(store, claim_id: str, seconds: float) -> threading.Thread:
    taken = threading.Event()

    def hold():
        with store.db.claim_tx(claim_id=claim_id):
            taken.set()
            time.sleep(seconds)

    thread = threading.Thread(target=hold)
    thread.start()
    assert taken.wait(5)
    return thread


def test_add_item_on_other_claim_does_not_wait_and_holds_no_lock_during_upload(client):
    store = client.app.state.store
    a = store.open_claim("DEMO-AUTO-1001")
    b = store.open_claim("DEMO-AUTO-1001")
    session = store.start_session(b["token"])["session_id"]
    nonce = store.issue_nonce(b["token"], session, "plate")["nonce"]

    lock_free = threading.Event()
    orig_put = store.blobs.put

    def checking_put(key, data, content_type):
        try:
            with store.db.tx() as conn:
                conn.execute("SELECT * FROM claims WHERE id=%s FOR UPDATE NOWAIT", (b["id"],))
            lock_free.set()
        except Exception:
            pass
        return orig_put(key, data, content_type)

    store.blobs.put = checking_put
    try:
        holder = hold_claim(store, a["id"], 1.5)
        t0 = time.monotonic()
        item = store.add_item(b["token"], "plate", JPEG, "image/jpeg", {}, session, nonce)
        elapsed = time.monotonic() - t0
        holder.join()
    finally:
        store.blobs.put = orig_put
    assert item["step_id"] == "plate"
    assert elapsed < 1.0, f"add_item on B waited {elapsed:.2f}s for A's lock"
    assert lock_free.is_set(), "add_item held B's claim lock during the blob upload"


def test_failed_add_item_leaves_no_blob(client):
    store = client.app.state.store
    claim = store.open_claim("DEMO-AUTO-1001")
    session = store.start_session(claim["token"])["session_id"]
    for n in range(6):
        nonce = store.issue_nonce(claim["token"], session, "damage_closeup")["nonce"]
        store.add_item(claim["token"], "damage_closeup", JPEG + bytes([n]), "image/jpeg", {}, session, nonce)

    seen: list[str] = []
    orig_put = store.blobs.put

    def recording_put(key, data, content_type):
        seen.append(key)
        return orig_put(key, data, content_type)

    store.blobs.put = recording_put
    try:
        nonce = store.issue_nonce(claim["token"], session, "damage_closeup")["nonce"]
        with pytest.raises(CaptureError) as exc:
            store.add_item(claim["token"], "damage_closeup", JPEG + b"7th", "image/jpeg", {}, session, nonce)
    finally:
        store.blobs.put = orig_put
    assert exc.value.status == 400
    assert seen, "the failing upload never reached the blob store"
    assert store.blobs.exists(seen[-1]) is False


def test_package_with_missing_blob_is_500(client):
    from test_loss_notice import DAMAGE_ANSWERS, capture_required, open_claim, save

    claim = open_claim(client)
    capture_required(client, claim["token"], save(client, claim["token"], DAMAGE_ANSWERS))
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 200
    store = client.app.state.store
    with store.db.tx() as conn:
        row = conn.execute(
            "SELECT id, path FROM items WHERE claim_id=%s AND superseded=0 LIMIT 1", (claim["id"],)
        ).fetchone()
    store.blobs.delete(row["path"])
    r = client.get(f"/api/admin/claims/{claim['id']}/package")
    assert r.status_code == 500
    assert "A sealed file is missing from storage:" in r.json()["detail"]


def test_open_claim_retries_on_id_collision(client, monkeypatch):
    from evidence_server import store as store_module

    store = client.app.state.store
    existing = store.open_claim("DEMO-AUTO-1001")
    real_new = store_module._new_claim_id
    calls = {"n": 0}

    def fake(local_day):
        calls["n"] += 1
        if calls["n"] == 1:
            return existing["id"]
        return real_new(local_day)

    monkeypatch.setattr(store_module, "_new_claim_id", fake)
    claim = store.open_claim("DEMO-AUTO-1001")
    assert claim["id"] != existing["id"]
    assert calls["n"] >= 2
