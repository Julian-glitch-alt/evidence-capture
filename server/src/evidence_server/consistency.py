"""Statement vs photos after submit (T21, tasks/t21-design.md): code compares, the model only reads.

Pure apart from save / load: no clock, no network. Results never accuse;
an `unclear` or `fail` only asks a person to look.
"""

import json
import uuid

import psycopg

from .photo_check import ADJACENT
from .photo_prompts import AREAS
from .policies import canonical
from . import consistency_prompts, damage

VERSION = "consistency/v2"
CHECK_ORDER = ("consistency.areas", "consistency.prior_damage", "consistency.story")
MAX_STATEMENT = 1000

_DETAIL_FIELDS: dict[str, list[str]] = {
    "hit_and_run": ["car_was"],
    "single_vehicle": ["hit_what"],
    "animal": ["animal"],
    "vandalism": ["vandalism_kind", "items_stolen"],
    "glass": ["glass_which", "glass_kind", "glass_cause"],
    "weather": ["weather_kind", "water_level"],
    "parts_theft": ["parts_taken"],
    "fire": ["fire_start", "fire_dept"],
    "vehicle_theft": ["last_seen_place", "last_seen_time", "keys_count", "keys_all",
                      "car_locked", "ownership", "lender", "items_in_car"],
    "collision_vehicle": [],
}


def _type_lines(answers: dict) -> list[str]:
    """Claim type and details lines for story_block; [] for the legacy types.

    other_damage behaves like vehicle_damage here: a generic damage claim with
    photos, where the block stays as it is for the old type."""
    from . import loss_notice

    loss_type = (answers or {}).get("loss_type")
    if loss_type in ("vehicle_damage", "something_else", "other_damage"):
        return []
    overlay = loss_notice.template()["overlays"].get(loss_type)
    if overlay is None:
        return []
    out = [f"Claim type: {overlay['label']}"]
    parts: list[str] = []
    for field in _DETAIL_FIELDS.get(loss_type, []):
        value = answers.get(field)
        if field == "fire_dept":
            if value == "Yes":
                parts.append("fire department came")
            elif value == "No":
                parts.append("fire department did not come")
            elif isinstance(value, str) and value.strip():
                parts.append(value.strip())
            continue
        if isinstance(value, list):
            parts.extend([v.strip() for v in value if isinstance(v, str) and v.strip()])
        elif isinstance(value, str) and value.strip():
            parts.append(value.strip())
    if parts:
        out.append(f"Details: {'; '.join(parts)}")
    return out


def usable(reading: dict) -> bool:
    """reading = damage.view(...) output; True when its status is "done" or "partial"."""
    return reading.get("status") in ("done", "partial")


def story_damages(reading: dict) -> list[dict]:
    """The damages the story prompt lists and cites by number: each physical damage once (damage.merge)."""
    return damage.merge(reading.get("damages") or [])


def story_block(answers: dict, reading: dict) -> str:
    """The claim block for consistency_prompts.story_prompt (format below).
    Damages are listed once each (story_damages), with every photo that shows them."""
    cause = answers.get("cause")
    other = answers.get("other_vehicle")
    prior = answers.get("prior_damage")
    declared = answers.get("damaged_areas")
    if isinstance(declared, list):
        declared_text = ", ".join(x for x in declared if isinstance(x, str)) or "none"
    else:
        declared_text = "none"
    raw = answers.get("description")
    statement = raw.strip()[:MAX_STATEMENT] if isinstance(raw, str) else ""
    lines = [
        f"Cause: {cause if isinstance(cause, str) and cause else 'not given'}",
        *_type_lines(answers),
        f"Another vehicle involved: {other if isinstance(other, str) and other else 'not given'}",
        f"Declared damaged areas: {declared_text}",
        f"Prior damage declared: {prior if isinstance(prior, str) and prior else 'not given'}",
        f"Statement: {statement or 'none'}",
        "Damage seen in the photos:",
    ]
    damages = story_damages(reading)
    if not damages:
        lines.append("none")
    for n, d in enumerate(damages, 1):
        words = str(d.get("damage_type", "")).lower().replace("_", " ")
        line = f"{n}. {d.get('area')}, {words}, {d.get('severity')}, {d.get('age')}"
        observed = d.get("observed")
        if isinstance(observed, str) and observed:
            line += f": {observed}"
        slots = list(dict.fromkeys(str(e.get("slot_id")) for e in d.get("evidence") or [d]))
        line += f" ({', '.join(slots)} photo{'s' if len(slots) > 1 else ''})"
        lines.append(line)
    return "\n".join(lines)


