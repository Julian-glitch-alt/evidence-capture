"""Adjuster reviews before sending (T40, tasks/t40-design.md).

Pure: no database, no clock, no network. Reviews never rewrite evidence:
a dismissed flag or an excluded damage stays listed; every accepted row is
an event in the hash chain. Damage values read from photos are never edited
by hand: a wrong read means Send back.
"""

REVIEW_MIN, REVIEW_MAX = 5, 500
NOTE_MIN, NOTE_MAX = 5, 1000


def state(rows: list[dict]) -> dict:
    """rows = reviews rows in `at` order -> review state (last row per target wins)."""
    ordered = sorted(rows or [], key=lambda r: (r.get("at") or r.get("decided_at") or ""))
    flags: dict = {}
    damages: dict = {}
    notes: list = []
    for r in ordered:
        kind = r.get("kind")
        by = r.get("by") or r.get("decided_by")
        at = r.get("at") or r.get("decided_at")
        if kind == "note":
            notes.append({"text": r.get("text"), "by": by, "at": at})
        elif kind == "flag":
            flags[r.get("target") or r.get("key")] = {
                "verdict": r.get("verdict"), "reason": r.get("reason"),
                "by": by, "at": at,
            }
        elif kind == "damage":
            damages[r.get("target") or r.get("key")] = {
                "verdict": r.get("verdict"), "reason": r.get("reason"),
                "by": by, "at": at,
            }
    return {"flags": flags, "damages": damages, "notes": notes}


def validate(body: dict, flag_keys: set[str], damage_keys: set[str]) -> dict:
    """-> the clean row fields, or raises ValueError(plain message)."""
    if not isinstance(body, dict):
        raise ValueError("The review is not valid")
    kind = body.get("kind")
    by = (body.get("by") or "Adjuster")
    by = (by.strip() if isinstance(by, str) else "")[:60] or "Adjuster"
    if kind == "flag":
        key = body.get("key")
        if key not in (flag_keys or set()):
            raise ValueError("Unknown finding")
        verdict = body.get("verdict")
        if verdict not in ("confirmed", "false_alarm"):
            raise ValueError("Pick confirmed or false alarm")
        reason = (body.get("reason") or "").strip() if isinstance(body.get("reason"), str) else ""
        if verdict == "false_alarm":
            if not REVIEW_MIN <= len(reason) <= REVIEW_MAX:
                raise ValueError("A reason of at least 5 characters is required")
            reason = reason or None
        else:
            if reason and len(reason) > REVIEW_MAX:
                raise ValueError("The reason is too long")
            reason = reason or None
        return {"kind": "flag", "target": key, "key": key, "verdict": verdict,
                "reason": reason, "text": None, "by": by}
    if kind == "damage":
        key = body.get("key")
        if key not in (damage_keys or set()):
            raise ValueError("Unknown damage")
        verdict = body.get("verdict")
        if verdict not in ("excluded", "included"):
            raise ValueError("Pick excluded or included")
        reason = (body.get("reason") or "").strip() if isinstance(body.get("reason"), str) else ""
        if verdict == "excluded":
            if not REVIEW_MIN <= len(reason) <= REVIEW_MAX:
                raise ValueError("A reason of at least 5 characters is required")
            reason = reason or None
        else:
            if reason and len(reason) > REVIEW_MAX:
                raise ValueError("The reason is too long")
            reason = reason or None
        return {"kind": "damage", "target": key, "key": key, "verdict": verdict,
                "reason": reason, "text": None, "by": by}
    if kind == "note":
        text = body.get("text")
        text = text.strip() if isinstance(text, str) else ""
        if not NOTE_MIN <= len(text) <= NOTE_MAX:
            raise ValueError("A note of at least 5 characters is required")
        return {"kind": "note", "target": None, "key": None, "verdict": None,
                "reason": None, "text": text, "by": by}
    raise ValueError("Unknown review")


def excluded(review_state: dict) -> set[str]:
    """Damage keys whose last verdict is excluded."""
    damages = (review_state or {}).get("damages") or {}
    return {key for key, entry in damages.items() if (entry or {}).get("verdict") == "excluded"}
