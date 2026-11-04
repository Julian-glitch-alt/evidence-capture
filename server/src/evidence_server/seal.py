"""Seal a submitted claim and verify a sealed package.

On submit the server signs a manifest (Ed25519) listing the hash of every file it
received, the answers, the policy snapshot and the head of the event chain. The
package an adjuster downloads holds those files plus the manifest and signature, so
anyone can check later that nothing was changed since the server received it. It does
not prove the photos are real; it proves they are the ones we received.
"""

import base64
import hashlib
import io
import json
import os
import zipfile
import zlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from .policies import canonical

FORMAT = "evidence-seal/1"
EXT = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"}
README = """This package was sealed by the claims server when the claim was submitted.

manifest.json lists every file with its SHA-256, plus the hash of answers.json and
policy_snapshot.json. signature.txt is an Ed25519 signature over manifest.json made
with the key in public_key.txt. events.jsonl holds every claim event with its
seq, prev_hash and hash; its head and count are in the manifest. Upload this zip
to the Verify page to check it, or verify offline with tools/verify_package.py.

Captured in the capture flow and sealed: a valid seal proves the files are the
ones the server received. It does not prove that a photo shows what it claims
to show.
"""
MAX_ENTRIES = 500  # a claim has a few dozen files at most
MAX_UNPACKED_BYTES = 250 * 1024 * 1024  # the upload cap (200 MB) plus room for stored-zip overhead


# Read whole (they are parsed or are tiny); every other entry is hashed in chunks.
SMALL_FILES = {"manifest.json", "signature.txt", "public_key.txt"}
MAX_SMALL_FILE_BYTES = 5 * 1024 * 1024
_CHUNK = 1024 * 1024


def _sha256_entry(z: zipfile.ZipFile, info: zipfile.ZipInfo) -> str:
    h = hashlib.sha256()
    with z.open(info) as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def invalid(problem: str) -> dict:
    return {"valid": False, "claim_id": None, "sealed_at": None, "items": 0,
            "removed": 0, "removed_at": None, "removed_reason": None, "problems": [problem]}


GENESIS = "0" * 64


def key_id_for(public_raw: bytes) -> str:
    return hashlib.sha256(public_raw).hexdigest()[:16]


def _event_hash(event: dict) -> str:
    body = {k: v for k, v in event.items() if k not in ("id", "hash")}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def verify_event_chain(events: list) -> tuple[str, str | None]:
    """Recompute the chain; -> (head, problem or None)."""
    prev = GENESIS
    for i, event in enumerate(events, start=1):
        if not isinstance(event, dict):
            return prev, f"events.jsonl entry {i} is not an event record."
        if event.get("seq") != i:
            return prev, f"events.jsonl chain is broken at entry {i}: seq is out of order."
        if event.get("prev_hash") != prev:
            return prev, f"events.jsonl chain is broken at seq {i}: prev_hash does not match."
        if _event_hash(event) != event.get("hash"):
            return prev, f"events.jsonl chain is broken at seq {i}: hash does not match."
        prev = event["hash"]
    return prev, None


def parse_events_jsonl(data: bytes) -> tuple[list, str | None]:
    events = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            events.append(json.loads(line))
        except ValueError:
            return [], "events.jsonl is not readable."
    return events, None


