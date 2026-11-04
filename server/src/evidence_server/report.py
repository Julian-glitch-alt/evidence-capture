"""Verified evidence report data, note, activity and package (T29a, tasks/t29a-design.md).

Pure: no database, no network, no model. Everything here is a pure function
of stored rows and the clock: the same claim always builds the same report.
The wording never decides: the adjuster does, and the footer says so.
"""

import hashlib
import hmac
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from . import damage as damage_lib
from . import loss_notice

VERSION = "report/v2"
PACKAGE_SCHEMA = "evidence-package/v1"
LINK_DAYS = 30
REVIEW_BUSINESS_DAYS = 2
PDF_NAME = "verified-evidence-report.pdf"

CHECK_TITLES = {
    "coverage.in_force": "Coverage in force",
    "coverage.vehicle_listed": "Vehicle listed",
    "coverage.driver_listed": "Driver listed",
    "coverage.coverage_bought": "Coverage bought",
    "coverage.deductible": "Deductible",
    "claimtype.fit": "Claim type fit",
    "photo.plate": "License plate photo",
    "photo.vin_photo": "VIN photo",
    "photo.odometer": "Odometer photo",
    "photo.driver_license": "Driver's license photo",
    "photo.damage_closeup": "Damage close-up photo",
    "photo.damage_wide": "Damage in context photo",
    "consistency.areas": "Damaged areas",
    "consistency.prior_damage": "Earlier damage",
    "consistency.story": "Statement",
    "capture.retake_trail": "Retakes after a mismatch",
    "capture.hint_edits": "Form edits after a photo hint",
    "capture.screen": "Photo of a screen or print",
    "fnol.statement": "First notice vs statement",
    "static_source": "Static source",
    "slow_upload": "Slow upload",
    "capture.sealed": "Captured in the capture flow and sealed",
}

_LANES_HIGH = ("siu_referral", "adjuster_review")


def build(detail: dict, org_name: str, generated_at: datetime, tz: ZoneInfo) -> dict:
    """detail = Store.claim_detail(...) output with proposal status "ready". -> report data."""
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=ZoneInfo("UTC"))
    proposal = detail.get("proposal") or {}
    estimate = proposal.get("estimate") or {}
    policy = detail.get("policy") or {}
    pdata = policy.get("data") or {}
    vehicles = pdata.get("vehicles") or []
    vehicle = vehicles[0] if vehicles else None
    answers = detail.get("answers") or {}
    damages = ((detail.get("damage") or {}).get("damages")) or []
    local = generated_at.astimezone(tz)
    hour12 = local.hour % 12 or 12
    return {
        "version": VERSION,
        "generated_at": generated_at.isoformat(),
        "generated_local": f"{local:%b} {local.day}, {local.year} {hour12}:{local:%M}"
                           f" {'AM' if local.hour < 12 else 'PM'} {local.tzname()}",
        "generated_date": local.date().isoformat(),
        "tz": getattr(tz, "key", str(tz)),
        "org": org_name,
        "case_id": detail.get("id"),
        "insurer_claim_number": detail.get("insurer_claim_number"),
        "policy_number": policy.get("policy_no"),
        "policy_verified": policy.get("verified") is True,
        "policy_source": policy.get("source"),
        "insured": (pdata.get("holder") or {}).get("name"),
        "vehicle": (
            {"year": vehicle.get("year"), "make": vehicle.get("make"), "model": vehicle.get("model"),
             "vin": vehicle.get("vin"), "plate": vehicle.get("plate")}
            if vehicle else None
        ),
        "loss": {
            "date": answers.get("loss_date"),
            "type": display_answer(answers, "cause"),
            "description": answers.get("description"),
        },
        "claim_type": detail.get("claim_type") or _claim_type(answers),
        "to_verify": loss_notice.to_verify(answers),  # e.g. the police report: confirm with the department
        "recommendation": {
            "lane": proposal.get("lane"),
            "label": proposal.get("label"),
            "headline": proposal.get("headline"),
            "reasons": list(proposal.get("reasons") or []),
            "next_step": proposal.get("next_step"),
        },
        "evidence": evidence_line(proposal.get("evidence")),
        "flags": [_finding(f) for f in proposal.get("flags") or []],
        "notes": [_finding(n) for n in proposal.get("notes") or []],
        "declared_areas": [a for a in (answers.get("damaged_areas") or []) if isinstance(a, str)],
        "story_reasons": _story_reasons(detail),
        "checks": [
            {"check_id": c.get("check_id"), "title": CHECK_TITLES.get(c.get("check_id"), c.get("check_id")),
             "result": c.get("result"), "reason": c.get("reason")}
            for c in detail.get("checks") or []
        ],
        "damages": _damages(damages),  # per photo: boxes on the photos, review keys
        "damage_list": _damage_list(damages),  # one entry per physical damage: list, map, AI facts
        "cost": {
            "status": estimate.get("status"),
            "likely": estimate.get("likely"),
            "full": estimate.get("full"),
            "basis": estimate.get("basis"),
            "deductible": proposal.get("deductible"),
            "suggested_reserve": proposal.get("suggested_reserve"),
        },
        "seal": _seal(detail),
        "chain": _chain(detail),
        "photos": _photos(damages, detail.get("items")),
    }


