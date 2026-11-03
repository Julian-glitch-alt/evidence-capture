"""Flags, lane and proposal from stored results (T23-T25, tasks/t22-t25-design.md).

Pure: no database, no clock, no network, no model. A proposal only suggests
who looks next and a starting reserve: it never decides money, never rejects,
and fast track still needs a person's approval.

Two lane rules (track 5, docs/explorations/evidence-math.md): "count" (lane_rule_v1, two
independent conflicts suggest SIU screening) and "cost" (lane_cost, SIU screening when the
weight of evidence makes it worth its cost on a claim of this size). Both are returned for
one release; LANE_RULES names which one drives the lane.
"""

from . import evidence as evidence_lib

VERSION = "proposal/v3"
FAST_TRACK_MAX = 2500  # likely high end, dollars; a person still approves
SIU_CONFLICTS = 2  # independent root causes with a conflict, model-only verdicts excluded
LANE_RULES = ("cost", "count")
DEFAULT_LANE_RULE = "count"  # production default is Julian's call (LANE_RULE env var); tests run "cost"

# Root cause of each reason code. Rows that share a cause are one fact seen from
# several places (the VIN typed in the form and the VIN in the photo, against the
# same policy), so the SIU rule counts causes, never rows. An unknown code is its own cause.
_PHOTO_QUALITY = ("PHOTO_WRONG_SUBJECT", "PHOTO_UNREADABLE", "PHOTO_READS_DISAGREE", "PHOTO_POOR_QUALITY",
                  "VIN_READ_INVALID", "DAMAGE_NOT_VISIBLE", "DAMAGE_PART_NOT_FOUND")
ROOT_CAUSES = {
    "POLICY_NOT_VERIFIED": "policy_on_file",
    "POLICY_NOT_IN_FORCE": "policy_period",
    "LOSS_DATE_NOT_PROVIDED": "policy_period",
    "COVERAGE_NOT_FOUND": "coverage",
    "COVERAGE_UNCLEAR": "coverage",
    "VIN_MISMATCH": "vehicle_identity",
    "PLATE_MISMATCH": "vehicle_identity",
    "VIN_NOT_PROVIDED": "vehicle_identity",
    "DRIVER_NOT_LISTED": "driver",
    "DRIVER_NOT_PROVIDED": "driver",
    "LICENSE_NAME_MISMATCH": "driver",
    "LICENSE_EXPIRED": "driver",
    "ODOMETER_IMPLAUSIBLE": "odometer",
    **{code: "photo_quality" for code in _PHOTO_QUALITY},
    "PHOTO_UNCHECKED": "analysis_missing",
    "DAMAGE_NOT_READ": "analysis_missing",
    "STORY_NOT_CHECKED": "analysis_missing",
    "DAMAGE_AREA_MISMATCH": "damage_location",
    "DAMAGE_AREA_UNCLEAR": "damage_location",
    "DAMAGE_OUTSIDE_STATEMENT": "damage_location",
    "PRIOR_DAMAGE_SUSPECTED": "prior_damage",
    "STORY_INCONSISTENT": "story",
    "STORY_UNCLEAR": "story",
    "REQUIRED_EVIDENCE_MISSING": "evidence_missing",
    "MULTI_PARTY": "multi_party",
    "INJURY_REPORTED": "injury",
    "LOSS_TYPE_OUT_OF_PHOTO_LANE": "loss_type",
    "POSSIBLE_TOTAL_LOSS": "total_loss",
    "POSSIBLE_DUPLICATE": "duplicate",
    "SCREEN_RECAPTURE_SUSPECTED": "screen_recapture",
    "RETAKE_AFTER_MISMATCH": "coaching",
    "ANSWER_CHANGED_AFTER_HINT": "coaching",
    "FNOL_MISMATCH": "fnol_statement",
    "POLICE_REPORT_NEEDED": "police_report",
    "KEYS_MISSING": "keys",
    "TYPE_DAMAGE_MISMATCH": "claim_type",
    "TYPE_DAMAGE_NOT_SEEN": "claim_type",
}
CAUSE_LABELS = {
    "policy_on_file": "no policy on file",
    "policy_period": "policy period",
    "coverage": "coverage",
    "vehicle_identity": "vehicle identity",
    "driver": "driver",
    "odometer": "odometer",
    "photo_quality": "photo quality",
    "analysis_missing": "automatic check missing",
    "damage_location": "where the damage is",
    "prior_damage": "earlier damage",
    "story": "statement vs damage",
    "evidence_missing": "skipped evidence",
    "multi_party": "another vehicle",
    "injury": "injury",
    "loss_type": "loss type",
    "total_loss": "possible total loss",
    "duplicate": "possible duplicate",
    "screen_recapture": "photo of a screen or print",
    "coaching": "retakes and edits after a hint",
    "fnol_statement": "first notice vs statement",
    "police_report": "police report",
    "keys": "keys",
    "claim_type": "claim type",
}
# Causes that rest on a model's judgment alone (not a model read compared in code):
# they ask a person to look but never count toward the SIU rule.
MODEL_ONLY = {"story", "prior_damage", "screen_recapture"}

