"""The adjuster's decision on a ready proposal (T27, tasks/t27-t26-t32-design.md).

Pure: no database, no clock, no network. A decision locks the claim; later
changes happen in the insurer's own system. Approving approves the proposal
(lane, next step, suggested initial reserve), never a payment.
"""

from .proposal import LANES

VERSION = "decision/v1"
ACTIONS = {
    "approve": "Approved",
    "edit_approve": "Edited and approved",
    "send_back": "Sent back for more photos",
    "refer_siu": "Referred to SIU",
    "field_inspection": "Sent for field inspection",
}
REASON_MIN, REASON_MAX = 5, 500


def validate(action: str, body: dict, proposal: dict, photo_slots: list[str]) -> dict:
    """-> the clean decision fields, or raises ValueError(plain message).

    body: {"action", "reason"?, "lane"?, "suggested_reserve"?, "slots"?, "by"?};
    proposal: the ready proposal; photo_slots: the claim's photo slot ids.
    """
    if action not in ACTIONS:
        raise ValueError("Unknown action")
    reason = (body.get("reason") or "").strip()
    if action == "approve" and not (proposal.get("flags") or []):
        if len(reason) > REASON_MAX:
            raise ValueError("The reason is too long")
        reason = reason or None
    else:
        if len(reason) < REASON_MIN:
            raise ValueError("A reason of at least 5 characters is required")
        if len(reason) > REASON_MAX:
            raise ValueError("The reason is too long")
    by = (body.get("by") or "Adjuster").strip()[:60]
    lane_before = proposal.get("lane")
    reserve_before = proposal.get("suggested_reserve")
    lane_after, reserve_after, slots = lane_before, reserve_before, []
    if action == "edit_approve":
        lane = body.get("lane")
        if lane not in LANES:
            raise ValueError("Pick a lane")
        reserve = body.get("suggested_reserve")
        if reserve is not None and (type(reserve) is not int or reserve < 0):
            raise ValueError("The reserve must be a whole number of dollars")
        if lane == lane_before and (reserve is None or reserve == reserve_before):
            raise ValueError("Nothing was changed: use Approve")
        lane_after = lane
        reserve_after = reserve if reserve is not None else reserve_before
    elif action == "send_back":
        slots = body.get("slots")
        if (not isinstance(slots, list) or not slots or len(set(slots)) != len(slots)
                or any(s not in (photo_slots or []) for s in slots)):
            raise ValueError("Pick the photos to retake")
    return {
        "action": action,
        "label": ACTIONS[action],
        "reason": reason,
        "by": by,
        "lane_before": lane_before,
        "lane_after": lane_after,
        "reserve_before": reserve_before,
        "reserve_after": reserve_after,
        "slots": slots,
    }


def outcome(clean: dict) -> dict:
    """-> {"status": target claim status, "activity": {"subject", "priority", "business_days"}}."""
    action, lane = clean.get("action"), clean.get("lane_after")
    if action == "send_back":
        return {"status": "more_evidence",
                "activity": {"subject": "Follow up on requested photos", "priority": "normal", "business_days": 2}}
    if action == "refer_siu" or (action in ("approve", "edit_approve") and lane == "siu_referral"):
        return {"status": "siu",
                "activity": {"subject": "SIU referral review", "priority": "high", "business_days": 1}}
    if action == "field_inspection":
        return {"status": "handed_to_human",
                "activity": {"subject": "Schedule field inspection", "priority": "high", "business_days": 1}}
    if action in ("approve", "edit_approve") and lane == "fast_track":
        return {"status": "approved",
                "activity": {"subject": "Send to estimating (fast track)", "priority": "normal", "business_days": 1}}
    return {"status": "approved",
            "activity": {"subject": "Send to estimating", "priority": "normal", "business_days": 2}}


def note(clean: dict, proposal: dict) -> dict:
    """-> {"topic": "general", "subject": "Adjuster decision", "body": str}."""
    body = f"Decision by {clean.get('by')}: {clean.get('label')}. Lane: {LANES.get(clean.get('lane_after'))}"
    if clean.get("lane_after") != clean.get("lane_before"):
        body += f" (changed from {LANES.get(clean.get('lane_before'))})"
    body += "."
    if clean.get("reserve_after") is not None:
        body += f" Suggested initial reserve ${clean['reserve_after']:,}"
        if clean.get("reserve_before") is not None and clean["reserve_before"] != clean["reserve_after"]:
            body += f" (changed from ${clean['reserve_before']:,})"
        body += "."
    if clean.get("action") == "send_back" and clean.get("slots"):
        titles = ", ".join((s or "").replace("_", " ").title() for s in clean["slots"])
        body += f" Photos requested: {titles}."
    if clean.get("reason"):
        body += f" Reason: {clean['reason']}"
    return {"topic": "general", "subject": "Adjuster decision", "body": body}