def display_answer(answers: dict, field_id: str):
    """A loss notice answer as the adjuster reads it: the chosen option, with the
    claimant's own words after a colon when they wrote past the escape option
    (T59, e.g. "Scrape or other: A cart rolled into the door"). Anything else
    (no `other` on the field, the escape not selected, no words) reads as stored."""
    value = (answers or {}).get(field_id)
    escape = _other_option(field_id)
    words = (answers or {}).get(f"{field_id}_other")
    if not escape or not (isinstance(words, str) and words.strip()):
        return value
    if value == escape or (isinstance(value, list) and escape in value):
        shown = ", ".join(v for v in value if isinstance(v, str)) if isinstance(value, list) else value
        return f"{shown}: {words.strip()}"
    return value


def _other_option(field_id: str) -> str | None:
    """The template's escape option for this field, if it has one."""
    for section in loss_notice.template()["sections"].values():
        for field in section["fields"]:
            if field["id"] == field_id:
                return field.get("other")
    return None


def evidence_line(ev: dict | None) -> dict | None:
    """Track 5: what the adjuster sees of the evidence score: a band and its causes, never a number."""
    if not ev:
        return None
    return {"band": ev.get("band"), "causes": [c.get("label") for c in ev.get("contributions") or []],
            "missing": [m.get("label") for m in ev.get("missing") or []], "note": ev.get("note")}


def summary(report: dict) -> str:
    """Three sentences from the report data: the loss, the vehicle fit, the items to check.

    Pure: the same report data always builds the same summary. A flag the
    adjuster dismissed as a false alarm is not counted as an item to check.
    """
    loss = report.get("loss") or {}
    cause = loss.get("type") or "Loss"
    raw_desc = loss.get("description")
    if isinstance(raw_desc, str) and raw_desc.strip():
        desc = raw_desc.strip()
        if desc.endswith("."):
            desc = desc[:-1]
        if len(desc) > 80:
            cut = desc[:80]
            if " " in cut:
                cut = cut.rsplit(" ", 1)[0]
            desc = cut.rstrip() + "…"
        first = f"{cause} ({desc})."
    else:
        first = f"{cause}."
    results = {c.get("check_id"): c.get("result") for c in report.get("checks") or []}
    vehicle = vehicle_sentence(report)
    story = results.get("consistency.story")
    if story == "pass":
        second = vehicle + " and the damage fits the statement."
    elif story == "fail":
        second = vehicle + ", and the damage does not fit the statement."
    else:
        second = vehicle + "."
    dismissed = {key for key, entry in (((report.get("review") or {}).get("flags")) or {}).items()
                 if (entry or {}).get("verdict") == "false_alarm"}
    active = [f for f in report.get("flags") or []
              if (f.get("key") or _flag_key(f)) not in dismissed]
    if not active:
        third = "Nothing to check: all checks passed."
    else:
        seen: list[str] = []
        for flag in active:
            reason = (flag.get("reason") or "").strip()
            if reason.endswith("."):
                reason = reason[:-1]
            reason = reason.strip()
            if reason:
                reason = reason[0].lower() + reason[1:] if len(reason) > 1 else reason.lower()
            labels = ", ".join(flag.get("evidence") or [])
            item = f"{reason} ({labels})" if labels else reason
            if item and item not in seen:
                seen.append(item)
        third = (f"{len(active)} item{'s' if len(active) != 1 else ''} to check: "
                 f"{'; '.join(seen[:3])}.")
    return f"{first} {second} {third}"