SUBMIT_REASONS = {
    "MULTI_PARTY": "Another vehicle was involved.",
    "INJURY_REPORTED": "Someone was hurt.",
    "LOSS_TYPE_OUT_OF_PHOTO_LANE": "This kind of loss is not handled from photos.",
    "POLICY_NOT_VERIFIED": "No policy on file to check against.",
    "VIN_MISMATCH": "The VIN in the claim is not on the policy.",
    "REQUIRED_EVIDENCE_MISSING": "A required photo or video was skipped.",
    "POLICE_REPORT_NEEDED": "A police report is usually required for a hit-and-run claim; none was given.",
    "KEYS_MISSING": "Not every key to the stolen car is with the policyholder.",
}
NOTE_ONLY = {("photo.driver_license", "PHOTO_UNCHECKED")}  # the licence is not sent to the model on free credits
LANES = {"siu_referral": "SIU screening suggested", "adjuster_review": "Adjuster review", "fast_track": "Fast track", "standard": "Standard"}
NEXT_STEPS = {
    "siu_referral": "Screen for an SIU referral before any payment; referring is the adjuster's decision.",
    "adjuster_review": "Review the flags, then approve, edit or send back.",
    "fast_track": "Send to estimating for the final estimate; payment needs a person's approval.",
    "standard": "Send to estimating; an adjuster reviews the estimate.",
}


def flags(checks: list[dict], submit_codes: list[str], reading: dict) -> dict:
    """checks = coverage + photo + consistency rows in that order -> {"flags": [...], "notes": [...]}."""
    found, notes = [], []
    for row in checks:
        level = {"fail": "conflict", "unclear": "review"}.get(row.get("result"))
        if level is None:  # pass and value ask nothing of a person
            continue
        if row.get("reason_code") in ("TYPE_DAMAGE_MISMATCH", "TYPE_DAMAGE_NOT_SEEN"):
            level = "review"
        entry = {
            "source": row.get("check_id"),
            "level": level,
            "reason_code": row.get("reason_code"),
            "reason": row.get("reason"),
            "evidence": _check_evidence(row),
            **_cause(row.get("reason_code")),
        }
        if (row.get("check_id"), row.get("reason_code")) in NOTE_ONLY:
            notes.append({**entry, "level": "note"})
        else:
            found.append(entry)
    seen = {f["reason_code"] for f in found}  # a checked finding is not counted again
    for code in submit_codes:
        if code in seen:
            continue
        found.append({
            "source": "submit",
            "level": "review",
            "reason_code": code,
            "reason": SUBMIT_REASONS.get(code, f"Needs a person: {code}."),
            "evidence": [],
            **_cause(code),
        })
    loss = _total_loss_evidence(reading)
    if loss:
        found.append({
            "source": "estimate",
            "level": "review",
            "reason_code": "POSSIBLE_TOTAL_LOSS",
            "reason": "Severe water or fire damage: the vehicle may be a total loss.",
            "evidence": loss,
            **_cause("POSSIBLE_TOTAL_LOSS"),
        })
    return {"flags": found, "notes": notes}


