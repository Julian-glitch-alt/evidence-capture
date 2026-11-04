"""Claim state machine and the append-only event log.

Every status change goes through `transition`, which writes its event in the same
transaction. Each event stores the hash of the previous one, so editing or deleting
any stored event breaks `verify_chain` from that point on.
"""

import hashlib
import json
import uuid

import psycopg

from .errors import CaptureError
from .policies import canonical

GENESIS = "0" * 64

# from_status -> {to_status: actors allowed}. Overview decision 29.
TRANSITIONS: dict[str, dict[str, set[str]]] = {
    "opened": {"capturing": {"customer"}, "closed": {"user"}},
    "capturing": {"submitted": {"customer"}, "closed": {"user"}},
    "submitted": {"verifying": {"system"}},
    "verifying": {"assessed": {"system"}, "handed_to_human": {"system", "user"}},
    "assessed": {"in_review": {"system", "user"}},
    "in_review": {
        "approved": {"user"},
        "more_evidence": {"user"},
        "siu": {"user"},
        "total_loss": {"user"},
        "handed_to_human": {"user"},
    },
    "more_evidence": {"capturing": {"customer"}},
    "handed_to_human": {"in_review": {"user"}, "closed": {"user"}},
    "siu": {"in_review": {"user"}, "closed": {"user"}},
    "approved": {"repairing": {"user", "customer"}, "awaiting_payment": {"user"}},
    "repairing": {"repair_proof": {"customer"}},
    "repair_proof": {"bill_submitted": {"customer"}},
    "bill_submitted": {"awaiting_payment": {"user"}},
    "total_loss": {"awaiting_payment": {"user"}},
    "awaiting_payment": {"paid": {"user", "system"}},
    "paid": {"closed": {"user", "system"}},
    "closed": {},
}


def transition(
    db: psycopg.Connection, claim_id: str, to_status: str, actor: str, at: str, data: dict | None = None
) -> dict:
    claim = db.execute(
        "SELECT c.status, s.verified FROM claims c"
        " JOIN policy_snapshots s ON s.id = c.policy_snapshot_id WHERE c.id=%s",
        (claim_id,),
    ).fetchone()
    if not claim:
        raise CaptureError("Claim not found", 404)
    from_status = claim["status"]
    if actor not in TRANSITIONS.get(from_status, {}).get(to_status, set()):
        raise CaptureError(f"Cannot move from {from_status} to {to_status} as {actor}", 409)
    if to_status == "awaiting_payment" and not claim["verified"]:
        raise CaptureError("Policy is not verified; a payment cannot be requested", 409)
    # Conditional update: a concurrent transition that got there first makes this one fail.
    cur = db.execute(
        "UPDATE claims SET status=%s WHERE id=%s AND status=%s", (to_status, claim_id, from_status)
    )
    if cur.rowcount == 0:
        raise CaptureError("Claim changed while updating; try again", 409)
    return append_event(db, claim_id, at, actor, f"claim.{to_status}", from_status, to_status, data)


def append_event(
    db: psycopg.Connection,
    claim_id: str,
    at: str,
    actor: str,
    type_: str,
    from_status: str | None = None,
    to_status: str | None = None,
    data: dict | None = None,
) -> dict:
    last = db.execute(
        "SELECT seq, hash FROM claim_events WHERE claim_id=%s ORDER BY seq DESC LIMIT 1", (claim_id,)
    ).fetchone()
    event = {
        "id": uuid.uuid4().hex,
        "claim_id": claim_id,
        "seq": last["seq"] + 1 if last else 1,
        "at": at,
        "actor": actor,
        "type": type_,
        "from_status": from_status,
        "to_status": to_status,
        "data": data or {},
        "prev_hash": last["hash"] if last else GENESIS,
    }
    event["hash"] = _event_hash(event)
    db.execute(
        "INSERT INTO claim_events (id, claim_id, seq, at, actor, type, from_status, to_status,"
        " data_json, prev_hash, hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (
            event["id"],
            claim_id,
            event["seq"],
            at,
            actor,
            type_,
            from_status,
            to_status,
            canonical(event["data"]),
            event["prev_hash"],
            event["hash"],
        ),
    )
    return event


def events(db: psycopg.Connection, claim_id: str) -> list[dict]:
    rows = db.execute("SELECT * FROM claim_events WHERE claim_id=%s ORDER BY seq", (claim_id,))
    return [_event_public(r) for r in rows]


def events_jsonl(event_list: list[dict]) -> bytes:
    """All claim events as JSON Lines (seq, prev_hash and hash included),
    one canonical JSON object per line, in seq order."""
    return "".join(canonical(e) + "\n" for e in event_list).encode()


def verify_event_list(event_list: list[dict]) -> dict:
    """Recompute the chain over an in-memory event list (no database)."""
    prev = GENESIS
    for i, event in enumerate(event_list, start=1):
        if event.get("seq") != i or event.get("prev_hash") != prev or _event_hash(event) != event.get("hash"):
            return {"ok": False, "broken_at": i, "head": prev, "count": len(event_list)}
        prev = event["hash"]
    return {"ok": True, "broken_at": None, "head": prev, "count": len(event_list)}


def verify_chain(db: psycopg.Connection, claim_id: str) -> dict:
    """Recompute every hash, and check the claim's status is the one the log ends in.

    Events cut off the end are not detectable here; the seal (signed chain head) is.
    """
    prev, status = GENESIS, None
    for i, event in enumerate(events(db, claim_id), start=1):
        if event["seq"] != i or event["prev_hash"] != prev or _event_hash(event) != event["hash"]:
            return {"ok": False, "broken_at": i}
        prev = event["hash"]
        status = event["to_status"] or status
    current = db.execute("SELECT status FROM claims WHERE id=%s", (claim_id,)).fetchone()
    if current and current["status"] != status:
        return {"ok": False, "broken_at": None, "reason": "claim status does not match the log"}
    return {"ok": True, "broken_at": None, "head": prev}


def _event_hash(event: dict) -> str:
    body = {k: v for k, v in event.items() if k not in ("id", "hash")}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def _event_public(row: dict) -> dict:
    return {
        "id": row["id"],
        "claim_id": row["claim_id"],
        "seq": row["seq"],
        "at": row["at"],
        "actor": row["actor"],
        "type": row["type"],
        "from_status": row["from_status"],
        "to_status": row["to_status"],
        "data": json.loads(row["data_json"]),
        "prev_hash": row["prev_hash"],
        "hash": row["hash"],
    }
