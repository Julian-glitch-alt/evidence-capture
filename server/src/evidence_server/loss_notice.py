"""The claimant's loss notice: one base template plus one overlay per loss type.

Only the server evaluates `show_if` / `required_if`. Every draft save returns the
layout (visible sections, fields, slots, what is still missing), so the phone and
the server cannot disagree about what is required.
"""

import json
import os
from datetime import date
from functools import cache
from pathlib import Path

from .errors import CaptureError

TEMPLATE_DIR = Path(__file__).parent / "templates" / "loss_notice"
TEMPLATE_ID = "loss_notice"
MAX_TEXT = 2000
# T59: a choice is a suggestion. A field with `"other": "<option>"` names the
# option that opens a free-text answer, stored beside the field as `<id>_other`.
MAX_OTHER = 200
# Track 4 proposal, off by default: a field's `proposed_options` (on `cause`: "Hit an animal",
# "Vandalism", both comprehensive losses) are offered only when this is "on".
PROPOSED_OPTIONS_ENV = "LOSS_NOTICE_COMPREHENSIVE_CAUSES"

VIN_VALUES = {
    **{str(d): d for d in range(10)},
    **dict(zip("ABCDEFGH", range(1, 9))),
    **dict(zip("JKLMN", range(1, 6))),
    "P": 7,
    "R": 9,
    **dict(zip("STUVWXYZ", range(2, 10))),
}
VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]


@cache
def template() -> dict:
    base = json.loads((TEMPLATE_DIR / "base.json").read_text())
    overlays = {}
    for path in sorted(TEMPLATE_DIR.glob("*.json")):
        if path.stem != "base":
            overlay = json.loads(path.read_text())
            missing = [_section_id(s) for s in overlay["sections"] if _section_id(s) not in base["sections"]]
            if missing:
                raise ValueError(f"{path.name} names unknown sections: {missing}")
            overlays[overlay["id"]] = overlay
    # Loss types in a fixed, deliberate order: the ten photo-lane types first,
    # then the catch-all with photos, then something else. vehicle_damage stays
    # for old drafts but is hidden.
    order = ["collision_vehicle", "hit_and_run", "single_vehicle", "animal", "vandalism",
             "glass", "weather", "parts_theft", "fire", "vehicle_theft", "other_damage", "something_else"]
    ordered = {k: overlays[k] for k in order if k in overlays}
    ordered.update({k: v for k, v in sorted(overlays.items()) if k not in ordered})
    return base | {"overlays": ordered}


def _section_id(entry) -> str:
    return entry if isinstance(entry, str) else entry.get("id")


def proposed_options_on() -> bool:
    return os.environ.get(PROPOSED_OPTIONS_ENV) == "on"


def options(field: dict) -> list[str]:
    """A choice field's options, plus its proposed ones while the option is switched on."""
    extra = field.get("proposed_options", []) if proposed_options_on() else []
    return [*field.get("options", []), *extra]


def all_slots() -> dict[str, dict]:
    return {slot["id"]: slot for section in template()["sections"].values() for slot in section["slots"]}


def vin_error(vin: str) -> str | None:
    vin = vin.strip().upper()
    if len(vin) != 17:
        return "A VIN has 17 characters."
    if any(c not in VIN_VALUES for c in vin):
        return "A VIN never contains the letters I, O or Q."
    remainder = sum(VIN_VALUES[c] * w for c, w in zip(vin, VIN_WEIGHTS)) % 11
    if vin[8] != ("X" if remainder == 10 else str(remainder)):
        return "This VIN is not valid. Check each character."
    return None


def date_error(value: str, latest_date: date | None) -> str | None:
    """A loss date must be a real calendar day, and not in the future."""
    try:
        day = date.fromisoformat(value)
    except (TypeError, ValueError):
        return "Enter a valid date."
    if latest_date is not None and day > latest_date:
        return "The date of the loss cannot be in the future."
    return None


