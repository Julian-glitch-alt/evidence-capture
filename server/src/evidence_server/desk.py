"""The adjuster's queue and the cycle-time clock (T26 + T32, tasks/t27-t26-t32-design.md).

Pure: no database, no clock, no network. Same events in, same numbers out.
"""

import math
from datetime import datetime

QUEUES = [("siu_referral", "SIU"), ("adjuster_review", "Adjuster review"), ("fast_track", "Fast track"),
          ("standard", "Standard"), ("analysing", "Analysing"), ("capturing", "Waiting for the claimant"),
          ("decided", "Decided")]

KEYS = ("link_to_submit", "submit_to_report", "report_to_decision", "link_to_decision")

FIRST_TYPES = ("claim.opened", "claim.submitted", "report.ready", "decision.recorded")
_FIRST_TYPES = FIRST_TYPES


WAITING_FOR_CLAIMANT = ("opened", "capturing", "more_evidence")


def queue_key(submitted_at: str | None, proposal: dict | None, decision: dict | None,
              status: str | None = None) -> str:
    """Capturing while the claimant has the link (also after a Send back);
    decided when a decision exists; capturing when not submitted;
    analysing when the proposal is None or pending; else the proposal's lane."""
    if status in WAITING_FOR_CLAIMANT:
        return "capturing"
    if decision is not None:
        return "decided"
    if not submitted_at:
        return "capturing"
    if not proposal or proposal.get("status") != "ready":
        return "analysing"
    return proposal.get("lane")


def cycle(events: list[dict]) -> dict:
    """-> {"link_to_submit", "submit_to_report", "report_to_decision", "link_to_decision"} in whole seconds.

    Uses the first event of each type: claim.opened, claim.submitted,
    report.ready, decision.recorded. Missing stages are None.
    """
    first: dict[str, str] = {}
    for event in events or []:
        type_ = event.get("type")
        if type_ in _FIRST_TYPES and type_ not in first and event.get("at") is not None:
            first[type_] = event["at"]

    def gap(before: str, after: str) -> int | None:
        if first.get(before) is None or first.get(after) is None:
            return None
        return int((datetime.fromisoformat(first[after]) - datetime.fromisoformat(first[before])).total_seconds())

    link_to_submit = gap("claim.opened", "claim.submitted")
    submit_to_report = gap("claim.submitted", "report.ready")
    report_to_decision = gap("report.ready", "decision.recorded")
    if link_to_submit is not None and submit_to_report is not None and report_to_decision is not None:
        link_to_decision = link_to_submit + submit_to_report + report_to_decision
    else:
        link_to_decision = gap("claim.opened", "decision.recorded")
    return {
        "link_to_submit": link_to_submit,
        "submit_to_report": submit_to_report,
        "report_to_decision": report_to_decision,
        "link_to_decision": link_to_decision,
    }


def averages(cycles: list[dict]) -> dict:
    """-> per key {"mean_s": int|None, "n": int} over the cycles where that key is not None (mean rounded half up)."""
    out = {}
    for key in KEYS:
        values = [c[key] for c in (cycles or []) if c.get(key) is not None]
        if not values:
            out[key] = {"mean_s": None, "n": 0}
        else:
            out[key] = {"mean_s": int(math.floor(sum(values) / len(values) + 0.5)), "n": len(values)}
    return out