def shape_story(answer, n_damages: int) -> dict | None:
    """The model's answer -> {"consistent", "impact_from_story", "impact_from_damage", "reasons": [{"text", "damages": [int]}]}, or None."""
    if not isinstance(answer, dict):
        return None
    if answer.get("consistent") not in ("yes", "no", "unsure"):
        return None
    impacts = consistency_prompts.IMPACTS
    story_impact = answer.get("impact_from_story")
    damage_impact = answer.get("impact_from_damage")
    reasons = answer.get("reasons")
    if not isinstance(reasons, list):
        reasons = []
    kept = []
    for entry in reasons:
        if not isinstance(entry, dict):
            continue
        text = entry.get("text")
        if not isinstance(text, str):
            continue
        text = text.strip()[:200]
        if not text:
            continue
        cited = entry.get("damages")
        if not isinstance(cited, list):
            cited = []
        numbers = []
        for v in cited:
            if type(v) is not int or v < 1 or v > n_damages or v in numbers:
                continue
            numbers.append(v)
        kept.append({"text": text, "damages": numbers})
        if len(kept) >= 3:
            break
    return {
        "consistent": answer["consistent"],
        "impact_from_story": story_impact if story_impact in impacts else "unknown",
        "impact_from_damage": damage_impact if damage_impact in impacts else "unknown",
        "reasons": kept,
    }


def check(answers: dict, reading: dict, story: dict | None) -> list[dict]:
    """Three results in CHECK_ORDER, each shaped like coverage results:
    {"check_id", "result": "pass"|"unclear"|"fail", "reason_code", "reason", "value", "inputs"}."""
    damages = reading.get("damages") or []
    if not usable(reading):
        return [
            _result(check_id, "unclear", "DAMAGE_NOT_READ",
                     "The damage photos were not analysed, so the statement could not be compared with them.",
                     None, {"reading.status": reading.get("status")})
            for check_id in CHECK_ORDER
        ]
    return [_areas(answers, reading, damages), _prior(answers, damages), _story(answers, reading, damages, story)]


def save(conn: psycopg.Connection, claim_id: str, results: list[dict], at: str) -> None:
    """Upsert one `checks` row per result (id = uuid4 hex, source = VERSION, created_at = at).
    A resubmission after Send back (track 3) replaces the earlier round's row; the
    event log keeps every round's results."""
    for r in results:
        conn.execute(
            "INSERT INTO checks (id, claim_id, check_id, result, reason_code, reason,"
            " value_json, inputs_json, source, created_at)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
            " ON CONFLICT (claim_id, check_id) DO UPDATE SET result=EXCLUDED.result,"
            " reason_code=EXCLUDED.reason_code, reason=EXCLUDED.reason, value_json=EXCLUDED.value_json,"
            " inputs_json=EXCLUDED.inputs_json, source=EXCLUDED.source, created_at=EXCLUDED.created_at",
            (
                uuid.uuid4().hex,
                claim_id,
                r["check_id"],
                r["result"],
                r["reason_code"],
                r["reason"],
                canonical(r["value"]),
                canonical(r["inputs"]),
                VERSION,
                at,
            ),
        )


def load(conn: psycopg.Connection | None, claim_id: str, rows: list[dict] | None = None) -> list[dict]:
    """Rows for a claim in CHECK_ORDER, as dicts shaped like check() results plus source, created_at."""
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
    return [by_id[c] for c in CHECK_ORDER if c in by_id]


def _result(check_id: str, result: str, reason_code: str | None, reason: str, value, inputs: dict) -> dict:
    return {
        "check_id": check_id,
        "result": result,
        "reason_code": reason_code,
        "reason": reason,
        "value": value,
        "inputs": inputs,
    }


def _matches(area: str, others) -> bool:
    others = set(others)
    return area in others or bool(ADJACENT.get(area, set()) & others)