def clean_answers(raw: dict) -> dict:
    """Keep only known fields, with values of the right shape. Used for drafts and submit."""
    if not isinstance(raw, dict):
        raise CaptureError("Answers must be an object")
    t = template()
    loss_type = raw.get("loss_type")
    if loss_type is not None and loss_type not in t["overlays"]:
        raise CaptureError("Unknown loss type")
    fields = {f["id"]: f for s in t["sections"].values() for f in s["fields"]}
    clean: dict = {"loss_type": loss_type} if loss_type else {}
    skips = _clean_skips(raw.get("slot_skips"), t["skip_reasons"])
    if skips:
        clean["slot_skips"] = skips
    for key, value in raw.items():
        field = fields.get(key)
        if not field:
            continue
        kind = field["type"]
        if kind == "checkbox":
            clean[key] = value is True
        elif kind == "multiselect":
            clean[key] = [v for v in value if v in field["options"]] if isinstance(value, list) else []
        elif kind == "choice":
            if value in options(field):
                clean[key] = value
        elif isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()[:MAX_TEXT]
            clean[key] = text.upper() if kind == "vin" else text
    for key, value in raw.items():
        if not key.endswith("_other"):
            continue
        field = fields.get(key[: -len("_other")])
        if not field or not _escape_selected(field, clean.get(field["id"])):
            continue
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()[:MAX_OTHER]
            if text:
                clean[key] = text
    return clean


def _escape_selected(field: dict, value) -> bool:
    """Whether the field's escape option (its `other`) is chosen: the value
    equals it (choice) or holds it (multiselect)."""
    escape = field.get("other")
    if not escape:
        return False
    if isinstance(value, list):
        return escape in value
    return value == escape


def layout(answers: dict, policy: dict | None, slot_counts: dict[str, int], latest_date: date | None = None) -> dict:
    t = template()
    overlay = t["overlays"].get(answers.get("loss_type"))
    loss_types = [{"id": o["id"], "label": o["label"], "description": o["description"], "group": o.get("group")}
                  for o in t["overlays"].values() if not o.get("hidden")]
    shared = {
        "loss_types": loss_types,
        "policy": policy and {k: v for k, v in policy.items() if k != "vins"},
        "skip_reasons": t["skip_reasons"],
        "screen_titles": t["screen_titles"],
        "screen_order": t["screen_order"],
    }
    if not overlay:
        return shared | {"loss_type": None, "sections": [], "complete": False, "missing": ["What happened"]}
    skips = answers.get("slot_skips", {})
    section_copy = overlay.get("section_copy") or {}
    slot_copy = overlay.get("slot_copy") or {}

    sections = []
    shown_sections = [s for s in overlay["sections"] if _section_shown(s, answers)]
    for number, section_id in enumerate(shown_sections):
        spec = t["sections"][_section_id(section_id)]
        fields, missing = [], []
        for f in spec["fields"]:
            if "show_if" in f and not _matches(f["show_if"], answers):
                continue
            required = bool(f.get("required")) or ("required_if" in f and _matches(f["required_if"], answers))
            value = answers.get(f["id"])
            prefilled = False
            if f.get("prefill") == "policy_vin":
                vin = _policy_vin(policy)
                if vin:
                    if _empty(value):
                        value, prefilled = vin, True
                    elif value == vin:
                        prefilled = True
            elif f.get("prefill") in POLICY_PREFILLS:
                candidate = POLICY_PREFILLS[f["prefill"]][0](policy)
                if candidate:
                    if _empty(value):
                        value, prefilled = candidate, True
                    elif value == candidate:
                        prefilled = True
            error, note = None, None
            if prefilled:
                if f.get("prefill") == "policy_vin":
                    note = "From your policy. Change it only if this is a different car."
                else:
                    note = POLICY_PREFILLS[f["prefill"]][1]
            elif f["type"] == "vin" and value:
                error = vin_error(value)
                if not error and policy and policy.get("verified"):
                    note = "Matches your policy" if value in policy["vins"] else "Does not match the VIN on your policy"
            elif f["type"] == "date" and value:
                error = date_error(value, latest_date)
            other_text = None
            if "other" in f:
                words = answers.get(f["id"] + "_other")
                if isinstance(words, str) and words.strip():
                    other_text = words.strip()
            if (required and _empty(value)) or error:
                missing.append(f["label"])
            if "other" in f and _escape_selected(f, value) and not other_text:
                if f["label"] not in missing:
                    missing.append(f["label"])
            shown = {k: v for k, v in f.items() if k not in ("proposed_options", "_proposed")}
            if "options" in f:
                shown["options"] = options(f)
            fields.append({**shown, "required": required, "value": value, "error": error, "note": note}
                          | ({"other_text": other_text} if "other" in f else {})
                          | ({"prefilled": True} if prefilled else {}))
        slots = []
        for s in spec["slots"]:
            if "show_if" in s and not _matches(s["show_if"], answers):
                continue
            required = _matches(s["required_if"], answers) if "required_if" in s else bool(s.get("required"))
            count = slot_counts.get(s["id"], 0)
            skipped = skips.get(s["id"]) if count == 0 else None
            title = slot_copy.get(s["id"], {}).get("title", s["title"])
            if required and count == 0 and not skipped:
                missing.append(title)
            slots.append(
                {**s, "title": title,
                 "instructions": slot_copy.get(s["id"], {}).get("instructions", s["instructions"]),
                 "required": required, "skippable": s.get("skippable", True),
                 "count": count, "skipped": skipped}
            )
        copy = section_copy.get(_section_id(section_id), {})
        out = {
            "id": _section_id(section_id),
            "number": number,
            "title": copy.get("title", spec["title"]),
            "intro": copy.get("intro", spec.get("intro")),
            "fields": fields,
            "slots": slots,
            "missing": missing,
            "complete": not missing,
        }
        if section_id == "policy":
            out["policy"] = policy and {k: v for k, v in policy.items() if k != "vins"}
        if section_id == "declaration":
            warnings = t["fraud_warnings"]
            out["notice"] = warnings.get((policy or {}).get("state"), warnings["default"])
        sections.append(out)
    missing = [m for s in sections for m in s["missing"]]
    return shared | {
        "loss_type": overlay["id"],
        "sections": sections,
        "complete": not missing,
        "missing": missing,
        "quality": t["quality"],
    }


