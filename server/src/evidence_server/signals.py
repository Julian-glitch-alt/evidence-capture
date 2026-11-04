"""Cheap, explainable signals for the adjuster (track 2, docs/explorations/algorithms.md).

Pure: no database, no clock, no network, no model call. Computed on read from
stored rows, like the proposal. Each signal returns check rows shaped like
coverage results ({"check_id", "result", "reason_code", "reason", "value",
"inputs"}); `value["evidence"]` lists the photos to look at. A signal never
blocks the claimant, never rejects and never decides money: an `unclear` only
asks a person to look. A signal with nothing to say returns no row, and a
signal with no input (no first notice, no screen answer) is missing, never a pass.
"""

import re
from datetime import date

from . import photo_check
from .coverage import _needed_coverage

VERSION = "signals/v1"
CHECK_ORDER = ("capture.retake_trail", "capture.hint_edits", "capture.screen", "fnol.statement",
               "static_source", "slow_upload")

# Retake reasons that say the shot showed something else than the claim, not a bad photo.
MISMATCH_CODES = {"PLATE_MISMATCH", "VIN_MISMATCH", "LICENSE_NAME_MISMATCH", "DAMAGE_AREA_MISMATCH"}

SLOT_WORDS = {
    "plate": "license plate", "vin_photo": "VIN", "odometer": "odometer", "driver_license": "driver's license",
    "damage_closeup": "damage close-up", "damage_wide": "damage in context",
}


def rows(photo_rows: list[dict], ctx: dict, events: list[dict], answers: dict,
         items: list[dict] | None = None) -> list[dict]:
    """Every signal's rows in CHECK_ORDER.
    photo_rows / ctx: exactly what photo_check.view takes (superseded shots included);
    events: the claim's log; answers: the submitted form; items: item publics
    with server facts (liveness and nonce latency), when available."""
    opened = next((e for e in events or [] if e.get("type") == "claim.opened"), None)
    out = [retake_trail(photo_rows, ctx), hint_edits(events), screen(photo_rows),
           fnol(((opened or {}).get("data") or {}).get("loss"), answers),
           static_source(items), slow_upload(items)]
    return [r for r in out if r is not None]


def static_source(items: list[dict] | None) -> dict | None:
    """Photos flagged as a possible static source (liveness frames missing or
    near-identical). A flag for the adjuster, never a block."""
    flagged = [i for i in items or [] if not i.get("superseded") and "static_source" in (i.get("flags") or [])]
    if not flagged:
        return None
    reasons = []
    for i in flagged:
        liveness = i.get("liveness")
        if not isinstance(liveness, dict):
            liveness = (i.get("server_facts") or {}).get("liveness")
        reason = liveness.get("reason") if isinstance(liveness, dict) else None
        if reason and reason not in reasons:
            reasons.append(reason)
    reason_text = reasons[0] if reasons else "Liveness frames suggest a static source."
    n = len(flagged)
    return _row(
        "static_source", "unclear", "STATIC_SOURCE",
        f"{n} photo{'s' if n > 1 else ''} flagged as a possible static source: {reason_text}",
        {"evidence": [{"item_id": i.get("id"), "slot_id": i.get("step_id")} for i in flagged]},
        {"photo.static_source": n},
    )


def slow_upload(items: list[dict] | None) -> dict | None:
    """Photos whose nonce-to-upload latency exceeded 60 seconds."""
    flagged = [i for i in items or [] if not i.get("superseded") and "slow_upload" in (i.get("flags") or [])]
    if not flagged:
        return None
    n = len(flagged)
    return _row(
        "slow_upload", "unclear", "SLOW_UPLOAD",
        f"{n} photo{'s' if n > 1 else ''} arrived more than 60 seconds after the capture code was issued.",
        {"evidence": [{"item_id": i.get("id"), "slot_id": i.get("step_id")} for i in flagged],
         "latencies": [i.get("nonce_to_upload_latency_seconds") for i in flagged]},
        {"photo.slow_upload": n},
    )


# --- 4. coaching trail ---


