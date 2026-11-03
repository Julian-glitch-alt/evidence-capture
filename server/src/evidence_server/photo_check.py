"""Photo check before signing (T34, tasks/t34-design.md): the model reads, this code decides.

Pure: no database, no clock, no network. A result never blocks a claim;
a `fail` row only sends it to an adjuster later.
"""

import json
import re
import uuid
from datetime import date, datetime, timedelta

import psycopg

from .loss_notice import vin_error
from .photo_prompts import AREAS
from . import viewpoint as _viewpoint
from .policies import canonical

VERSION = "photo/v4"
CHECKED_SLOTS = ("plate", "vin_photo", "odometer", "driver_license", "damage_closeup", "damage_wide")
_DAMAGE_SLOTS = ("damage_closeup", "damage_wide")
MAX_SHOTS = 3
STALE_AFTER = timedelta(seconds=45)
MILES_PER_YEAR_MAX = 30_000

ADJACENT: dict[str, set[str]] = {
    "Front bumper": {"Hood", "Front left", "Front right", "Underneath"},
    "Hood": {"Front bumper", "Front left", "Front right", "Windshield"},
    "Front left": {"Front right", "Front bumper", "Hood", "Left side", "Wheels or tires"},
    "Front right": {"Front left", "Front bumper", "Hood", "Right side", "Wheels or tires"},
    "Left side": {"Right side", "Front left", "Rear left", "Roof", "Wheels or tires"},
    "Right side": {"Left side", "Front right", "Rear right", "Roof", "Wheels or tires"},
    "Rear left": {"Rear right", "Rear bumper", "Left side", "Trunk or tailgate", "Wheels or tires"},
    "Rear right": {"Rear left", "Rear bumper", "Right side", "Trunk or tailgate", "Wheels or tires"},
    "Rear bumper": {"Rear left", "Rear right", "Trunk or tailgate", "Underneath"},
    "Trunk or tailgate": {"Rear bumper", "Rear left", "Rear right", "Rear window"},
    "Roof": {"Windshield", "Rear window", "Left side", "Right side"},
    "Windshield": {"Hood", "Roof"},
    "Rear window": {"Roof", "Trunk or tailgate"},
    "Wheels or tires": {"Front left", "Front right", "Left side", "Right side", "Rear left", "Rear right",
                        "Underneath"},
    "Interior": set(),
    "Underneath": {"Front bumper", "Rear bumper", "Wheels or tires"},
}

_SUBJECT_KEY = {
    "plate": "is_plate",
    "vin_photo": "is_vin",
    "odometer": "is_odometer",
    "driver_license": "is_license",
    "damage_closeup": "is_vehicle",
    "damage_wide": "is_vehicle",
}

_NO_SUBJECT = {
    "plate": "a license plate",
    "vin_photo": "a VIN",
    "odometer": "the odometer",
    "driver_license": "a driver's license",
    "damage_closeup": "a vehicle",
    "damage_wide": "a vehicle",
}

_UNREADABLE_NOUN = {
    "plate": "license plate",
    "vin_photo": "VIN",
    "odometer": "odometer",
    "driver_license": "driver's license",
    "damage_closeup": "damage",
    "damage_wide": "damage",
}

PROBLEM_HINTS = {
    "blurry": "The photo is blurry. Hold the phone still and tap the screen to focus.",
    "too_dark": "The photo is too dark. Move to a brighter spot or turn on more light.",
    "glare": "There is glare on the photo. Tilt the phone so the light does not reflect.",
    "too_far": "Move closer so it fills more of the frame.",
    "cut_off": "Part of it is cut off. Keep all of it inside the frame.",
}

UNREADABLE_HINTS = {
    "plate": "We could not read the plate. Photograph it straight on so every character is sharp.",
    "vin_photo": "We could not read the VIN. Get close, avoid glare, and keep all 17 characters in the frame.",
    "odometer": "We could not read the odometer. Switch on the ignition and photograph the mileage straight on.",
    "driver_license": "We could not read the license. Lay it flat in good light and avoid glare.",
    "damage_closeup": "We could not see the damage clearly. Get closer to the damaged area.",
    "damage_wide": "Step back so the damage and the surrounding part of the vehicle are both visible.",
}