def outcome(answers: dict, layout_: dict, policy_verified: bool, vin_on_policy: bool) -> dict:
    """What submit records: reason codes for a person to look at, and reported facts."""
    overlay = template()["overlays"][answers["loss_type"]]
    codes = list(overlay["reason_codes"])
    codes += [flag["code"] for flag in overlay["flags"] if _matches(flag["if"], answers)]
    if not policy_verified:
        codes.append("POLICY_NOT_VERIFIED")
    elif answers.get("vin") and not vin_on_policy:
        codes.append("VIN_MISMATCH")

    missing_information = [
        f"{slot['title']}: {slot['skipped']['reason']}"
        for section in layout_["sections"]
        for slot in section["slots"]
        if slot["required"] and slot["skipped"]
    ]
    if missing_information:
        codes.append("REQUIRED_EVIDENCE_MISSING")
    shown = {f["id"]: f["value"] for section in layout_["sections"] for f in section["fields"]}
    if _police_report_needed_for(answers.get("loss_type"), shown):
        codes.append("POLICE_REPORT_NEEDED")
    if answers.get("loss_type") == "weather" and shown.get("water_level") in ("Up to the seats", "Up to the dashboard or higher"):
        codes.append("POSSIBLE_TOTAL_LOSS")

    facts = {}
    for section in layout_["sections"]:
        for f in section["fields"]:
            if f.get("fact") and not _empty(f["value"]):
                facts[f["fact"]] = (f["value"], [f"form:{section['id']}.{f['id']}"])
    rule = overlay["loss_event_type"]
    by_cause = rule.get("by_field", {})
    if by_cause and shown.get(by_cause["field"]) in by_cause["map"]:
        event_type = by_cause["map"][shown[by_cause["field"]]]
        facts["loss_event_type"] = (event_type, [f"form:{by_cause['field']}"])
    elif "from_field" in rule:
        event_type = rule["map"].get(answers.get(rule["from_field"]), "UNKNOWN")
        facts["loss_event_type"] = (event_type, [f"form:{rule['from_field']}"])
    else:
        event_type = rule["then"] if "if" in rule and _matches(rule["if"], answers) else rule["default"]
        facts["loss_event_type"] = (event_type, [f"form:loss_type={overlay['id']}"])
    return {"reason_codes": codes, "facts": facts, "missing_information": missing_information}


def police_report_needed(shown: dict) -> bool:
    """Carriers usually make a police report a condition of a hit-and-run (UMPD) or vandalism
    claim (docs/research/claim-documents-1-nov.md 1.4, 1.7). Without a report number a person
    reviews the claim; the claimant can still submit. shown = the answers the layout shows."""
    hit_and_run = shown.get("other_driver_left") == "Yes"
    return (hit_and_run or shown.get("cause") == "Vandalism") and _empty(shown.get("police_report_no"))


