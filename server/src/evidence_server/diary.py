"""Claim diaries (T52, tasks/t52-design.md).

Pure validation; storage lives in Store. A diary is a follow-up with a due
date and a reason, set by any signed-in user and marked done when handled.
"""

from datetime import date, timedelta

REASON_MIN, REASON_MAX = 3, 200
DUE_MAX_DAYS = 365  # how far ahead a diary may be set


def validate(body: dict, today: date) -> dict:
    """-> {"due_on": "YYYY-MM-DD", "reason": trimmed}, or raises ValueError(plain message)."""
    if not isinstance(body, dict):
        raise ValueError("The diary is not valid")
    raw_due = body.get("due_on")
    try:
        due = date.fromisoformat(raw_due) if isinstance(raw_due, str) else None
    except (TypeError, ValueError):
        due = None
    if due is None:
        raise ValueError("due_on must be a real date (YYYY-MM-DD)")
    if due < today:
        raise ValueError("due_on cannot be in the past")
    if due > today + timedelta(days=DUE_MAX_DAYS):
        raise ValueError("due_on is too far ahead (at most 365 days)")
    reason = body.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if len(reason) < REASON_MIN:
        raise ValueError("A reason of at least 3 characters is required")
    if len(reason) > REASON_MAX:
        raise ValueError("The reason is too long")
    return {"due_on": due.isoformat(), "reason": reason}