def vehicle_matches_policy(report: dict) -> bool:
    """Only a verified policy and a pass on both the VIN and the plate photo may say the vehicle matches."""
    results = {c.get("check_id"): c.get("result") for c in report.get("checks") or []}
    return (report.get("policy_verified") is True
            and results.get("photo.vin_photo") == "pass" and results.get("photo.plate") == "pass")


def vehicle_sentence(report: dict) -> str:
    """What the VIN and plate photos say about the vehicle, never more than the checks prove.
    A match proves the VIN and plate photos show the policy's vehicle, not that the damage photos do."""
    results = {c.get("check_id"): c.get("result") for c in report.get("checks") or []}
    vin, plate = results.get("photo.vin_photo"), results.get("photo.plate")
    if report.get("policy_verified") is not True:
        return "No verified policy on file, so the vehicle could not be matched to a policy"
    if vehicle_matches_policy(report):
        return "The VIN and plate photos match the vehicle on the policy"
    if vin == "fail" or plate == "fail":
        return "The VIN or plate photo may not match the vehicle on the policy"
    return "The vehicle could not be fully confirmed from the photos"


def _flag_key(flag: dict) -> str:
    return f"{flag.get('source')}:{flag.get('reason_code')}"


def note(report: dict) -> dict:
    """-> {"topic": "general", "subject": "Verified evidence capture", "body": str}."""
    rec = report.get("recommendation") or {}
    cost = report.get("cost") or {}
    sealed = (report.get("seal") or {}).get("items") or 0
    line0 = (f"Verified capture complete: {sealed} items sealed {_sealed_day(report)}. "
             f"Recommendation: {rec.get('label')}. {rec.get('headline')}")
    line1 = _note_findings(report)
    parts = []
    likely = cost.get("likely")
    if cost.get("status") != "none" and _money_pair(likely):
        parts.append(f"Rough range ${likely[0]:,}-${likely[1]:,} (public ballpark, not an estimate)")
    reserve = cost.get("suggested_reserve")
    if type(reserve) is int:
        parts.append(f"suggested initial reserve ${reserve:,}")
    line2 = ("; ".join(parts) + ". " if parts else "") + f"See {PDF_NAME}."
    return {"topic": "general", "subject": "Verified evidence capture",
            "body": "\n".join([line0, line1, line2])}


def activity(report: dict) -> dict:
    """-> {"subject", "description", "priority": "high"|"normal", "due_date": "YYYY-MM-DD"}."""
    rec = report.get("recommendation") or {}
    lane = rec.get("lane")
    if lane == "siu_referral":
        subject = "Review verified evidence: SIU screening suggested"
    elif lane == "adjuster_review":
        subject = f"Review verified evidence: {len(report.get('flags') or [])} finding(s)"
    elif lane == "fast_track":
        subject = "Review verified evidence: fast track suggested"
    else:
        subject = "Review verified evidence"
    return {
        "subject": subject,
        "description": rec.get("next_step"),
        "priority": "high" if lane in _LANES_HIGH else "normal",
        "due_date": business_days_after(_generated_day(report), REVIEW_BUSINESS_DAYS).isoformat(),
    }