WRONG_SUBJECT_HINTS = {
    "plate": "This does not look like a license plate. Photograph the plate on this vehicle.",
    "vin_photo": "This does not look like a VIN. Photograph the VIN at the bottom of the windshield or on the driver's door frame.",
    "odometer": "This does not look like the odometer. Switch on the ignition and photograph the mileage.",
    "driver_license": "This does not look like a driver's license. Photograph the front of the license.",
    "damage_closeup": "We could not see a vehicle in this photo. Photograph the damaged vehicle.",
    "damage_wide": "We could not see a vehicle in this photo. Photograph the damaged vehicle.",
}

POOR_QUALITY = {  # problems that make a reading untrustworthy, per slot
    "plate": {"blurry", "too_dark", "glare"},
    "vin_photo": {"blurry", "too_dark", "glare"},
    "odometer": {"blurry", "too_dark", "glare"},
    "driver_license": {"blurry", "too_dark", "glare"},
    "damage_closeup": {"blurry", "too_dark"},  # paint always reflects a little
    "damage_wide": {"blurry", "too_dark"},
}

QUALITY_WORDS = {"blurry": "blurry", "too_dark": "too dark", "glare": "affected by glare"}

# Identity mismatches (plate, VIN, licence name) get the same plain hint: telling the
# claimant what did not match teaches a fraudster what to fix. The adjuster sees the
# earlier shot and the exact reason (signals.retake_trail). Judge review 1 Nov, Claude #4.
IDENTITY_HINT = "Retake this photo."

_CHECK_RUNNING = "The photo check is still running."
_NOT_CHECKED = "The photo could not be checked automatically."
_NOT_FINISHED = "The photo check had not finished when the claim was sent."
LICENSE_NOT_SENT = "License photos are not sent to the AI (privacy setting). Check the license yourself."


def decide(slot_id: str, readings: list | None, ctx: dict, attempt: int = 1) -> dict:
    """ctx = {"policy": parsed policy data or None when the snapshot is not verified,
    "answers": the claimant's answers now, "today": date}.
    -> {"result": "pass" | "retake" | "flagged" | "unclear" | "unchecked", "reason_code": str | None,
        "reason": str, "hint": str | None, "value": ..., "inputs": dict}
    "unclear": read fine but nothing trustworthy to match it against (no verified policy);
    the claimant sees it as saved, the adjuster sees why it is not a pass."""
    if slot_id not in _SUBJECT_KEY:
        return _unchecked()
    shaped = _shaped(slot_id, readings)
    if shaped is None:
        return _unchecked()
    subject = _SUBJECT_KEY[slot_id]
    # A second damage reading never blocks the early checks: only the first
    # must be usable (T49); the second is judged in _damage and ignored when
    # it is unusable or unsure.
    early = shaped[:1] if slot_id in _DAMAGE_SLOTS else shaped
    for answer in early:
        if not answer[subject]:
            return _finish(slot_id, "retake", "PHOTO_WRONG_SUBJECT",
                           f"The photo does not show {_NO_SUBJECT[slot_id]}.",
                           WRONG_SUBJECT_HINTS[slot_id], None, {}, attempt)
    for index, answer in enumerate(early):
        if answer["unsure"] or _needed_null(slot_id, answer, index):
            hint = PROBLEM_HINTS.get(answer["problem"], UNREADABLE_HINTS[slot_id])
            return _finish(slot_id, "retake", "PHOTO_UNREADABLE",
                           f"The {_UNREADABLE_NOUN[slot_id]} in the photo could not be read.",
                           hint, None, {}, attempt)
    for answer in early:
        if answer["problem"] in POOR_QUALITY.get(slot_id, set()):
            return _finish(slot_id, "retake", "PHOTO_POOR_QUALITY",
                           f"The photo could be read but is {QUALITY_WORDS[answer['problem']]},"
                           " so the reading is not trusted.",
                           PROBLEM_HINTS[answer["problem"]], None,
                           {"photo.problems": [a["problem"] for a in early]}, attempt)
    policy = ctx.get("policy")
    answers = ctx.get("answers") or {}
    if slot_id == "plate":
        return _plate(shaped[0], policy, attempt)
    if slot_id == "vin_photo":
        return _vin(shaped[0], shaped[1], policy, answers, attempt)
    if slot_id == "odometer":
        return _odometer(shaped[0], policy, answers, ctx.get("today"), attempt)
    if slot_id == "driver_license":
        return _license(shaped[0], policy, answers, ctx.get("today"), attempt)
    return _damage(slot_id, shaped, answers, attempt)