def causes(found: list[dict]) -> list[dict]:
    """Flags grouped by root cause, in first-seen order:
    [{"cause", "label", "level": "conflict"|"review", "model_only", "rows", "codes"}]."""
    out: dict = {}
    for f in found:
        cause = f.get("cause") or _cause(f.get("reason_code"))["cause"]
        entry = out.setdefault(cause, {"cause": cause, "label": CAUSE_LABELS.get(cause, cause.lower().replace("_", " ")),
                                       "level": "review", "model_only": cause in MODEL_ONLY, "rows": 0, "codes": []})
        entry["rows"] += 1
        if f.get("reason_code") and f["reason_code"] not in entry["codes"]:
            entry["codes"].append(f["reason_code"])
        if f.get("level") == "conflict":
            entry["level"] = "conflict"
    return list(out.values())


def lane_rule_v1(flagged: dict, reading: dict, estimate: dict) -> dict:
    """The counting rule ("count"). First matching rule wins; a lane suggests who looks next,
    never money or rejection. SIU screening needs SIU_CONFLICTS independent conflicts: root
    causes, not rows, and never a model-only verdict."""
    found = flagged.get("flags") or []
    siu = [c for c in causes(found) if c["level"] == "conflict" and not c["model_only"]]
    if len(siu) >= SIU_CONFLICTS:
        names = ", ".join(c["label"] for c in siu)
        return _lane("siu_referral", "R1_SIU", f"{len(siu)} independent conflicts: {names}.")
    return _after_siu(found, estimate)


def lane_cost(flagged: dict, reading: dict, estimate: dict, ev: dict) -> dict:
    """The expected-cost rule ("cost"). SIU screening when the claim value is above the
    break-even value of evidence.siu_test; the claim value is the rough range midpoint.
    Unpriced claims fall back to the counting rule, and the result says so. Below SIU the
    rules are the counting rule's (review, fast track, standard)."""
    found = flagged.get("flags") or []
    value = estimate.get("midpoint") if estimate.get("priced") and estimate.get("midpoint") else None
    if value is None:
        out = lane_rule_v1(flagged, reading, estimate)
        test = {**evidence_lib.siu_test(ev, None), "value_basis": "not priced", "fallback": True}
        if out["lane"] == "siu_referral":
            out = {**out, "reason": out["reason"] + " Not priced, so the counting rule decided."}
        return {**out, "cost": test}
    test = {**evidence_lib.siu_test(ev, value), "value_basis": "rough range midpoint", "fallback": False}
    names = ", ".join(c["label"] for c in ev["contributions"])
    if test["suggest"]:
        out = _lane("siu_referral", "C1_SIU_COST",
                    f"{ev['band'].capitalize()} evidence ({names}) on a claim near ${value:,}: "
                    f"screening pays off above about ${test['break_even_value']:,} (assumed weights).")
        return {**out, "cost": test}
    out = _after_siu(found, estimate)
    if test["eligible"]:
        out = {**out, "reason": out["reason"] + f" SIU screening would pay off only above about "
                                                f"${test['break_even_value']:,}; likely cost near ${value:,}."}
    return {**out, "cost": test}


def lane(flagged: dict, reading: dict, estimate: dict, rule: str = DEFAULT_LANE_RULE, ev: dict | None = None) -> dict:
    """The lane under `rule` ("cost" or "count")."""
    if rule == "cost":
        return lane_cost(flagged, reading, estimate, ev or evidence(flagged))
    return lane_rule_v1(flagged, reading, estimate)


def evidence(flagged: dict) -> dict:
    """The evidence block (evidence.score) of the flags and notes."""
    return evidence_lib.score(causes(flagged.get("flags") or []), causes(flagged.get("notes") or []))


