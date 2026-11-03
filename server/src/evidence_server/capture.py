"""Capture sessions and single-use slot nonces.

A file is accepted only inside an open capture session, carrying a nonce the server
issued for that slot moments before. This raises the cost of posting files straight
to the API; it does not make it impossible (a native app is the full fix).
"""

import json
import secrets
from datetime import datetime, timedelta

import psycopg

from .errors import CaptureError

SESSION_TTL = timedelta(minutes=30)
NONCE_TTL = timedelta(minutes=3)
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I, L, O, 0, 1: easy to read aloud
CODE_LENGTH = 5


def start_session(db: psycopg.Connection, claim_id: str, now: datetime, slots: list[str] | None = None) -> dict:
    """A new session replaces any earlier one: one claim, one capturing device.
    slots: after a Send back, the only slots this session may capture (None = all)."""
    db.execute("UPDATE capture_sessions SET superseded=1 WHERE claim_id=%s", (claim_id,))
    session = {
        "session_id": secrets.token_urlsafe(16),
        "code": "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH)),
        "expires_at": (now + SESSION_TTL).isoformat(),
    }
    db.execute(
        "INSERT INTO capture_sessions (id, claim_id, code, started_at, expires_at, slots_json)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (session["session_id"], claim_id, session["code"], now.isoformat(), session["expires_at"],
         json.dumps(slots) if slots is not None else None),
    )
    if slots is not None:
        session["slots"] = list(slots)
    return session


def require_slot(db: psycopg.Connection, claim_id: str, session_id: str, slot_id: str) -> None:
    """A session opened after a Send back captures only the requested slots."""
    row = db.execute(
        "SELECT slots_json FROM capture_sessions WHERE id=%s AND claim_id=%s", (session_id, claim_id)
    ).fetchone()
    if row and row["slots_json"] is not None and slot_id not in json.loads(row["slots_json"]):
        raise CaptureError("Only the photos the adjuster asked for can be retaken.", 403)


def issue_nonce(db: psycopg.Connection, claim_id: str, session_id: str, slot_id: str, now: datetime) -> dict:
    require_session(db, claim_id, session_id, now)
    nonce = {"nonce": secrets.token_urlsafe(16), "expires_at": (now + NONCE_TTL).isoformat()}
    db.execute(
        "INSERT INTO slot_nonces (nonce, claim_id, session_id, slot_id, issued_at, expires_at)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (nonce["nonce"], claim_id, session_id, slot_id, now.isoformat(), nonce["expires_at"]),
    )
    return nonce


def consume_nonce(
    db: psycopg.Connection,
    claim_id: str,
    session_id: str,
    slot_id: str,
    nonce: str,
    sha256: str,
    now: datetime,
) -> str | None:
    """Mark the nonce used. Returns the earlier item id when this is a retry of an
    upload that already succeeded (same nonce, same file), so the phone gets 200."""
    require_session(db, claim_id, session_id, now)
    row = db.execute(
        "SELECT * FROM slot_nonces WHERE nonce=%s AND claim_id=%s AND session_id=%s",
        (nonce, claim_id, session_id),
    ).fetchone()
    if not row or row["slot_id"] != slot_id:
        raise CaptureError("This shot was not taken in the capture screen. Please retake it.", 403)
    if row["used_at"]:
        same_file = row["item_id"] and db.execute(
            "SELECT 1 FROM items WHERE id=%s AND sha256=%s", (row["item_id"], sha256)
        ).fetchone()
        if same_file:
            return row["item_id"]
        raise CaptureError("This shot was already uploaded. Please retake it.", 403)
    if datetime.fromisoformat(row["expires_at"]) < now:
        raise CaptureError("This shot was not taken in the capture screen. Please retake it.", 403)
    db.execute("UPDATE slot_nonces SET used_at=%s WHERE nonce=%s", (now.isoformat(), nonce))
    return None


def link_item(db: psycopg.Connection, nonce: str, item_id: str) -> None:
    db.execute("UPDATE slot_nonces SET item_id=%s WHERE nonce=%s", (item_id, nonce))


def require_session(db: psycopg.Connection, claim_id: str, session_id: str, now: datetime) -> None:
    row = db.execute(
        "SELECT expires_at, superseded FROM capture_sessions WHERE id=%s AND claim_id=%s",
        (session_id, claim_id),
    ).fetchone()
    if not row or row["superseded"]:
        raise CaptureError("This capture session is not active. Please reopen the link.", 403)
    if datetime.fromisoformat(row["expires_at"]) < now:
        raise CaptureError("This capture session has expired. Please reopen the link.", 403)
    db.execute(
        "UPDATE capture_sessions SET expires_at=%s WHERE id=%s", ((now + SESSION_TTL).isoformat(), session_id)
    )


def sessions(db: psycopg.Connection, claim_id: str) -> list[dict]:
    rows = db.execute(
        "SELECT code, started_at, expires_at, superseded FROM capture_sessions WHERE claim_id=%s ORDER BY started_at",
        (claim_id,),
    )
    return [dict(r) | {"superseded": bool(r["superseded"])} for r in rows]