def view(rows: list[dict], ctx: dict, now: datetime) -> list[dict]:
    """rows: every item of the claim in CHECKED_SLOTS (superseded too), each
    {"item_id", "slot_id", "received_at", "superseded": bool, "status": "pending"|"done"|"failed"|None,
     "readings": list | None, "started_at": str | None, "error": str | None}.
    -> one entry per CURRENT item, ordered by received_at:
       {"item_id", "slot_id", "result": "pending"|"pass"|"retake"|"flagged"|"unchecked",
        "reason_code", "reason", "hint", "value", "inputs", "attempt", "shots_left"}"""
    ordered = sorted(rows, key=lambda r: (r["received_at"], r["item_id"]))
    out = []
    for row in ordered:
        if row["superseded"]:
            continue
        status = row.get("status")
        started = _parse_dt(row.get("started_at"))
        attempt = _attempt(row, ordered, ctx)
        if (status is None or status == "pending") and started is not None and now - started <= STALE_AFTER:
            out.append({
                "item_id": row["item_id"], "slot_id": row["slot_id"], "result": "pending",
                "reason_code": None, "reason": _CHECK_RUNNING, "hint": None, "value": None,
                "inputs": None, "attempt": attempt, "shots_left": max(0, MAX_SHOTS - attempt),
            })
        else:
            readings = row.get("readings") if status == "done" else None
            entry = {
                "item_id": row["item_id"], "slot_id": row["slot_id"], "attempt": attempt,
                "shots_left": max(0, MAX_SHOTS - attempt),
                **decide(row["slot_id"], readings, ctx, attempt),
            }
            if row["slot_id"] == "driver_license" and row.get("error") == "license_check_off":
                entry["reason"] = LICENSE_NOT_SENT
            out.append(entry)
    return out


def public(entries: list[dict]) -> list[dict]:
    """The customer's subset: item_id, slot_id, result, hint, shots_left. An `unclear`
    (nothing to match against) shows as `unchecked`: saved, nothing asked of them."""
    return [
        {"item_id": e["item_id"], "slot_id": e["slot_id"],
         "result": "unchecked" if e["result"] == "unclear" else e["result"],
         "hint": e["hint"], "shots_left": e["shots_left"]}
        for e in entries
    ]


HINT_FIELDS = {  # reason codes whose hint invites the customer to change the form
    "DAMAGE_AREA_MISMATCH": ("damaged_areas",),
    "VIN_MISMATCH": ("vin",),
    "LICENSE_NAME_MISMATCH": ("driver_is_holder", "driver_name"),
}


def hint_edits(entries: list[dict], before: dict, after: dict) -> list[dict]:
    """Form changes made while the customer was looking at a hint that invites them."""
    out = []
    for entry in entries:
        if entry.get("result") != "retake":
            continue
        fields = HINT_FIELDS.get(entry.get("reason_code"))
        if not fields:
            continue
        for field in fields:
            old, new = before.get(field), after.get(field)
            if old != new:
                out.append({"item_id": entry["item_id"], "slot": entry["slot_id"],
                            "reason_code": entry["reason_code"], "field": field,
                            "before": old, "after": new})
    return out


def rows(entries: list[dict]) -> list[dict]:
    """One check result per slot in CHECKED_SLOTS that has an entry, shaped like coverage results:
    {"check_id", "result": "pass"|"unclear"|"fail", "reason_code", "reason", "value", "inputs"}.
    The worst entry of a slot wins (fail beats unclear beats pass). Pure; save() stores these."""
    by_slot: dict[str, list[dict]] = {}
    for entry in entries:
        by_slot.setdefault(entry["slot_id"], []).append(entry)
    out = []
    for slot_id in CHECKED_SLOTS:
        slot_entries = by_slot.get(slot_id)
        if not slot_entries:
            continue
        worst = max(slot_entries, key=lambda e: _RANK.get(e["result"], 0))
        if worst["result"] == "pass":
            result, code, reason = "pass", None, worst["reason"]
        elif worst["result"] in ("retake", "flagged"):
            result, code, reason = "fail", worst["reason_code"], worst["reason"]
        elif worst["result"] == "pending":
            result, code, reason = "unclear", "PHOTO_UNCHECKED", _NOT_FINISHED
        elif worst["result"] == "unclear":
            result, code, reason = "unclear", worst["reason_code"], worst["reason"]
        else:
            result, code, reason = "unclear", "PHOTO_UNCHECKED", worst["reason"]
        out.append({
            "check_id": f"photo.{slot_id}", "result": result, "reason_code": code, "reason": reason,
            "value": worst["value"], "inputs": dict(worst["inputs"] or {}) | {"item_id": worst["item_id"]},
        })
    return out


