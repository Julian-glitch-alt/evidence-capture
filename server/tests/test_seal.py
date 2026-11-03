"""Sealing on submit, the package an adjuster downloads, and the public Verify check."""

import base64
import io
import json
import zipfile

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from evidence_server import seal
from test_loss_notice import DAMAGE_ANSWERS, capture_required, open_claim, save


@pytest.fixture
def sealed(client):
    claim = open_claim(client)
    capture_required(client, claim["token"], save(client, claim["token"], DAMAGE_ANSWERS))
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 200
    return claim


def package(client, claim) -> dict[str, bytes]:
    r = client.get(f"/api/admin/claims/{claim['id']}/package")
    assert r.status_code == 200, r.text
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        return {n: z.read(n) for n in z.namelist()}


def rezip(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def verify(client, data: bytes) -> dict:
    r = client.post("/api/verify", files={"file": ("claim.zip", data, "application/zip")})
    assert r.status_code == 200, r.text
    return r.json()


def test_submit_seals_the_claim(client, sealed):
    d = client.get(f"/api/admin/claims/{sealed['id']}").json()
    assert d["seal"]["sealed_at"]
    # Sealing is the last step of the claimant's submit; analysis (T13 coverage check) follows it.
    # The damage reading (T20) and the consistency check (T21) run after the response and log after these.
    after = {"damage.read", "consistency.checked", "report.ready"}  # T29a report follows the analysis
    assert [e["type"] for e in d["events"] if e["type"] not in after][-2:] == ["claim.sealed", "coverage.checked"]


def test_untouched_package_verifies(client, sealed):
    files = package(client, sealed)
    assert {"manifest.json", "signature.txt", "public_key.txt", "answers.json", "policy_snapshot.json"} <= set(files)
    manifest = json.loads(files["manifest.json"])
    assert len(manifest["items"]) == 8
    result = verify(client, rezip(files))
    assert result["valid"] is True, result
    assert result["claim_id"] == sealed["id"]
    assert result["problems"] == []


def first_photo(files):
    return next(n for n in files if n.startswith("files/plate-"))


@pytest.mark.parametrize(
    "tamper, expect",
    [
        ("photo_byte", "files/plate-"),
        ("answers", "answers.json"),
        ("removed_item", "files/plate-"),
        ("added_item", "files/extra.jpg"),
        ("manifest_field", "manifest"),
        ("other_key", "key"),
    ],
)
def test_every_tamper_is_caught_and_named(client, sealed, tamper, expect):
    files = package(client, sealed)
    if tamper == "photo_byte":
        name = first_photo(files)
        data = bytearray(files[name])
        data[-1] ^= 1
        files[name] = bytes(data)
    elif tamper == "answers":
        answers = json.loads(files["answers.json"])
        answers["description"] = "Something else entirely."
        files["answers.json"] = json.dumps(answers).encode()
    elif tamper == "removed_item":
        del files[first_photo(files)]
    elif tamper == "added_item":
        files["files/extra.jpg"] = b"\xff\xd8\xffnot-captured-live"
    elif tamper == "manifest_field":
        m = json.loads(files["manifest.json"])
        m["loss_type"] = "something_else"
        files["manifest.json"] = json.dumps(m).encode()
    elif tamper == "other_key":
        key = Ed25519PrivateKey.generate()
        files["signature.txt"] = base64.b64encode(key.sign(files["manifest.json"]))
        files["public_key.txt"] = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    result = verify(client, rezip(files))
    assert result["valid"] is False
    assert any(expect in p for p in result["problems"]), result["problems"]


def test_file_changed_on_the_server_disk_is_caught(client, sealed):
    store = client.app.state.store
    with store.db.tx() as conn:
        key = conn.execute(
            "SELECT path FROM items WHERE claim_id=%s AND step_id='plate' AND superseded=0",
            (sealed["id"],),
        ).fetchone()["path"]
    store.blobs.put(key, store.blobs.get(key) + b"edited later", "image/jpeg")
    result = verify(client, rezip(package(client, sealed)))
    assert result["valid"] is False
    assert any("files/plate-" in p for p in result["problems"])


def test_garbage_upload_is_not_a_valid_package(client):
    result = verify(client, b"this is not a zip")
    assert result["valid"] is False


def test_zip_bomb_is_refused_without_unpacking(client, monkeypatch):
    monkeypatch.setattr(seal, "MAX_UNPACKED_BYTES", 1024 * 1024)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", "{}")
        z.writestr("signature.txt", "x")
        z.writestr("files/bomb.bin", b"\0" * (5 * 1024 * 1024))  # ~5 KB packed, 5 MB unpacked
    result = verify(client, buf.getvalue())
    assert result["valid"] is False
    assert "too large" in result["problems"][0]


@pytest.mark.parametrize(
    "tamper",
    [
        lambda f: f.update({"public_key.txt": b"not base64!"}),
        lambda f: f.update({"signature.txt": b"not base64!"}),
        lambda f: f.update({"manifest.json": b"[1, 2, 3]"}),
        lambda f: f.update({"manifest.json": json.dumps({"items": ["x", 5]}).encode()}),
    ],
    ids=["bad-key-encoding", "bad-signature-encoding", "manifest-not-object", "items-not-objects"],
)
def test_malformed_package_is_invalid_not_a_server_error(client, sealed, tamper):
    files = package(client, sealed)
    tamper(files)
    assert verify(client, rezip(files))["valid"] is False


def test_public_key_is_published(anon, client, sealed):
    key = anon.get("/api/verify/public-key").json()["public_key"]
    assert base64.b64decode(package(client, sealed)["public_key.txt"]) == base64.b64decode(key)


def test_unsubmitted_claim_has_no_package(client):
    claim = open_claim(client)
    assert client.get(f"/api/admin/claims/{claim['id']}/package").status_code == 409