def _evidences(damages: list[dict]) -> list[dict]:
    """One {"item_id", "slot_id", "number"} per damage; number counts the photo's own damages."""
    counts: dict = {}
    out = []
    for d in damages:
        key = d.get("item_id")
        counts[key] = counts.get(key, 0) + 1
        out.append({"item_id": d.get("item_id"), "slot_id": d.get("slot_id"), "number": counts[key]})
    return out


def _areas(answers: dict, reading: dict, damages: list[dict]) -> dict:
    declared_raw = answers.get("damaged_areas")
    declared = [x for x in declared_raw if isinstance(x, str)] if isinstance(declared_raw, list) else []
    areas = reading.get("areas") or []
    undeclared = [a for a in AREAS if a in areas and not _matches(a, declared)]
    not_seen = [a for a in declared if not _matches(a, areas)]
    undeclared_set = set(undeclared)
    evidences = _evidences(damages)
    evidence = [
        evidences[i] for i, d in enumerate(damages)
        if d.get("age") in ("new", "unsure") and d.get("area") in undeclared_set
    ]
    value = {"undeclared": undeclared, "not_seen": not_seen, "evidence": evidence}
    inputs = {"form.damaged_areas": declared, "photo.areas": list(areas)}
    if undeclared:
        return _result(
            "consistency.areas", "unclear", "DAMAGE_OUTSIDE_STATEMENT",
            f"The photos show new damage on {', '.join(undeclared)}, which the claim does not list.",
            value, inputs,
        )
    reason = "Every damaged area in the photos is listed in the claim."
    if not_seen:
        verb = "are listed" if len(not_seen) > 1 else "is listed"
        reason += f" {', '.join(not_seen)} {verb} but not seen in the photos; they may not cover it."
    return _result("consistency.areas", "pass", None, reason, value, inputs)


def _prior(answers: dict, damages: list[dict]) -> dict:
    groups = [g for g in damage.merge(damages) if g["age"] == "old"]  # one per physical damage
    old = [e for g in groups for e in g["evidence"]]
    said = answers.get("prior_damage")
    value = {"old": old}
    inputs = {"form.prior_damage": said, "photo.old_damage": len(groups)}
    if not old:
        return _result("consistency.prior_damage", "pass", None, "No older damage seen in the photos.", value, inputs)
    if said == "No":
        n = len(groups)
        return _result(
            "consistency.prior_damage", "unclear", "PRIOR_DAMAGE_SUSPECTED",
            f"The photos suggest older damage ({n} item{'s' if n > 1 else ''}),"
            " but the claimant said none of the damage was there before.",
            value, inputs,
        )
    if said in ("Yes", "Not sure"):
        return _result(
            "consistency.prior_damage", "pass", None,
            "The claimant said some damage may be older; the photos agree.", value, inputs,
        )
    return _result(
        "consistency.prior_damage", "unclear", "PRIOR_DAMAGE_SUSPECTED",
        "The photos suggest older damage, and the claimant did not say whether any damage was there before.",
        value, inputs,
    )


def _story(answers: dict, reading: dict, damages: list[dict], story: dict | None) -> dict:
    raw_cause = answers.get("cause")
    raw_description = answers.get("description")
    inputs = {
        "form.cause": raw_cause or None,
        "form.statement_chars": len(raw_description.strip()) if isinstance(raw_description, str) else 0,
        "reading.damages": len(story_damages(reading)),
    }
    if story is None:
        return _result(
            "consistency.story", "unclear", "STORY_NOT_CHECKED",
            "The statement could not be compared with the damage automatically.", None, inputs,
        )
    listed = story_damages(reading)  # the model cites these numbers
    value = {
        "consistent": story["consistent"],
        "impact_from_story": story["impact_from_story"],
        "impact_from_damage": story["impact_from_damage"],
        "reasons": [
            {"text": r["text"], "evidence": [e for n in r["damages"] if n <= len(listed) for e in listed[n - 1]["evidence"]]}
            for r in story["reasons"]
        ],
    }
    if story["consistent"] == "yes":
        return _result("consistency.story", "pass", None, "The statement fits the damage in the photos.", value, inputs)
    if story["consistent"] == "no":
        return _result(
            "consistency.story", "fail", "STORY_INCONSISTENT",
            "The statement does not fit the damage in the photos.", value, inputs,
        )
    return _result(
        "consistency.story", "unclear", "STORY_UNCLEAR",
        "The statement could not be matched with confidence to the damage.", value, inputs,
    )