def save(conn: psycopg.Connection, claim_id: str, entries: list[dict], at: str) -> None:
    """Upsert one `checks` row per rows() result (ON CONFLICT (claim_id, check_id) DO UPDATE)."""
    for r in rows(entries):
        conn.execute(
            "INSERT INTO checks (id, claim_id, check_id, result, reason_code, reason,"
            " value_json, inputs_json, source, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (claim_id, check_id) DO UPDATE SET result=EXCLUDED.result,"
            " reason_code=EXCLUDED.reason_code, reason=EXCLUDED.reason, value_json=EXCLUDED.value_json,"
            " inputs_json=EXCLUDED.inputs_json, source=EXCLUDED.source, created_at=EXCLUDED.created_at",
            (
                uuid.uuid4().hex, claim_id, r["check_id"], r["result"], r["reason_code"], r["reason"],
                canonical(r["value"]), canonical(r["inputs"]), VERSION, at,
            ),
        )


def load(conn: psycopg.Connection | None, claim_id: str, rows: list[dict] | None = None) -> list[dict]:
    """`photo.*` rows of `checks` in CHECKED_SLOTS order, shaped like coverage.load()."""
    if rows is None:  # the queue passes the claim's rows, read in one batch for every claim
        rows = conn.execute("SELECT * FROM checks WHERE claim_id=%s", (claim_id,)).fetchall()
    by_id = {
        row["check_id"]: {
            "check_id": row["check_id"],
            "result": row["result"],
            "reason_code": row["reason_code"],
            "reason": row["reason"],
            "value": json.loads(row["value_json"]),
            "inputs": json.loads(row["inputs_json"]),
            "source": row["source"],
            "created_at": row["created_at"],
        }
        for row in rows
    }
    return [by_id[f"photo.{s}"] for s in CHECKED_SLOTS if f"photo.{s}" in by_id]


# fail beats unclear beats pass; max() keeps the earliest received on ties (view order).
_RANK = {"retake": 2, "flagged": 2, "unclear": 1, "pending": 1}


def _attempt(entry: dict, ordered: list[dict], ctx: dict) -> int:
    """1 + earlier superseded shots of the same slot that needed a retake. A voluntary
    retake of a good photo, and unchecked shots, do not use up shots."""
    n = 0
    for row in ordered:
        if row["item_id"] == entry["item_id"]:
            break
        if row["slot_id"] != entry["slot_id"] or not row["superseded"]:
            continue
        if row.get("status") != "done":
            continue
        if decide(entry["slot_id"], row.get("readings"), ctx, 1)["result"] == "retake":
            n += 1
    return 1 + n