def _police_report_needed_for(loss_type: str | None, shown: dict) -> bool:
    """Hit and run, vandalism, parts theft and theft need a report number whatever the
    contact answer says; vehicle_damage keeps today's rule."""
    if loss_type in ("hit_and_run", "vandalism", "parts_theft", "vehicle_theft"):
        return _empty(shown.get("police_report_no"))
    return police_report_needed(shown)


def effective(answers: dict, policy: dict | None = None) -> dict:
    """The answers with this type's implied values set (they win over anything typed),
    keys implied by another type but neither implied here nor shown now removed, and
    the policy VIN stored (a typed value always wins). Idempotent."""
    overlay = template()["overlays"].get((answers or {}).get("loss_type"))
    if not overlay:
        return dict(answers)
    out = dict(answers)
    implied = overlay.get("implied") or {}
    for key, value in implied.items():
        out[key] = value
    other_implied = set()
    for o in template()["overlays"].values():
        other_implied.update((o.get("implied") or {}))
    for key in other_implied - set(implied) - _visible_field_ids(overlay, out):
        out.pop(key, None)
    # The stolen car is the insured car, and a VIN field left empty is the policy
    # car too: one verified vehicle, nothing typed. something_else names no
    # coverage and keeps whatever the claimant typed (or nothing).
    if _empty(out.get("vin")) and (overlay.get("coverage") is not None or "vin" in _visible_field_ids(overlay, out)):
        vin = _policy_vin(policy)
        if vin:
            out["vin"] = vin
    # T59b: the claimant's own details from the policy, where this type shows
    # the field: an untouched answer is stored as the policy's value (a typed
    # value always wins). Fields this type never shows stay empty. Idempotent.
    visible = _visible_field_ids(overlay, out)
    for kind, field_id in POLICY_PREFILL_FIELDS.items():
        if field_id in visible and _empty(out.get(field_id)):
            candidate = POLICY_PREFILLS[kind][0](policy)
            if candidate:
                out[field_id] = candidate
    return out


def shows_section(answers: dict, section_id: str) -> bool:
    """Whether the claimant's layout shows this section now."""
    overlay = template()["overlays"].get((answers or {}).get("loss_type"))
    if not overlay:
        return False
    return any(_section_id(s) == section_id and _section_shown(s, answers) for s in overlay["sections"])


def _section_shown(entry, answers: dict) -> bool:
    return not isinstance(entry, dict) or "show_if" not in entry or _matches(entry["show_if"], answers)


def _visible_field_ids(overlay: dict, answers: dict) -> set[str]:
    """Field ids the layout shows for these answers (section and field conditions)."""
    t = template()
    out = set()
    for entry in overlay["sections"]:
        if not _section_shown(entry, answers):
            continue
        for f in t["sections"][_section_id(entry)]["fields"]:
            if "show_if" not in f or _matches(f["show_if"], answers):
                out.add(f["id"])
    return out


def _policy_vin(policy: dict | None) -> str | None:
    """The single verified policy vehicle's VIN, else None (typed values win, and
    with no or several vehicles there is nothing unambiguous to prefill)."""
    vins = (policy or {}).get("vins") or []
    if (policy or {}).get("verified") and len(vins) == 1 and isinstance(vins[0], str):
        return vins[0]
    return None


def _nonblank(value) -> str | None:
    """A string with something in it, stripped, else None."""
    return (value.strip() or None) if isinstance(value, str) else None


def _policy_state(policy: dict | None) -> str | None:
    """The policy's state (2 letters), else None (no policy, or nothing on file)."""
    return _nonblank((policy or {}).get("state"))


def _policy_holder_name(policy: dict | None) -> str | None:
    """The policyholder's name. The store's policy view carries it as `holder`
    (a plain string) with `holder_name` beside it; a `holder` mapping with
    `name` reads the same way. Else None."""
    p = policy or {}
    holder = p.get("holder")
    if isinstance(holder, dict):
        return _nonblank(holder.get("name"))
    if isinstance(holder, str):
        return _nonblank(holder)
    return _nonblank(p.get("holder_name"))


def _policy_holder_phone(policy: dict | None) -> str | None:
    """The policyholder's phone, same shapes as the name. Else None."""
    p = policy or {}
    holder = p.get("holder")
    if isinstance(holder, dict):
        return _nonblank(holder.get("phone"))
    return _nonblank(p.get("holder_phone"))


