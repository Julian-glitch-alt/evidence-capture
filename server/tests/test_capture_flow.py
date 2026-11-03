"""Capture rules that hold for every slot: media type, retakes, caps, locking after submit."""

from conftest import JPEG, MP4, Phone
from test_loss_notice import DAMAGE_ANSWERS, capture_required, open_claim, save


def test_full_flow_is_stored_and_listed(client):
    claim = open_claim(client)
    capture_required(client, claim["token"], save(client, claim["token"], DAMAGE_ANSWERS))
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 200

    detail = client.get(f"/api/admin/claims/{claim['id']}").json()
    assert detail["status"] == "submitted"
    assert detail["answers"]["damaged_areas"] == ["Rear bumper"]
    assert len(detail["items"]) == 8  # 7 photos / video + signature
    item = detail["items"][0]
    assert len(item["sha256"]) == 64
    assert client.get(f"/api/admin/items/{item['id']}/file").content in (JPEG, MP4)
    assert client.get("/api/admin/claims").json()[0]["policy_number"] == "DEMO-AUTO-1001"


def test_nothing_changes_after_submit(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    capture_required(client, claim["token"], save(client, claim["token"], DAMAGE_ANSWERS))
    client.post(f"/api/c/{claim['token']}/submit", json={})

    assert client.post(f"/api/c/{claim['token']}/session").status_code == 409
    assert phone.upload("plate", nonce="any").status_code == 409
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 409


def test_wrong_media_and_unknown_link_are_refused(client):
    claim = open_claim(client)
    assert Phone(client, claim["token"]).upload("walkaround").status_code == 400  # photo for a video slot
    phone = Phone(client, claim["token"])
    r = client.post(f"/api/c/{claim['token']}/nonce", json={"session_id": phone.session["session_id"], "slot_id": "nope"})
    assert r.status_code == 400
    assert client.post("/api/c/not-a-token/session").status_code == 404


def test_retake_replaces_single_shot_and_caps_multi_shot(client):
    claim = open_claim(client)
    token = claim["token"]
    phone = Phone(client, token)
    phone.upload("plate")
    phone.upload("plate", data=JPEG + b"retake")
    state = client.get(f"/api/c/{token}").json()
    assert len([i for i in state["items"] if i["step_id"] == "plate"]) == 1

    for n in range(6):
        assert phone.upload("damage_closeup", data=JPEG + bytes([n])).status_code == 200
    assert phone.upload("damage_closeup", data=JPEG + b"7").status_code == 400


def test_claimant_sees_own_photos_only_through_own_link(client):
    claim = open_claim(client)
    other = open_claim(client)
    item = Phone(client, claim["token"]).upload("plate").json()

    assert client.get(f"/api/c/{claim['token']}/items/{item['id']}/file").content == JPEG
    assert client.get(f"/api/c/{other['token']}/items/{item['id']}/file").status_code == 404