def package(report: dict, event_id: str, pdf_sha256: str, pdf_path: str, sealed_path: str) -> dict:
    """The `evidence.ready` message. Paths are relative URLs, e.g. /api/r/{token}."""
    cost = report.get("cost") or {}
    test_mode = bool(report.get("test_mode"))
    rec = {**(report.get("recommendation") or {}),
           "suggested_reserve": cost.get("suggested_reserve"), "deductible": cost.get("deductible")}
    rec["estimate"] = {"status": cost.get("status"), "likely": cost.get("likely"),
                       "full": cost.get("full"), "basis": cost.get("basis")}
    vehicle = report.get("vehicle") or {}
    return {
        "schema": PACKAGE_SCHEMA,
        "event": "evidence.ready",
        "event_id": event_id,
        "occurred_at": report.get("generated_at"),
        "test_mode": test_mode,
        "claim": {
            "case_id": report.get("case_id"),
            "insurer_claim_number": report.get("insurer_claim_number"),
            "policy_number": report.get("policy_number"),
            "vin": vehicle.get("vin"),
        },
        "recommendation": rec,
        "flags": report.get("flags"),
        "documents": [
            {"name": PDF_NAME, "document_type": "evidence_report", "mime": "application/pdf",
             "url": pdf_path, "sha256": pdf_sha256},
            {"name": f"{report.get('case_id')}-sealed.zip", "document_type": "sealed_evidence",
             "mime": "application/zip", "url": sealed_path, "sha256": None},
        ],
        "notes": [note(report)],
        "activities": [activity(report)],
        "seal": report.get("seal"),
        "report_version": VERSION,
    }


PENDING_REVIEW_SUBJECT = "Review verified evidence in Evidence Capture"


def pending_review(report: dict, event_id: str, console_path: str) -> dict:
    """The `evidence.pending_review` message (delivery mode on_decision, track 3).

    Sent when the analysis is ready but the report waits for the adjuster's review:
    one activity pointing at our console, no documents, no flags, no recommendation.
    `console_path` is relative (e.g. /claims/CLM-...), resolved by the receiver
    against our base URL like the document links.
    """
    vehicle = report.get("vehicle") or {}
    return {
        "schema": PACKAGE_SCHEMA,
        "event": "evidence.pending_review",
        "event_id": event_id,
        "occurred_at": report.get("generated_at"),
        "test_mode": bool(report.get("test_mode")),
        "claim": {
            "case_id": report.get("case_id"),
            "insurer_claim_number": report.get("insurer_claim_number"),
            "policy_number": report.get("policy_number"),
            "vin": vehicle.get("vin"),
        },
        "documents": [],
        "flags": [],
        "notes": [],
        "activities": [{
            "subject": PENDING_REVIEW_SUBJECT,
            "description": "Verified evidence is ready for review in Evidence Capture. "
                           "The report and package follow after the review.",
            "priority": "normal",
            "due_date": business_days_after(_generated_day(report), REVIEW_BUSINESS_DAYS).isoformat(),
            "url": console_path,
        }],
        "links": {"console": console_path},
    }


def ping(event_id: str, sent_at: str, org_name: str) -> dict:
    """The test-webhook message: no claim, signed exactly like real events."""
    return {"event": "ping", "id": event_id, "sent_at": sent_at, "org": org_name}


def decision_recorded(claim: dict, event_id: str, occurred_at: str, clean: dict,
                      documents: list, notes: list, activities: list,
                      complete_activities: list | None = None,
                      test_mode: bool = False, round: int | None = None) -> dict:
    """The `decision.recorded` message (track 3, on_decision mode follow-up)."""
    body: dict = {
        "schema": PACKAGE_SCHEMA,
        "event": "decision.recorded",
        "event_id": event_id,
        "occurred_at": occurred_at,
        "test_mode": bool(test_mode),
        "claim": {
            "case_id": claim.get("case_id"),
            "insurer_claim_number": claim.get("insurer_claim_number"),
            "policy_number": claim.get("policy_number"),
            "vin": claim.get("vin"),
        },
        "decision": {**clean, "at": occurred_at},
        "documents": documents,
        "flags": [],
        "notes": notes,
        "activities": activities,
        "complete_activities": list(complete_activities or []),
    }
    if round is not None and round > 1:
        body["round"] = round
    return body