def retake_trail(photo_rows: list[dict], ctx: dict) -> dict | None:
    """Superseded shots that showed a different plate, VIN, licence name or damage area.

    The claimant only sees "Retake this photo" (or the damage-area hint Julian kept);
    the adjuster sees the earlier shot. Judged against the policy and today's answers,
    as photo_check.decide would judge it now."""
    shots = []
    for row in sorted(photo_rows or [], key=lambda r: (r["received_at"], r["item_id"])):
        if not row.get("superseded") or row.get("status") != "done":
            continue
        decided = photo_check.decide(row["slot_id"], row.get("readings"), ctx, 1)
        if decided["result"] == "retake" and decided["reason_code"] in MISMATCH_CODES:
            shots.append({"item_id": row["item_id"], "slot_id": row["slot_id"], "received_at": row["received_at"],
                          "reason_code": decided["reason_code"], "reason": decided["reason"]})
    if not shots:
        return None
    slots = list(dict.fromkeys(SLOT_WORDS.get(s["slot_id"], s["slot_id"]) for s in shots))
    n = len(shots)
    return _row(
        "capture.retake_trail", "unclear", "RETAKE_AFTER_MISMATCH",
        f"{n} earlier shot{'s' if n > 1 else ''} did not match the claim and {'were' if n > 1 else 'was'}"
        f" retaken ({', '.join(slots)}). Look at the earlier shot{'s' if n > 1 else ''}.",
        {"shots": shots, "evidence": [_earlier(s) for s in shots]},
        {"photo.superseded_mismatches": n},
    )


def hint_edits(events: list[dict]) -> dict | None:
    """Form edits made while a photo hint invited them (answers.changed_after_photo_hint),
    with the photo that showed the hint as evidence."""
    edits = [e.get("data") or {} for e in events or [] if e.get("type") == "answers.changed_after_photo_hint"]
    if not edits:
        return None
    fields = list(dict.fromkeys(str(e.get("field")) for e in edits))
    evidence = []
    for e in edits:
        ref = {"item_id": e.get("item_id"), "slot_id": e.get("slot"), "earlier": True}
        if ref not in evidence:
            evidence.append(ref)
    return _row(
        "capture.hint_edits", "unclear", "ANSWER_CHANGED_AFTER_HINT",
        f"The claimant changed {', '.join(f.replace('_', ' ') for f in fields)} while a photo hint invited it."
        " It may be a correction or coaching; compare the earlier photo with the answer.",
        {"edits": [{k: e.get(k) for k in ("item_id", "slot", "reason_code", "field", "before", "after")} for e in edits],
         "evidence": evidence},
        {"events.changed_after_photo_hint": len(edits)},
    )


# --- 5. screen or print re-capture ---


def screen(photo_rows: list[dict]) -> dict | None:
    """Photos the model calls a picture of a screen or a print ("screen_or_print": "yes").

    A model-only judgment: it asks a person to look, never blocks, never counts
    toward SIU. Reads made before the question existed carry no answer: missing,
    so they never make a pass."""
    seen, answered, missing = [], 0, 0
    for row in sorted(photo_rows or [], key=lambda r: (r["received_at"], r["item_id"])):
        if row.get("status") != "done" or not isinstance(row.get("readings"), list):
            continue
        said = [r.get("screen_or_print") for r in row["readings"] if isinstance(r, dict)]
        if any(v == "yes" for v in said):
            seen.append(row)
        if any(v in ("yes", "no", "unsure") for v in said):
            answered += 1
        else:
            missing += 1
    if not seen:
        return None
    slots = list(dict.fromkeys(SLOT_WORDS.get(r["slot_id"], r["slot_id"]) for r in seen))
    n = len(seen)
    evidence = [{"item_id": r["item_id"], "slot_id": r["slot_id"], **({"earlier": True} if r.get("superseded") else {})}
                for r in seen]
    return _row(
        "capture.screen", "unclear", "SCREEN_RECAPTURE_SUSPECTED",
        f"The model thinks {n} photo{'s' if n > 1 else ''} may show a screen or a printed picture"
        f" rather than the vehicle itself ({', '.join(slots)}). Look at the photo; this is not a finding of fraud.",
        {"evidence": evidence},
        {"photo.screen_yes": n, "photo.screen_answered": answered, "photo.screen_missing": missing},
    )


