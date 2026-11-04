#!/usr/bin/env python3
"""Verify a sealed evidence package offline.

Usage:
    python verify_package.py package.zip --public-key <b64>

The public key is the base64 raw Ed25519 public key printed by
GET /api/verify/public-key (or in the package's public_key.txt). Only the
Python standard library and `cryptography` are used, and no network is
needed. Exit code 0 means the package is valid; 1 means it is not (the
problems are listed); 2 means the arguments or key could not be read.
"""

import argparse
import base64
import hashlib
import json
import sys
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

GENESIS = "0" * 64


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def event_hash(event: dict) -> str:
    body = {k: v for k, v in event.items() if k not in ("id", "hash")}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify a sealed evidence package offline.")
    parser.add_argument("package", help="Path to the sealed package .zip")
    parser.add_argument("--public-key", required=True,
                        help="Base64 raw Ed25519 public key of the sealing server")
    args = parser.parse_args(argv)

    try:
        public_raw = base64.b64decode(args.public_key.strip(), validate=True)
        if len(public_raw) != 32:
            raise ValueError("public key must be 32 bytes")
        key = Ed25519PublicKey.from_public_bytes(public_raw)
    except (ValueError, Exception) as exc:  # noqa: BLE001 - report any bad key
        print(f"error: --public-key is not a valid base64 Ed25519 key: {exc}")
        return 2

    problems: list[str] = []
    try:
        z = zipfile.ZipFile(args.package)
    except (zipfile.BadZipFile, OSError) as exc:
        print(f"invalid: not a sealed claim package (zip): {exc}")
        return 1

    names = {i.filename for i in z.infolist() if not i.is_dir()}
    digests = {}
    for info in z.infolist():
        if info.is_dir():
            continue
        h = hashlib.sha256()
        with z.open(info) as fh:
            while chunk := fh.read(1024 * 1024):
                h.update(chunk)
        digests[info.filename] = h.hexdigest()

    def read(name):
        try:
            return z.read(name)
        except KeyError:
            return None

    manifest_bytes = read("manifest.json")
    signature_txt = read("signature.txt")
    if manifest_bytes is None or signature_txt is None:
        print("invalid: manifest.json or signature.txt is missing.")
        return 1
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError:
        print("invalid: manifest.json is not readable.")
        return 1
    if not isinstance(manifest, dict) or not isinstance(manifest.get("items", []), list):
        print("invalid: manifest.json is not a sealed claim manifest.")
        return 1

    # The key named by the manifest must be the key we were given.
    expected_key_id = hashlib.sha256(public_raw).hexdigest()[:16]
    if manifest.get("key_id") and manifest["key_id"] != expected_key_id:
        problems.append(
            f"manifest key_id {manifest['key_id']} does not match --public-key ({expected_key_id})")
    package_key = read("public_key.txt")
    if package_key is not None:
        try:
            if base64.b64decode(package_key.strip(), validate=True) != public_raw:
                problems.append("public_key.txt does not match --public-key")
        except ValueError:
            problems.append("public_key.txt is not a valid key.")
    try:
        key.verify(base64.b64decode(signature_txt.strip()), manifest_bytes)
    except (InvalidSignature, ValueError):
        problems.append("The manifest was changed after sealing, or the signature does not match it.")

    if "events.jsonl" not in names:
        problems.append("events.jsonl is missing from the package.")
    else:
        events = []
        parse_ok = True
        for line in (read("events.jsonl") or b"").decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            try:
                events.append(json.loads(line))
            except ValueError:
                problems.append("events.jsonl is not readable.")
                parse_ok = False
                break
        if parse_ok:
            prev = GENESIS
            for i, event in enumerate(events, start=1):
                if not isinstance(event, dict) or event.get("seq") != i:
                    problems.append(f"events.jsonl chain is broken at entry {i}: seq is out of order.")
                    break
                if event.get("prev_hash") != prev:
                    problems.append(f"events.jsonl chain is broken at seq {i}: prev_hash does not match.")
                    break
                if event_hash(event) != event.get("hash"):
                    problems.append(f"events.jsonl chain is broken at seq {i}: hash does not match.")
                    break
                prev = event["hash"]
            else:
                if prev != manifest.get("event_chain_head"):
                    problems.append("events.jsonl head does not match manifest.event_chain_head.")
                if len(events) != manifest.get("event_count"):
                    problems.append("events.jsonl count does not match manifest.event_count.")

    listed = set()
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            problems.append("manifest.json lists an item that is not a file record.")
            continue
        name = item.get("file", "")
        listed.add(name)
        if name not in digests:
            problems.append(f"{name} is listed in the manifest but missing from the package.")
        elif digests[name] != item.get("sha256"):
            problems.append(f"{name} was changed: its SHA-256 no longer matches the seal.")
    for name in digests:
        if name.startswith("files/") and name not in listed:
            problems.append(f"{name} was added: it is not in the sealed manifest.")
    for name, field in (("answers.json", "answers_sha256"),
                        ("policy_snapshot.json", "policy_snapshot_sha256")):
        if name not in digests:
            problems.append(f"{name} is missing from the package.")
        elif digests[name] != manifest.get(field):
            problems.append(f"{name} was changed: its SHA-256 no longer matches the seal.")

    if problems:
        print("invalid:")
        for problem in problems:
            print(f" - {problem}")
        return 1
    print(f"valid: claim {manifest.get('claim_id')}, sealed {manifest.get('sealed_at')}, "
          f"{len(manifest.get('items', []))} items, {manifest.get('event_count')} events")
    return 0


if __name__ == "__main__":
    sys.exit(main())