def sign(body: bytes, secret: bytes, timestamp: int) -> str:
    """-> "t={timestamp},v1={hex HMAC-SHA256 of f"{timestamp}.".encode() + body}."""
    digest = hmac.new(secret, f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={timestamp},v1={digest}"


def verify_signature(body: bytes, secret: bytes, header: str) -> bool:
    """Constant-time compare (hmac.compare_digest); False on any malformed header."""
    try:
        stamp, signature = (header or "").split(",", 1)
        if not stamp.startswith("t=") or not signature.startswith("v1="):
            return False
        int(stamp[2:])
        expected = hmac.new(secret, f"{stamp[2:]}.".encode() + body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature[len("v1="):])
    except (ValueError, TypeError):
        return False


def business_days_after(day: date, n: int) -> date:
    """Skip Saturday and Sunday (no holiday calendar in round 1)."""
    out = day
    left = n
    while left > 0:
        out += timedelta(days=1)
        if out.weekday() < 5:
            left -= 1
    return out


_LATIN1_FIX = {
    "–": "-", "—": "-", "·": ".", "’": "'", "‘": "'",
    "“": '"', "”": '"', "…": "...", "✓": "[ok]", "✗": "[x]",
    "→": "->", "•": "*",
}


def latin1(text: str) -> str:
    """Replace typography and symbols with latin-1-safe forms, then drop the rest."""
    fixed = "".join(_LATIN1_FIX.get(c, c) for c in (text or ""))
    return fixed.encode("latin-1", errors="ignore").decode("latin-1")


def _slot_label(slot_id: str) -> str:
    """The short label an adjuster recognises: Close-up, Wide, or the slot id in words."""
    if slot_id == "damage_closeup":
        return "Close-up"
    if slot_id == "damage_wide":
        return "Wide"
    return (slot_id or "").replace("_", " ").title()


def _evidence_label(evidence: dict) -> str:
    """An evidence dict with `number` -> "Close-up #1"; without -> "Odometer photo"."""
    label = _slot_label(evidence.get("slot_id") or "")
    number = evidence.get("number")
    if type(number) is int:
        return f"{label} #{number}"
    if evidence.get("earlier"):
        return f"{label} photo (earlier shot)"
    return f"{label} photo"


def _note_findings(report: dict) -> str:
    """Second line of the note: the first 3 distinct finding reasons with their photo."""
    flags = report.get("flags") or []
    if not flags:
        return "Findings: All checks passed."
    seen: list[str] = []
    for flag in flags:
        reason = (flag.get("reason") or "").strip()
        if reason.endswith("."):
            reason = reason[:-1]
        labels = ", ".join(flag.get("evidence") or [])
        item = f"{reason} ({labels})" if labels else reason
        if item and item not in seen:
            seen.append(item)
    return "Findings: " + "; ".join(seen[:3]) + "."


def _finding(entry: dict) -> dict:
    return {
        "source": entry.get("source"),
        "key": f"{entry.get('source')}:{entry.get('reason_code')}",
        "level": entry.get("level"),
        "reason_code": entry.get("reason_code"),
        "reason": entry.get("reason"),
        "evidence": [_evidence_label(e) for e in entry.get("evidence") or []],
    }


def _story_reasons(detail: dict) -> list[dict]:
    """The statement-vs-photos reasons with their photo labels, for the PDF's AI notes."""
    for check in detail.get("checks") or []:
        if check.get("check_id") == "consistency.story":
            return [
                {"text": r.get("text"),
                 "evidence": [_evidence_label(e) for e in r.get("evidence") or []]}
                for r in ((check.get("value") or {}).get("reasons") or [])
                if isinstance(r, dict) and r.get("text")
            ]
    return []


def _damages(damages: list[dict]) -> list[dict]:
    """Damages in reading order, numbered per photo (the photo's own 1st, 2nd, ...)."""
    counts: dict = {}
    out = []
    for d in damages or []:
        key = d.get("item_id")
        counts[key] = counts.get(key, 0) + 1
        out.append({
            "label": f"{_slot_label(d.get('slot_id') or '')} #{counts[key]}",
            "item_id": d.get("item_id"),
            "slot_id": d.get("slot_id"),
            "number": counts[key],
            "key": f"{d.get('item_id')}:{counts[key]}",
            "area": d.get("area"),
            "damage_type": d.get("damage_type"),
            "severity": d.get("severity"),
            "age": d.get("age"),
            "box": d.get("box"),
            "observed": d.get("observed") if isinstance(d.get("observed"), str) else "",
            "outline": _outline(d.get("outline")),
        })
    return out


def _outline(value) -> dict | None:
    """The SAM 3.1 outline of one damage, as the PDF draws it (track 6); None when there is none."""
    if not isinstance(value, dict):
        return None
    return {"sam_agrees": value.get("sam_agrees"), "regions": value.get("regions") or [],
            "area": value.get("area")}


def _damage_list(damages: list[dict]) -> list[dict]:
    """Each physical damage once (damage.merge), numbered 1..n, with every photo label that shows it."""
    out = []
    for n, d in enumerate(damage_lib.merge(damages or []), 1):
        out.append({
            "n": n,
            "label": _evidence_label(d),
            "labels": [_evidence_label(e) for e in d["evidence"]],
            "keys": d["keys"],
            "area": d["area"],
            "damage_type": d["damage_type"],
            "severity": d["severity"],
            "age": d["age"],
            "observed": d["observed"],
        })
    return out


def _photos(damages: list[dict], items: list[dict] | None = None) -> list[dict]:
    """Current damage photos that have a reading, in reading order, listed once."""
    seen: dict = {}
    for d in damages or []:
        if d.get("item_id") not in seen:
            seen[d.get("item_id")] = d.get("slot_id")
    flags_by_item = {}
    for item in items or []:
        if isinstance(item, dict) and item.get("id"):
            flags_by_item[item["id"]] = list(item.get("flags") or [])
    out = []
    for item_id, slot_id in seen.items():
        entry = {"item_id": item_id, "slot_id": slot_id, "label": f"{_slot_label(slot_id or '')} photo"}
        if flags_by_item.get(item_id):
            entry["flags"] = flags_by_item[item_id]
        out.append(entry)
    return out


def _seal(detail: dict) -> dict | None:
    """sealed_at and key_id from the seal row; the manifest hash and item count from the log."""
    info = detail.get("seal")
    if not info:
        return None
    sealed = next((e for e in detail.get("events") or [] if e.get("type") == "claim.sealed"), None)
    event_data = (sealed or {}).get("data") or {}
    return {
        "sealed_at": info.get("sealed_at"),
        "key_id": info.get("key_id"),
        "manifest_sha256": info.get("manifest_sha256") or event_data.get("manifest_sha256"),
        "items": event_data.get("items"),
    }


def _chain(detail: dict) -> dict:
    events = detail.get("events") or []
    return {"events": len(events), "head": events[-1].get("hash") if events else None}


def _claim_type(answers: dict) -> dict | None:
    """{"id", "label"} from the overlay; None when no type."""
    overlay = loss_notice.template()["overlays"].get((answers or {}).get("loss_type"))
    if overlay is None:
        return None
    return {"id": overlay["id"], "label": overlay["label"]}


def _sealed_day(report: dict) -> str:
    """The sealed date for the note's first line ("Oct 30, 2026" style)."""
    raw = (report.get("seal") or {}).get("sealed_at") or report.get("generated_at")
    try:
        day = datetime.fromisoformat(raw)
        return f"{day:%b} {day.day}, {day.year}"
    except (TypeError, ValueError):
        return ""


def _generated_day(report: dict) -> date:
    """The claim-local date the report was generated, from generated_date when present."""
    raw = report.get("generated_date")
    if raw:
        try:
            return date.fromisoformat(raw)
        except (TypeError, ValueError):
            pass
    try:
        return datetime.strptime(" ".join((report.get("generated_local") or "").split()[:3]), "%b %d, %Y").date()
    except (TypeError, ValueError):
        return datetime.fromisoformat(report.get("generated_at")).date()


def _money_pair(value) -> bool:
    return (isinstance(value, (list, tuple)) and len(value) == 2
            and type(value[0]) is int and type(value[1]) is int)
