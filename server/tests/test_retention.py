"""T50 (tasks/t50-design.md): media removed 90 days after the decision or on request; the seal still verifies."""

import io
import json
import zipfile

from evidence_server.blobstore import BlobNotFound
from test_damage_flow import db_rows
from test_decision_flow import decide
from test_loss_notice import detail
from test_review_flow import ready
from test_users import signed_in

REASON = "The claimant asked us to delete their photos."


def blobs_left(client, claim_id):
    store = client.app.state.store
    left = 0
    for row in db_rows(client, "SELECT path FROM items WHERE claim_id=%s", claim_id):
        try:
            store.blobs.get(row["path"])
            left += 1
        except BlobNotFound:
            pass
    return left


def decided(client, clock):
    claim = ready(client, clock)
    r = decide(client, claim["id"], action="approve", reason="Checked the photos myself.")
    assert r.status_code == 200, r.text
    return claim


def sweep(client):
    r = client.post("/api/admin/sweep")
    assert r.status_code == 200, r.text
    return r.json()


def removed_events(client, claim):
    return [e for e in detail(client, claim)["events"] if e["type"] == "media.removed"]


def package(client, claim):
    r = client.get(f"/api/admin/claims/{claim['id']}/package")
    assert r.status_code == 200, r.text
    return r.content


# --- retention ---


def test_media_is_removed_90_days_after_the_decision(client, clock):
    claim = decided(client, clock)
    before = blobs_left(client, claim["id"])
    assert before > 0
    clock.advance(days=89)
    assert sweep(client)["retention"] == 0 and blobs_left(client, claim["id"]) == before
    clock.advance(days=2)
    assert sweep(client)["retention"] == 1
    assert blobs_left(client, claim["id"]) == 0
    [event] = removed_events(client, claim)
    assert (event["data"]["reason"], event["data"]["note"], event["data"]["user_id"]) == ("retention", None, None)
    assert event["data"]["files"] == before
    assert client.app.state.store.verify_chain(claim["id"])["ok"]
    assert sweep(client)["retention"] == 0  # nothing left to remove


def test_a_claim_without_a_decision_keeps_its_media(client, clock):
    claim = ready(client, clock)
    clock.advance(days=400)
    sweep(client)
    assert blobs_left(client, claim["id"]) > 0 and not removed_events(client, claim)


def test_an_undelivered_report_keeps_the_media(client, clock):
    claim = decided(client, clock)
    with client.app.state.store.db.tx() as conn:  # a message still on its way to the insurer
        conn.execute(
            "INSERT INTO outbox (id, claim_id, event, body_json, created_at, attempts, next_attempt_at)"
            " VALUES ('ob-test', %s, 'test.pending', '{}', %s, 1, '2999-01-01T00:00:00+00:00')",
            (claim["id"], clock.now().isoformat()),
        )
    clock.advance(days=91)
    sweep(client)
    assert blobs_left(client, claim["id"]) > 0


def test_the_retention_window_is_a_supervisor_setting(anon, client, clock):
    assert client.get("/api/admin/settings").json()["retention_days"] == 90
    for bad in (29, 3651, "90", None, 30.5):
        assert client.put("/api/admin/settings", json={"retention_days": bad}).status_code == 400, bad
    assert client.put("/api/admin/settings", json={"retention_days": 30}).status_code == 200
    claim = decided(client, clock)
    clock.advance(days=31)
    sweep(client)
    assert blobs_left(client, claim["id"]) == 0
    maria, _ = signed_in(anon, client)
    assert maria.put("/api/admin/settings", json={"retention_days": 60}).status_code == 403


# --- on request ---


def test_a_supervisor_deletes_a_claims_media_on_request(anon, client, clock):
    claim = ready(client, clock)
    maria, _ = signed_in(anon, client)
    url = f"/api/admin/claims/{claim['id']}/delete-media"
    assert maria.post(url, json={"reason": REASON}).status_code == 403
    assert client.post(url, json={"reason": "no"}).status_code == 400
    assert client.post(url, json={}).status_code == 400
    r = client.post(url, json={"reason": REASON})
    assert r.status_code == 200, r.text
    assert blobs_left(client, claim["id"]) == 0
    [event] = removed_events(client, claim)
    assert (event["data"]["reason"], event["data"]["note"], event["data"]["user_name"]) == ("request", REASON, "Administrator")
    r = client.post(url, json={"reason": REASON})
    assert (r.status_code, r.json()["detail"]) == (409, "This claim's media was already deleted")


