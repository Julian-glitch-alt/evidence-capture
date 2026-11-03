import re
from pathlib import Path

import pytest

from conftest import JPEG, Phone


@pytest.fixture
def claim(client):
    return client.post("/api/admin/claims", json={"policy_number": "DEMO-AUTO-1001"}).json()


def stored_files(tmp_path: Path) -> list[Path]:
    root = tmp_path / "blobs"
    return [p for p in root.rglob("*") if p.is_file()] if root.exists() else []


def test_session_start_gives_code_and_moves_claim_to_capturing(client, claim):
    phone = Phone(client, claim["token"])

    assert re.fullmatch(r"[A-HJ-NP-Z2-9]{5}", phone.session["code"])
    detail = client.get(f"/api/admin/claims/{claim['id']}").json()
    assert detail["status"] == "capturing"
    assert [e["type"] for e in detail["events"]] == ["claim.opened", "claim.capturing", "capture.session_started"]
    assert detail["sessions"][-1]["code"] == phone.session["code"]  # what the adjuster sees


def test_upload_with_session_and_nonce_is_stored_with_event(client, claim):
    phone = Phone(client, claim["token"])
    r = phone.upload("plate")
    assert r.status_code == 200, r.text

    events = client.get(f"/api/admin/claims/{claim['id']}").json()["events"]
    received = [e for e in events if e["type"] == "item.received"]
    assert received[-1]["data"]["slot"] == "plate"
    assert received[-1]["data"]["sha256"] == r.json()["sha256"]


@pytest.mark.parametrize(
    "attack",
    ["no_session", "no_nonce", "made_up_nonce", "reused_nonce", "other_slot_nonce", "expired_nonce", "expired_session", "old_session"],
)
def test_upload_outside_a_valid_session_is_refused_and_not_stored(client, claim, clock, tmp_path, attack):
    phone = Phone(client, claim["token"])
    kwargs = {}
    if attack == "no_session":
        kwargs = {"session_id": "none"}
    elif attack == "no_nonce":
        kwargs = {"nonce": ""}
    elif attack == "made_up_nonce":
        kwargs = {"nonce": "x" * 22}
    elif attack == "reused_nonce":
        nonce = phone.nonce("plate")
        assert phone.upload("plate", nonce=nonce).status_code == 200
        kwargs = {"nonce": nonce, "data": JPEG + b"a different file"}
    elif attack == "other_slot_nonce":
        kwargs = {"nonce": phone.nonce("vin_photo")}
    elif attack == "expired_nonce":
        nonce = phone.nonce("plate")
        clock.advance(minutes=4)
        kwargs = {"nonce": nonce}
    elif attack == "expired_session":
        nonce = phone.nonce("plate")
        clock.advance(minutes=31)
        kwargs = {"nonce": nonce}
    elif attack == "old_session":
        nonce = phone.nonce("plate")
        Phone(client, claim["token"])  # a second device starts a new session
        kwargs = {"nonce": nonce}
    before = stored_files(tmp_path)

    r = phone.upload("plate", **kwargs)

    assert r.status_code == 403, r.text
    assert stored_files(tmp_path) == before


def test_activity_renews_the_session(client, claim, clock):
    phone = Phone(client, claim["token"])
    for _ in range(4):  # 4 x 20 min = 80 min, each step renews the 30 min window
        clock.advance(minutes=20)
        assert phone.upload("plate").status_code == 200


def test_retake_writes_retaken_event(client, claim):
    phone = Phone(client, claim["token"])
    phone.upload("plate")
    phone.upload("plate", data=JPEG + b"2")
    types = [e["type"] for e in client.get(f"/api/admin/claims/{claim['id']}").json()["events"]]
    assert types.count("item.received") == 2
    assert types.count("item.retaken") == 1


def test_blob_written_before_row(client, claim, tmp_path, monkeypatch):
    """The blob lands before its row; when the insert fails the blob is gone (I5)."""
    from evidence_server import lifecycle
    from evidence_server.store import CaptureError

    store = client.app.state.store
    phone = Phone(client, claim["token"])
    before = stored_files(tmp_path)

    def boom(*args, **kwargs):
        raise CaptureError("row insert failed", 500)

    monkeypatch.setattr(lifecycle, "append_event", boom)
    r = phone.upload("plate")

    assert r.status_code == 500
    assert stored_files(tmp_path) == before
    with store.db.tx() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM items WHERE claim_id=%s", (claim["id"],)).fetchone()["n"] == 0