def _after_siu(found: list[dict], estimate: dict) -> dict:
    """Review, fast track or standard: the rules below SIU, shared by both lane rules."""
    if found or estimate.get("status") == "none":
        if found:
            n = len(found)
            return _lane("adjuster_review", "R2_REVIEW", f"{n} finding{'s' if n != 1 else ''} to review.")
        return _lane("adjuster_review", "R2_REVIEW", "The damage photos could not be priced.")
    likely = estimate.get("likely") or []
    if estimate.get("status") == "complete" and estimate.get("priced") and likely[1] <= FAST_TRACK_MAX:
        return _lane("fast_track", "R3_FAST", f"All checks passed; likely cost up to ${likely[1]:,}.")
    return _lane("standard", "R4_STANDARD", "All checks passed; the cost needs a full estimate.")


def build(checks, submit_codes, reading, estimate, deductible, lane_rule: str = DEFAULT_LANE_RULE) -> dict:
    """The adjuster's proposal: lane, why, the range and a suggested reserve (a suggestion, not a decision).
    `lane_rule` picks which rule drives the lane; both rules' results are returned."""
    if lane_rule not in LANE_RULES:
        raise ValueError(f"lane_rule must be one of {', '.join(LANE_RULES)}")
    flagged = flags(checks, submit_codes, reading)
    ev = evidence(flagged)
    by_rule = {"count": lane_rule_v1(flagged, reading, estimate), "cost": lane_cost(flagged, reading, estimate, ev)}
    decided = by_rule[lane_rule]
    ordered = [f for f in flagged["flags"] if f["level"] == "conflict"]
    ordered += [f for f in flagged["flags"] if f["level"] == "review"]
    midpoint = estimate.get("midpoint")
    return {
        "version": VERSION,
        "lane": decided["lane"], "label": decided["label"], "rule": decided["rule"],
        "headline": decided["reason"],
        "lane_rule": lane_rule,
        "lane_v1": {k: by_rule["count"][k] for k in ("lane", "label", "rule", "reason")},
        "lane_cost": by_rule["cost"],
        "evidence": ev,
        "reasons": [f["reason"] for f in ordered[:3]] or ["All checks passed."],
        "flags": flagged["flags"], "notes": flagged["notes"],
        "causes": causes(flagged["flags"]),
        "estimate": estimate,
        "deductible": deductible,
        "suggested_reserve": max(0, midpoint - (deductible or 0)) if midpoint is not None else None,
        "next_step": NEXT_STEPS[decided["lane"]],
    }


def _cause(reason_code) -> dict:
    cause = ROOT_CAUSES.get(reason_code) or str(reason_code)
    return {"cause": cause, "model_only": cause in MODEL_ONLY}


def _lane(lane: str, rule: str, reason: str) -> dict:
    return {"lane": lane, "label": LANES[lane], "rule": rule, "reason": reason}


def _check_evidence(row: dict) -> list[dict]:
    """Where the check's finding comes from: T21 evidence, the photo's slot, or nothing."""
    check_id = row.get("check_id") or ""
    value = row.get("value")
    if check_id == "consistency.areas":
        return list((value or {}).get("evidence") or [])
    if check_id == "consistency.prior_damage":
        return list((value or {}).get("old") or [])
    if check_id == "consistency.story":
        out = []
        for reason in (value or {}).get("reasons") or []:
            for e in (reason or {}).get("evidence") or []:
                if e not in out:
                    out.append(e)
        return out
    if check_id.startswith("photo."):
        inputs = row.get("inputs") or {}
        if "item_id" in inputs:
            return [{"item_id": inputs["item_id"], "slot_id": check_id[len("photo."):]}]
        return []
    if isinstance(value, dict) and isinstance(value.get("evidence"), list):  # signals.py rows
        return list(value["evidence"])
    return []


def _total_loss_evidence(reading: dict) -> list[dict]:
    """Numbered evidence for severe water or fire damage of any age, in reading order."""
    counts: dict = {}
    out = []
    for d in reading.get("damages") or []:
        counts[d.get("item_id")] = counts.get(d.get("item_id"), 0) + 1
        if d.get("damage_type") in ("WATER_DAMAGE", "FIRE_DAMAGE") and d.get("severity") == "severe":
            out.append({
                "item_id": d.get("item_id"), "slot_id": d.get("slot_id"),
                "number": counts[d.get("item_id")],
            })
    return out