# --- 6. first notice of loss vs statement ---

# Words that name one end of the car without ambiguity. Left/right, "back", "backed" and
# "driver" were dropped: "I left the car", "came back", "a truck backed into me" and
# "the other driver" made false contradictions. "in front of" is removed before matching.
_FRONT = {"front", "hood", "headlight", "headlights", "grille", "windshield", "head-on"}
_REAR = {"rear", "trunk", "tailgate", "taillight", "taillights", "rear-ended"}


def fnol(loss, answers: dict) -> dict | None:
    """The loss the insurer sent with the capture request (claim.opened data["loss"]) against
    the claimant's answers. Code compares the loss date and the coverage the cause maps to;
    free text is compared only for an impact end that contradicts (front vs rear).
    Both texts always travel in the value. None when the insurer sent nothing."""
    if not isinstance(loss, dict):
        return None
    first = {
        "date": _str(loss.get("date") or loss.get("loss_date")),
        "cause": _str(loss.get("cause") or loss.get("loss_cause") or loss.get("type")),
        "description": _str(loss.get("description")),
    }
    if not any(first.values()):
        return None
    said = {
        "date": _str(answers.get("loss_date")),
        "cause": _str(answers.get("cause") or answers.get("what_happened")),
        "coverage": _needed_coverage(answers),
        "description": _str(answers.get("description")),
    }
    differences, compared = [], []
    a, b = _day(first["date"]), _day(said["date"])
    if a is not None and b is not None:
        compared.append("loss date")
        if a != b:
            differences.append({"field": "loss date", "fnol": first["date"], "statement": said["date"]})
    kind = (first["cause"] or "").strip().lower()
    if kind in ("collision", "comprehensive") and said["coverage"] is not None:
        compared.append("cause")
        if kind != said["coverage"]:
            differences.append({"field": "cause", "fnol": first["cause"],
                                "statement": f"{said['cause'] or 'not given'} ({said['coverage']})"})
    x, y = _end(first["description"]), _end(said["description"])
    if x is not None and y is not None:
        compared.append("impact end")
        if x != y:
            differences.append({"field": "impact end", "fnol": x, "statement": y})
    value = {"fnol": first, "statement": said, "compared": compared, "differences": differences, "evidence": []}
    inputs = {"fnol.date": first["date"], "fnol.cause": first["cause"], "form.loss_date": said["date"],
              "form.cause": said["cause"]}
    if differences:
        parts = [f"{d['field']} (first notice: {d['fnol']}; statement: {d['statement']})" for d in differences]
        return _row("fnol.statement", "unclear", "FNOL_MISMATCH",
                    "The statement differs from the first notice of loss: " + "; ".join(parts) + ".", value, inputs)
    if not compared:
        return _row("fnol.statement", "value", None,
                    "The first notice of loss is attached for comparison; nothing in it could be compared by rule.",
                    value, inputs)
    return _row("fnol.statement", "pass", None,
                f"The statement matches the first notice of loss on {', '.join(compared)}.", value, inputs)


def _end(text) -> str | None:
    """"front" or "rear" when the text names only one end of the car; None when it names none or both."""
    if not text:
        return None
    words = set(re.findall(r"[a-z]+(?:-[a-z]+)?", re.sub(r"\bin front of\b", " ", text.lower())))
    front, rear = bool(words & _FRONT), bool(words & _REAR)
    if front == rear:
        return None
    return "front" if front else "rear"


def _day(value) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _str(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _earlier(shot: dict) -> dict:
    return {"item_id": shot["item_id"], "slot_id": shot["slot_id"], "earlier": True}


def _row(check_id: str, result: str, reason_code: str | None, reason: str, value, inputs: dict) -> dict:
    return {"check_id": check_id, "result": result, "reason_code": reason_code, "reason": reason,
            "value": value, "inputs": inputs, "source": VERSION}