def test_an_unsent_claims_media_cannot_be_deleted(client):
    claim = client.post("/api/admin/claims", json={"policy_number": "DEMO-AUTO-1001"}).json()
    r = client.post(f"/api/admin/claims/{claim['id']}/delete-media", json={"reason": REASON})
    assert (r.status_code, r.json()["detail"]) == (409, "Only a sent claim's media can be deleted")


def test_removed_files_answer_410(client, clock):
    claim = ready(client, clock)
    [item] = db_rows(client, "SELECT id FROM items WHERE claim_id=%s LIMIT 1", claim["id"])
    client.post(f"/api/admin/claims/{claim['id']}/delete-media", json={"reason": REASON})
    assert client.get(f"/api/admin/items/{item['id']}/file").status_code == 410
    assert client.get(f"/api/c/{claim['token']}/items/{item['id']}/file").status_code == 410


# --- the seal still verifies ---


def test_the_package_still_verifies_and_says_what_was_removed(client, clock):
    claim = decided(client, clock)
    client.post(f"/api/admin/claims/{claim['id']}/delete-media", json={"reason": REASON})
    pkg = package(client, claim)
    names = zipfile.ZipFile(io.BytesIO(pkg)).namelist()
    assert "removal.json" in names and "removal.sig" in names
    assert not any(n.startswith("files/") for n in names)
    result = client.app.state.store.verify_package(pkg)
    assert result["valid"] is True, result["problems"]
    assert (result["removed"], result["removed_reason"]) == (result["items"], "request") and result["removed_at"]
    public = client.post("/api/verify", files={"file": ("claim.zip", pkg, "application/zip")}).json()
    assert public["valid"] is True and public["removed"] == result["removed"]


def test_a_forged_removal_receipt_is_caught(client, clock):
    claim = decided(client, clock)
    client.post(f"/api/admin/claims/{claim['id']}/delete-media", json={"reason": REASON})
    original = zipfile.ZipFile(io.BytesIO(package(client, claim)))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as forged:
        for info in original.infolist():
            data = original.read(info)
            if info.filename == "removal.json":
                receipt = json.loads(data)
                receipt["removed"] = receipt["removed"][1:]  # claim one file was never removed
                data = json.dumps(receipt).encode()
            forged.writestr(info, data)
    result = client.app.state.store.verify_package(out.getvalue())
    assert result["valid"] is False
    assert "removal.json was changed or is not signed by this server" in result["problems"]


def test_packages_without_removals_verify_as_before(client, clock):
    claim = decided(client, clock)
    result = client.app.state.store.verify_package(package(client, claim))
    assert result["valid"] is True and (result["removed"], result["removed_at"], result["removed_reason"]) == (0, None, None)


# --- at scale ---


def test_old_claims_without_a_decision_never_hold_others_back(client, clock, monkeypatch):
    from evidence_server import store as store_mod

    monkeypatch.setattr(store_mod, "RETENTION_SWEEP_LIMIT", 2)
    for _ in range(9):  # sent long ago, never decided
        ready(client, clock)
    claim = decided(client, clock)
    clock.advance(days=91)
    sweep(client)
    assert blobs_left(client, claim["id"]) == 0


def test_blobs_are_deleted_once_not_on_every_sweep(client, clock):
    claim = ready(client, clock)
    client.post(f"/api/admin/claims/{claim['id']}/delete-media", json={"reason": REASON})
    store = client.app.state.store
    calls = []
    original = store.blobs.delete

    def counting(key):
        calls.append(key)
        return original(key)

    store.blobs.delete = counting
    sweep(client)
    sweep(client)
    assert calls == []


def test_a_failed_blob_delete_is_finished_by_the_next_sweep(client, clock):
    claim = ready(client, clock)
    store = client.app.state.store
    original = store.blobs.delete

    def down(key):
        raise OSError("storage down")

    store.blobs.delete = down
    r = client.post(f"/api/admin/claims/{claim['id']}/delete-media", json={"reason": REASON})
    assert r.status_code == 200, r.text  # the rows say removed; the files go when storage is back
    assert blobs_left(client, claim["id"]) > 0
    store.blobs.delete = original
    sweep(client)
    assert blobs_left(client, claim["id"]) == 0