def _parse_dt(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _parse_date(value) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _unchecked() -> dict:
    return {
        "result": "unchecked", "reason_code": "PHOTO_UNCHECKED", "reason": _NOT_CHECKED,
        "hint": None, "value": None, "inputs": {},
    }


def _finish(slot_id: str, result: str, reason_code: str | None, reason: str,
            hint: str | None, value, inputs: dict, attempt: int) -> dict:
    """A `retake` on the third shot becomes `flagged`; pass and unchecked never change."""
    if result == "retake" and attempt >= MAX_SHOTS:
        return {"result": "flagged", "reason_code": reason_code, "reason": reason,
                "hint": None, "value": value, "inputs": inputs}
    if result != "retake":
        hint = None
    return {"result": result, "reason_code": reason_code, "reason": reason,
            "hint": hint, "value": value, "inputs": inputs}


def _unclear(reason_code: str, reason: str, value, inputs: dict) -> dict:
    return {"result": "unclear", "reason_code": reason_code, "reason": reason,
            "hint": None, "value": value, "inputs": inputs}


def _pass(reason: str, value, inputs: dict) -> dict:
    return {"result": "pass", "reason_code": None, "reason": reason,
            "hint": None, "value": value, "inputs": inputs}


def _base_ok(answer) -> bool:
    return (
        isinstance(answer, dict)
        and type(answer.get("unsure")) is bool
        and isinstance(answer.get("problem"), str)
    )


def _str_or_null(answer: dict, key: str) -> bool:
    return key in answer and (answer[key] is None or isinstance(answer[key], str))


def _shaped(slot_id: str, readings) -> list | None:
    """The model answers when they have the expected shape, else None (P3).

    The damage slots accept one or two readings; only the first must be usable.
    A second reading that is unusable or unsure is ignored by decide/_damage."""
    if slot_id in _DAMAGE_SLOTS:
        if not isinstance(readings, list) or len(readings) not in (1, 2):
            return None
        if not _base_ok(readings[0]):
            return None
    else:
        if not isinstance(readings, list) or len(readings) != (2 if slot_id == "vin_photo" else 1):
            return None
        if any(not _base_ok(a) for a in readings):
            return None
    if slot_id == "plate":
        a = readings[0]
        if type(a.get("is_plate")) is not bool or not _str_or_null(a, "text") or not _str_or_null(a, "state"):
            return None
    elif slot_id == "vin_photo":
        first, second = readings
        if type(first.get("is_vin")) is not bool or not _str_or_null(first, "vin"):
            return None
        if type(second.get("is_vin")) is not bool:
            return None
        if "characters" not in second:
            return None
        chars = second["characters"]
        if chars is not None and (not isinstance(chars, list) or any(not isinstance(c, str) for c in chars)):
            return None
    elif slot_id == "odometer":
        a = readings[0]
        if type(a.get("is_odometer")) is not bool or "unit" not in a or a["unit"] not in ("mi", "km", None):
            return None
        if "reading" not in a:
            return None
        reading = a["reading"]
        if reading is None:
            return readings
        if isinstance(reading, bool) or not isinstance(reading, (int, float)):
            return None
        try:
            whole = int(reading)
        except (OverflowError, ValueError):
            return None
        if float(reading) != whole:  # an integral float reads as an int; anything else is unreadable
            return None
    elif slot_id == "driver_license":
        a = readings[0]
        if type(a.get("is_license")) is not bool or not _str_or_null(a, "name") or not _str_or_null(a, "state"):
            return None
        if not _str_or_null(a, "expires"):
            return None
    else:
        a = readings[0]
        if type(a.get("is_vehicle")) is not bool or type(a.get("damage_visible")) is not bool:
            return None
        if not isinstance(a.get("parts"), list) or any(not isinstance(p, str) for p in a["parts"]):
            return None
    return readings


def _needed_null(slot_id: str, answer: dict, index: int) -> bool:
    if slot_id == "vin_photo":
        return answer.get("vin" if index == 0 else "characters") is None
    key = {"plate": "text", "odometer": "reading", "driver_license": "name"}.get(slot_id)
    return key is not None and answer.get(key) is None


def _plate(answer: dict, policy: dict | None, attempt: int) -> dict:
    text = _norm_plate(answer["text"])
    inputs = {"photo.plate": text}
    if policy is None:  # never "pass": nothing says this plate belongs to the insured vehicle
        return _unclear("POLICY_NOT_VERIFIED",
                        f"Plate {text} was read, but there is no verified policy on file to match it against.",
                        text, inputs)
    plates = [_norm_plate(v.get("plate") or "") for v in policy.get("vehicles", [])]
    inputs["policy.plates"] = plates
    if text in plates:
        return _pass(f"Plate {text} matches a vehicle on the policy.", text, inputs)
    return _finish("plate", "retake", "PLATE_MISMATCH",
                   f"Plate {text} does not match any vehicle on the policy.",
                   IDENTITY_HINT,
                   text, inputs, attempt)


def _vin(first: dict, second: dict, policy: dict | None, answers: dict, attempt: int) -> dict:
    read = _norm_vin(first["vin"])
    other = _norm_vin("".join(second["characters"]))
    inputs = {"photo.vin": [read, other]}
    if read != other:
        return _finish("vin_photo", "retake", "PHOTO_READS_DISAGREE",
                       "The two VIN reads of the photo do not agree.",
                       UNREADABLE_HINTS["vin_photo"], None, inputs, attempt)
    invalid = vin_error(read)
    if invalid is not None:
        return _finish("vin_photo", "retake", "VIN_READ_INVALID", invalid,
                       UNREADABLE_HINTS["vin_photo"], read, inputs, attempt)
    if policy is not None:
        vins = [v.get("vin") for v in policy.get("vehicles", [])]
        inputs["policy.vins"] = vins
        if not any(isinstance(v, str) and _norm_vin(v) == read for v in vins):
            return _finish("vin_photo", "retake", "VIN_MISMATCH",
                           f"VIN {read} is not listed on the policy.",
                           IDENTITY_HINT,
                           read, inputs, attempt)
        return _pass(f"VIN {read} matches the vehicle on the policy.", read, inputs)
    typed = answers.get("vin")
    if isinstance(typed, str) and typed.strip():
        inputs["form.vin"] = typed
        if _norm_vin(typed) != read:
            return _finish("vin_photo", "retake", "VIN_MISMATCH",
                           f"VIN {read} does not match the VIN entered in the claim.",
                           IDENTITY_HINT,
                           read, inputs, attempt)
    return _unclear("POLICY_NOT_VERIFIED",
                    f"VIN {read} has a valid check digit, but there is no verified policy on file to match it against.",
                    read, inputs)


def _odometer(answer: dict, policy: dict | None, answers: dict, today, attempt: int) -> dict:
    reading = answer["reading"]
    miles = int(reading) if answer.get("unit") in ("mi", None) else round(reading * 0.621371)
    inputs = {"photo.odometer": miles}
    if policy is None:
        return _pass("No policy on file to check the mileage against.", miles, inputs)
    typed = answers.get("vin")
    key = typed.upper().strip() if isinstance(typed, str) else ""
    vehicles = policy.get("vehicles", [])
    vehicle = next((v for v in vehicles if isinstance(v.get("vin"), str) and key and v["vin"].upper() == key), None)
    vehicle = vehicle or (vehicles[0] if vehicles else None)
    if vehicle is None or today is None:
        return _pass("No policy vehicle to check the mileage against.", miles, inputs)
    year = vehicle["year"]
    inputs["policy.vehicle_year"] = year
    if miles > MILES_PER_YEAR_MAX * max(1, today.year - year + 1):
        return _finish("odometer", "retake", "ODOMETER_IMPLAUSIBLE",
                       f"Odometer {miles:,} miles is very high for a {year} vehicle.",
                       f"We read {miles:,} miles, which is very high for a {year} vehicle."
                       " Photograph the main odometer, not the trip meter.",
                       miles, inputs, attempt)
    return _pass(f"Odometer {miles:,} miles is plausible for a {year} vehicle.", miles, inputs)


def _license(answer: dict, policy: dict | None, answers: dict, today, attempt: int) -> dict:
    name, expires = answer["name"], answer["expires"]
    inputs = {"photo.name": name, "photo.expires": expires, "expected.name": None}
    holder = answers.get("driver_is_holder")
    if holder == "Yes":
        expected = (policy.get("holder") or {}).get("name") if policy is not None else answers.get("full_name")
    elif holder == "No":
        expected = answers.get("driver_name")
    else:
        expected = None
    expected = expected if isinstance(expected, str) and expected.strip() else None
    inputs["expected.name"] = expected
    if expected is not None and not _names_match(expected, name):
        return _finish("driver_license", "retake", "LICENSE_NAME_MISMATCH",
                       "The name on the license does not match the driver in the claim.",
                       IDENTITY_HINT,
                       name, inputs, attempt)
    loss_day = _parse_date(answers.get("loss_date")) or today
    expiry = _parse_date(expires)
    if expiry is not None and loss_day is not None and expiry < loss_day:
        return {"result": "flagged", "reason_code": "LICENSE_EXPIRED",
                "reason": f"The license expired on {expires}; the loss was on {loss_day}.",
                "hint": None, "value": name, "inputs": inputs}
    if expected is not None:
        return _pass("The license name matches the driver and the license is valid on the loss date.", name, inputs)
    return _pass("The license is valid on the loss date.", name, inputs)


def _resolved_reading(answer: dict) -> tuple[list[str], dict]:
    """One damage reading -> (resolved parts, inputs base). The model's words are
    kept in inputs["photo.parts_read"] when any part was corrected."""
    view = answer.get("viewpoint") if answer.get("viewpoint") in _viewpoint.VIEWPOINTS else "unknown"
    side = answer.get("image_side") if answer.get("image_side") in _viewpoint.IMAGE_SIDES else "unknown"
    raw = [p for p in answer["parts"] if p in AREAS]
    resolved: list[str] = []
    corrected = False
    for part in raw:
        fixed, was = _viewpoint.resolve(part, view, side)
        resolved.append(fixed)
        corrected = corrected or was
    base: dict = {"photo.viewpoint": view, "photo.image_side": side}
    if corrected:
        base["photo.parts_read"] = raw
    return resolved, base


def _second_usable(answer) -> bool:
    """A second damage reading the unclear rule may use: the right shape, on a
    vehicle, and not a guess. Anything else is ignored (the first decides)."""
    if not isinstance(answer, dict):
        return False
    if not _base_ok(answer):
        return False
    if type(answer.get("is_vehicle")) is not bool or type(answer.get("damage_visible")) is not bool:
        return False
    if not isinstance(answer.get("parts"), list) or any(not isinstance(p, str) for p in answer["parts"]):
        return False
    if not answer["is_vehicle"] or answer["unsure"]:
        return False
    return True


def _damage(slot_id: str, readings: list, answers: dict, attempt: int) -> dict:
    answer = readings[0]
    parts, base = _resolved_reading(answer)
    declared = answers.get("damaged_areas")
    declared = [d for d in declared if isinstance(d, str)] if isinstance(declared, list) else []
    inputs = {"photo.parts": parts, "form.damaged_areas": declared, **base}
    if slot_id == "damage_closeup":
        if not answer["damage_visible"]:
            return _finish(slot_id, "retake", "DAMAGE_NOT_VISIBLE",
                           "No damage is visible in the close-up photo.",
                           UNREADABLE_HINTS[slot_id], parts, inputs, attempt)
        if parts and declared and not any(_matches(p, set(declared)) for p in parts):
            if len(readings) > 1 and _second_usable(readings[1]):
                second_parts, _ = _resolved_reading(readings[1])
                if any(_matches(p, set(declared)) for p in second_parts):
                    return _unclear("DAMAGE_AREA_UNCLEAR",
                                    "Two readings of this photo disagree about which part is damaged;"
                                    " the adjuster will look.",
                                    parts, inputs)
            return _finish(slot_id, "retake", "DAMAGE_AREA_MISMATCH",
                           f"The photo shows {', '.join(parts)} but the claim lists {', '.join(declared)}.",
                           _area_hint(parts, declared), parts, inputs, attempt)
        return _pass("The damage in the photo matches the damaged areas in the claim.", parts, inputs)
    if not parts:
        return _finish(slot_id, "retake", "DAMAGE_PART_NOT_FOUND",
                       "No damaged vehicle part can be made out in the wide photo.",
                       UNREADABLE_HINTS[slot_id], [], inputs, attempt)
    if declared and not any(_matches(p, set(declared)) for p in parts):
        if len(readings) > 1 and _second_usable(readings[1]):
            second_parts, _ = _resolved_reading(readings[1])
            if any(_matches(p, set(declared)) for p in second_parts):
                return _unclear("DAMAGE_AREA_UNCLEAR",
                                "Two readings of this photo disagree about which part is damaged;"
                                " the adjuster will look.",
                                parts, inputs)
        return _finish(slot_id, "retake", "DAMAGE_AREA_MISMATCH",
                       f"The photo shows {', '.join(parts)} but the claim lists {', '.join(declared)}.",
                       _area_hint(parts, declared), parts, inputs, attempt)
    return _pass("The damage in the photo matches the damaged areas in the claim.", parts, inputs)


def _matches(part: str, declared: set[str]) -> bool:
    return part in declared or bool(ADJACENT.get(part, set()) & declared)


def _area_hint(parts: list[str], declared: list[str]) -> str:
    shown = ", ".join(p.lower() for p in parts)
    said = ", ".join(d.lower() for d in declared)
    return (f"This photo seems to show the {shown}, but you said the damage is on the {said}. "
            "Check the damaged areas in the form, or retake the photo.")


def _norm_plate(text: str) -> str:
    return "".join(c for c in text.upper() if "A" <= c <= "Z" or "0" <= c <= "9")


def _norm_vin(text: str) -> str:
    return text.upper().replace(" ", "").replace("-", "")


def _words(name: str) -> list[str]:
    return [w for w in re.findall(r"[a-z]+", name.lower()) if len(w) > 1]


def _names_match(expected: str, licence: str) -> bool:
    return set(_words(expected)) <= set(_words(licence or ""))