class Sealer:
    def __init__(self, private_key: Ed25519PrivateKey):
        self.key = private_key
        self.public_raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        self.key_id = hashlib.sha256(self.public_raw).hexdigest()[:16]

    @classmethod
    def from_env_or_dev_file(cls, data_dir: Path) -> "Sealer":
        """SEAL_PRIVATE_KEY (base64 raw 32 bytes) when deployed; a local key file in development."""
        encoded = os.environ.get("SEAL_PRIVATE_KEY")
        if encoded:
            return cls(Ed25519PrivateKey.from_private_bytes(base64.b64decode(encoded)))
        dev_file = data_dir / "dev_seal_key"
        if not dev_file.exists():
            data_dir.mkdir(parents=True, exist_ok=True)
            dev_file.write_bytes(base64.b64encode(Ed25519PrivateKey.generate().private_bytes_raw()))
            dev_file.chmod(0o600)
        return cls(Ed25519PrivateKey.from_private_bytes(base64.b64decode(dev_file.read_bytes())))

    @property
    def public_key_b64(self) -> str:
        return base64.b64encode(self.public_raw).decode()

    def known_public_keys(self) -> dict[str, bytes]:
        """key_id -> raw public key: the current key plus SEAL_OLD_PUBLIC_KEYS
        (comma-separated base64), so rotation does not break old packages."""
        keys = {self.key_id: self.public_raw}
        for part in os.environ.get("SEAL_OLD_PUBLIC_KEYS", "").split(","):
            part = part.strip()
            if not part:
                continue
            try:
                raw = base64.b64decode(part, validate=True)
            except ValueError:
                continue
            if len(raw) == 32:
                keys[key_id_for(raw)] = raw
        return keys

    def verify_key_for(self, manifest: dict) -> bytes | None:
        """The key named by manifest.key_id from the known set, or None."""
        kid = manifest.get("key_id") if isinstance(manifest, dict) else None
        keys = self.known_public_keys()
        if isinstance(kid, str) and kid:
            return keys.get(kid)
        return self.public_raw

    def sign(self, manifest: dict) -> tuple[bytes, str]:
        body = canonical(manifest).encode()
        return body, base64.b64encode(self.key.sign(body)).decode()

    def verify_package(self, data) -> dict:
        """data: the package bytes or a seekable binary file object.

        Memory stays small whatever the package size: evidence files are hashed in
        chunks straight from the zip; only the few small metadata files are read whole.
        """
        problems: list[str] = []
        try:
            z = zipfile.ZipFile(io.BytesIO(data) if isinstance(data, (bytes, bytearray)) else data)
            entries = [i for i in z.infolist() if not i.is_dir()]
            # Sizes from the zip headers; reading stops at the declared size, so a lying header fails the CRC instead.
            if len(entries) > MAX_ENTRIES or sum(i.file_size for i in entries) > MAX_UNPACKED_BYTES:
                return invalid("This package is too large or has too many files to be one sealed claim.")
            files: dict[str, bytes] = {}
            digests: dict[str, str] = {}
            for i in entries:
                if i.filename in SMALL_FILES and i.file_size <= MAX_SMALL_FILE_BYTES:
                    files[i.filename] = z.read(i)
                    digests[i.filename] = hashlib.sha256(files[i.filename]).hexdigest()
                else:
                    digests[i.filename] = _sha256_entry(z, i)
        except (zipfile.BadZipFile, ValueError, zlib.error, EOFError):
            return invalid("This is not a sealed claim package (zip).")
        if "manifest.json" not in files or "signature.txt" not in files:
            return invalid("manifest.json or signature.txt is missing.")

        manifest_bytes = files["manifest.json"]
        try:
            manifest = json.loads(manifest_bytes)
        except ValueError:
            return invalid("manifest.json is not readable.")
        if not isinstance(manifest, dict) or not isinstance(manifest.get("items", []), list):
            return invalid("manifest.json is not a sealed claim manifest.")

        verify_key = self.verify_key_for(manifest)
        if verify_key is None:
            problems.append(
                f"Signed with an unknown sealing key (key_id {manifest.get('key_id')}); "
                "it is not in this server's key set.")
        package_key = files.get("public_key.txt", b"").strip()
        try:
            if package_key and verify_key is not None and base64.b64decode(package_key, validate=True) != verify_key:
                problems.append("Signed with a different key than this server's sealing key.")
        except ValueError:
            problems.append("public_key.txt is not a valid key.")
        if verify_key is not None:
            try:
                Ed25519PublicKey.from_public_bytes(verify_key).verify(base64.b64decode(files["signature.txt"].strip()), manifest_bytes)
            except (InvalidSignature, ValueError):
                problems.append("The manifest was changed after sealing, or the signature does not match it.")

        # The verifiable event log inside the package.
        if "events.jsonl" not in digests:
            problems.append("events.jsonl is missing from the package.")
        else:
            try:
                events_bytes = z.read("events.jsonl")
            except (zipfile.BadZipFile, ValueError, zlib.error, EOFError, KeyError):
                events_bytes = b""
                problems.append("events.jsonl is not readable.")
            if events_bytes or "events.jsonl" in digests:
                event_list, parse_problem = parse_events_jsonl(events_bytes)
                if parse_problem:
                    problems.append(parse_problem)
                else:
                    head, chain_problem = verify_event_chain(event_list)
                    if chain_problem:
                        problems.append(chain_problem)
                    if head != manifest.get("event_chain_head"):
                        problems.append("events.jsonl head does not match manifest.event_chain_head.")
                    if len(event_list) != manifest.get("event_count"):
                        problems.append("events.jsonl count does not match manifest.event_count.")

        by_name = {i.filename: i for i in entries}
        removed = 0
        removed_at = None
        removed_reason = None
        receipt_map: dict[str, str | None] = {}
        receipt_trusted = False
        if "removal.json" in digests:
            try:
                r_info = by_name.get("removal.json")
                s_info = by_name.get("removal.sig")
                if r_info is None or s_info is None:
                    raise ValueError("missing receipt signature")
                if r_info.file_size > MAX_SMALL_FILE_BYTES or s_info.file_size > MAX_SMALL_FILE_BYTES:
                    raise ValueError("receipt too large")
                removal_bytes = z.read("removal.json")
                removal_sig = z.read("removal.sig")
                receipt_keys = [k for k in (verify_key, self.public_raw) if k is not None]
                receipt_ok = False
                for candidate in dict.fromkeys(receipt_keys):
                    try:
                        Ed25519PublicKey.from_public_bytes(candidate).verify(
                            base64.b64decode(removal_sig.strip()), removal_bytes)
                        receipt_ok = True
                        break
                    except (InvalidSignature, ValueError):
                        continue
                if not receipt_ok:
                    raise ValueError("receipt signature")
                receipt = json.loads(removal_bytes)
                if not isinstance(receipt, dict) or receipt.get("claim_id") != manifest.get("claim_id"):
                    raise ValueError("receipt claim mismatch")
                removed_list = receipt.get("removed")
                if not isinstance(removed_list, list):
                    raise ValueError("receipt shape")
                for e in removed_list:
                    if isinstance(e, dict) and isinstance(e.get("file"), str):
                        receipt_map[e["file"]] = e.get("sha256")
                removed = len(removed_list)
                removed_at = receipt.get("removed_at")
                removed_reason = receipt.get("reason")
                receipt_trusted = True
            except (InvalidSignature, ValueError):
                problems.append("removal.json was changed or is not signed by this server")
                receipt_trusted = False
                receipt_map = {}
                removed = 0
                removed_at = None
                removed_reason = None

        listed = set()
        for item in manifest.get("items", []):
            if not isinstance(item, dict):
                problems.append("manifest.json lists an item that is not a file record.")
                continue
            name = item.get("file", "")
            listed.add(name)
            if name not in digests:
                if receipt_trusted and receipt_map.get(name) == item.get("sha256"):
                    continue
                problems.append(f"{name} is listed in the manifest but missing from the package.")
            elif digests[name] != item.get("sha256"):
                problems.append(f"{name} was changed: its SHA-256 no longer matches the seal.")
        for name in digests:
            if name.startswith("files/") and name not in listed:
                problems.append(f"{name} was added: it is not in the sealed manifest.")
        for name, key in (("answers.json", "answers_sha256"), ("policy_snapshot.json", "policy_snapshot_sha256")):
            if name not in digests:
                problems.append(f"{name} is missing from the package.")
            elif digests[name] != manifest.get(key):
                problems.append(f"{name} was changed: its SHA-256 no longer matches the seal.")

        return {
            "valid": not problems,
            "claim_id": manifest.get("claim_id"),
            "sealed_at": manifest.get("sealed_at"),
            "items": len(manifest.get("items", [])),
            "removed": removed,
            "removed_at": removed_at,
            "removed_reason": removed_reason,
            "problems": problems,
        }