# T59b: prefill kind -> (policy value, note shown under the field). The VIN
# keeps its own branch above; these three behave exactly like it.
POLICY_PREFILLS = {
    "policy_state": (_policy_state, "From your policy. Change it if this happened in another state."),
    "policy_holder_phone": (_policy_holder_phone, "The number we have for you. Change it if another number is better."),
    "policy_holder_name": (_policy_holder_name, "From your policy. Change it if someone else is signing."),
}

# T59b: prefill kind -> field id, for storing an untouched value with the answers.
POLICY_PREFILL_FIELDS = {
    "policy_state": "loss_state",
    "policy_holder_phone": "callback_phone",
    "policy_holder_name": "full_name",
}


def visible_answers(answers: dict) -> dict:
    """The answers whose fields the claimant's loss type shows now; a value typed before
    a later answer hid its field stays in the draft but counts for nothing."""
    overlay = template()["overlays"].get(answers.get("loss_type"))
    if not overlay:
        return {}
    out = {}
    for entry in overlay["sections"]:
        if not _section_shown(entry, answers):
            continue
        for f in template()["sections"][_section_id(entry)]["fields"]:
            if ("show_if" not in f or _matches(f["show_if"], answers)) and not _empty(answers.get(f["id"])):
                out[f["id"]] = answers[f["id"]]
    return out


def to_verify(answers: dict) -> list[str]:
    """Claimant-reported items an adjuster confirms with the issuer; we never claim we did.
    One line each, shared by the report PDF and the console."""
    shown = visible_answers(answers)
    out: list[str] = []
    if shown.get("police_contacted") == "Yes":
        report_no = str(shown.get("police_report_no") or "").strip()
        agency = str(shown.get("police_agency") or "").strip()
        officer = str(shown.get("police_officer") or "").strip()
        head = f"Police report {report_no}" if report_no else "Police contacted, no report number given"
        parts = [head, *([agency] if agency else []), *([f"officer {officer}"] if officer else [])]
        out.append(", ".join(parts) + ": confirm with the department")
    raw = answers if isinstance(answers, dict) else {}
    loss_type = raw.get("loss_type")
    if loss_type == "weather":
        kind = str(raw.get("weather_kind") or "").strip()
        city = str(raw.get("loss_city") or shown.get("loss_city") or "").strip()
        state = str(raw.get("loss_state") or shown.get("loss_state") or "").strip()
        day = str(raw.get("loss_date") or shown.get("loss_date") or "").strip()
        if kind and city and state and day:
            out.append(f"NOAA Storm Events: {kind.lower()} near {city}, {state} on {day}: confirm the event")
    if loss_type == "fire" and raw.get("fire_dept") == "Yes":
        out.append("Fire department report: confirm with the department")
    if loss_type == "vehicle_theft":
        vin = str(raw.get("vin") or "").strip()
        if vin:
            out.append(f"NICB VINCheck for VIN {vin}: confirm no earlier theft or salvage record")
    return out


def _clean_skips(raw, reasons: list[str]) -> dict:
    """A claimant who cannot take a shot says why; the claim goes on, flagged for a person."""
    if not isinstance(raw, dict):
        return {}
    slots = all_slots()
    clean = {}
    for slot_id, skip in raw.items():
        slot = slots.get(slot_id)
        if not slot or not slot.get("skippable", True) or not isinstance(skip, dict):
            continue
        if skip.get("reason") in reasons:
            note = str(skip.get("note") or "").strip()[:300]
            clean[slot_id] = {"reason": skip["reason"]} | ({"note": note} if note else {})
    return clean


def _matches(condition: dict, answers: dict) -> bool:
    """{"field", "in"} | {"field", "not_in"} | {"field", "contains"} (a multiselect
    answer holding the value) | {"all": [condition, ...]}."""
    if "all" in condition:
        return all(_matches(c, answers) for c in condition["all"])
    if "not_in" in condition:
        return answers.get(condition["field"]) not in condition["not_in"]
    if "contains" in condition:
        value = answers.get(condition["field"])
        return condition["contains"] in value if isinstance(value, list) else value == condition["contains"]
    return answers.get(condition["field"]) in condition["in"]


def _empty(value) -> bool:
    return value is None or value == "" or value == [] or value is False
