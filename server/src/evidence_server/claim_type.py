"""Claim type fit vs the damage reading (T43b, tasks/t43b-design.md): code compares, the model only reads.

Pure: no database, no clock, no network. The damage reading stays neutral (it is
never told the claim type). A row never rejects; a `fail` only sends it to review.
"""

import json
import uuid

import psycopg

from . import damage
from .policies import canonical

VERSION = "claimtype/v1"
CHECK_ID = "claimtype.fit"

REAR_AREAS = {"Rear bumper", "Rear left", "Rear right", "Trunk or tailgate", "Rear window"}


def _words(dtype) -> str:
    return str(dtype or "").lower().replace("_", " ")


def fit(answers: dict, reading: dict) -> dict | None:
    """Compare the merged damages with what the claim type implies.

    -> one check row shaped like the coverage rows, or None when the type has no rule
    or the damage reading has nothing read yet (status other than done/partial).
    """
    if not isinstance(answers, dict) or not isinstance(reading, dict):
        return None
    loss_type = answers.get("loss_type")
    if reading.get("status") not in ("done", "partial"):
        return None
    merged = damage.merge(reading.get("damages") or [])
    considered = [d for d in merged if d.get("age") != "old"]
    types = sorted({d.get("damage_type") for d in considered if isinstance(d.get("damage_type"), str)})
    value = list(types)
    numbers = list(range(1, len(considered) + 1))
    base_inputs: dict = {"form.loss_type": loss_type, "reading.damages": numbers}
    if loss_type == "glass":
        if not any(d.get("damage_type") == "GLASS_DAMAGE" for d in merged):
            return _row("fail", "TYPE_DAMAGE_MISMATCH", "No glass damage is visible in the photos.",
                        value, base_inputs)
        rest = sorted({t for t in types if t != "GLASS_DAMAGE"})
        if rest:
            words = ", ".join(_words(t) for t in rest)
            return _row("fail", "TYPE_DAMAGE_MISMATCH",
                        f"Damage beyond the glass is visible: {words}.", value, base_inputs)
        return _row("pass", None, "Only glass damage is visible in the photos.", value, base_inputs)
    if loss_type == "weather":
        kind = answers.get("weather_kind")
        if kind == "Hail":
            inputs = {**base_inputs, "form.weather_kind": kind}
            if any(d.get("damage_type") == "HAIL_DENTS" for d in merged):
                return _row("pass", None, "Hail dents are visible in the photos.", value, inputs)
            return _row("fail", "TYPE_DAMAGE_MISMATCH", "No hail dents are visible in the photos.",
                        value, inputs)
        if kind == "Flood or high water":
            inputs = {**base_inputs, "form.weather_kind": kind}
            if any(d.get("damage_type") in ("WATER_DAMAGE", "INTERIOR_DAMAGE") for d in merged):
                return _row("pass", None, "Water damage is visible in the photos.", value, inputs)
            return _row("fail", "TYPE_DAMAGE_MISMATCH", "No water damage is visible in the photos.",
                        value, inputs)
        return None
    if loss_type == "fire":
        if any(d.get("damage_type") == "FIRE_DAMAGE" for d in merged):
            return _row("pass", None, "Fire damage is visible in the photos.", value, base_inputs)
        return _row("fail", "TYPE_DAMAGE_MISMATCH", "No fire damage is visible in the photos.",
                    value, base_inputs)
    if loss_type == "parts_theft":
        if any(d.get("damage_type") == "UNDERCARRIAGE_DAMAGE" or d.get("area") == "Underneath"
               for d in merged):
            return _row("pass", None, "Damage where the part was taken is visible.", value, base_inputs)
        return _row("unclear", "TYPE_DAMAGE_NOT_SEEN", "The photos do not show where the part was taken.",
                    value, base_inputs)
    if loss_type == "animal":
        if any(d.get("area") not in REAR_AREAS for d in merged):
            return _row("pass", None, "Damage outside the rear is visible in the photos.", value, base_inputs)
        return _row("fail", "TYPE_DAMAGE_MISMATCH",
                    "All damage is at the rear; animal strikes are usually at the front or sides.",
                    value, base_inputs)
    return None


def _row(result: str, reason_code: str | None, reason: str, value, inputs: dict) -> dict:
    return {
        "check_id": CHECK_ID,
        "result": result,
        "reason_code": reason_code,
        "reason": reason,
        "value": value,
        "inputs": inputs,
    }


def save(conn: psycopg.Connection, claim_id: str, row: dict, at: str) -> None:
    """Upsert the fit row (ON CONFLICT (claim_id, check_id) DO UPDATE)."""
    conn.execute(
        "INSERT INTO checks (id, claim_id, check_id, result, reason_code, reason,"
        " value_json, inputs_json, source, created_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
        " ON CONFLICT (claim_id, check_id) DO UPDATE SET result=EXCLUDED.result,"
        " reason_code=EXCLUDED.reason_code, reason=EXCLUDED.reason, value_json=EXCLUDED.value_json,"
        " inputs_json=EXCLUDED.inputs_json, source=EXCLUDED.source, created_at=EXCLUDED.created_at",
        (
            uuid.uuid4().hex, claim_id, row["check_id"], row["result"], row["reason_code"],
            row["reason"], canonical(row["value"]), canonical(row["inputs"]), VERSION, at,
        ),
    )


def clear(conn: psycopg.Connection, claim_id: str) -> None:
    """Remove a stale fit row when the type has no rule or nothing was read."""
    conn.execute("DELETE FROM checks WHERE claim_id=%s AND check_id=%s", (claim_id, CHECK_ID))


def load(conn: psycopg.Connection | None, claim_id: str, rows: list[dict] | None = None) -> list[dict]:
    """The fit row for a claim, shaped like coverage.load()."""
    if rows is None:
        rows = conn.execute("SELECT * FROM checks WHERE claim_id=%s", (claim_id,)).fetchall()
    out = []
    for row in rows:
        if row["check_id"] != CHECK_ID:
            continue
        out.append({
            "check_id": row["check_id"],
            "result": row["result"],
            "reason_code": row["reason_code"],
            "reason": row["reason"],
            "value": json.loads(row["value_json"]),
            "inputs": json.loads(row["inputs_json"]),
            "source": row["source"],
            "created_at": row["created_at"],
        })
    return out