def build_manifest(claim, snapshot, items, answers_json: str, sessions: list[str], chain: dict, sealed_at: str, key_id: str) -> dict:
    return {
        "format": FORMAT,
        "claim_id": claim["id"],
        "org_id": claim["org_id"],
        "template_id": claim["template_id"],
        "loss_type": json.loads(answers_json).get("loss_type"),
        "policy_no": snapshot["policy_no"],
        "policy_verified": bool(snapshot["verified"]),
        "policy_snapshot_sha256": hashlib.sha256((snapshot["data_json"] or "").encode()).hexdigest(),
        "answers_sha256": hashlib.sha256(answers_json.encode()).hexdigest(),
        "items": [
            {
                "id": i["id"],
                "slot": i["step_id"],
                "kind": i["kind"],
                "mime": i["mime"],
                "size": i["size"],
                "sha256": i["sha256"],
                "received_at": i["received_at"],
                "file": f"files/{i['step_id']}-{i['id']}.{EXT.get(i['mime'], 'bin')}",
            }
            for i in items
        ],
        "session_codes": sessions,
        "event_chain_head": chain["head"],
        "event_count": chain["count"],
        "sealed_at": sealed_at,
        "key_id": key_id,
    }


def build_package(manifest_bytes: bytes, signature: str, public_key_b64: str, answers_json: str, snapshot_json: str, files: dict[str, bytes],
                  removal_json: bytes | None = None, removal_sig: str | None = None,
                  events_jsonl: bytes | str | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.json", manifest_bytes)
        z.writestr("signature.txt", signature)
        z.writestr("public_key.txt", public_key_b64)
        z.writestr("answers.json", answers_json)
        z.writestr("policy_snapshot.json", snapshot_json)
        z.writestr("README.txt", README)
        if events_jsonl is not None:
            z.writestr("events.jsonl", events_jsonl.encode() if isinstance(events_jsonl, str) else events_jsonl)
        for name, data in files.items():
            z.writestr(name, data)
        if removal_json is not None and removal_sig is not None:
            z.writestr("removal.json", removal_json)
            z.writestr("removal.sig", removal_sig)
    return buf.getvalue()
