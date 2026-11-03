"""Simulated policy administration system (PAS) and policy snapshots.

The insurer's PAS stays the system of record. At claim opening we copy the policy as it
stood into a snapshot; a policy we cannot find still opens a claim, marked unverified.
"""

import hashlib
import json
import re
import uuid
from datetime import date
from pathlib import Path

import psycopg

from .errors import CaptureError
from .loss_notice import vin_error

SEED_FILE = Path(__file__).parent / "seed" / "policies.json"

STATE_TZ = {
    "AZ": "America/Phoenix",
    "CA": "America/Los_Angeles",
    "NY": "America/New_York",
    "TX": "America/Chicago",
}


def canonical(data) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


US_STATES = frozenset({
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC",
})

TRIAL_COVERAGES = {
    "collision": {"deductible": 500},
    "comprehensive": {"deductible": 250},
    "liability": {"limits": "100/300/100"},
}


def seed(db: psycopg.Connection) -> str:
    """Load the demo org and policies once. Returns the org id."""
    data = json.loads(SEED_FILE.read_text())
    org = data["org"]
    db.execute(
        "INSERT INTO orgs (id, name, default_tz) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
        (org["id"], org["name"], org["default_tz"]),
    )
    for p in data["policies"]:
        db.execute(
            "INSERT INTO policies (org_id, policy_no, state, data_json) VALUES (%s, %s, %s, %s)"
            " ON CONFLICT DO NOTHING",
            (org["id"], p["policy_no"], p["state"], canonical(p)),
        )
    return org["id"]


def list_policies(db: psycopg.Connection, org_id: str) -> list[dict]:
    """What an adjuster needs to pick a policy; the full record stays in the PAS."""
    rows = db.execute(
        "SELECT data_json FROM policies WHERE org_id=%s ORDER BY policy_no", (org_id,)
    ).fetchall()
    result = []
    for row in rows:
        p = json.loads(row["data_json"])
        v = p["vehicles"][0]
        result.append(
            {
                "policy_no": p["policy_no"],
                "holder": p["holder"]["name"],
                "vehicle": f"{v['year']} {v['make']} {v['model']}",
                "state": p["state"],
                "expires": p["expires"],
            }
        )
    return result


def take_snapshot(
    db: psycopg.Connection, org_id: str, policy_no: str, provided: dict | None = None
) -> dict:
    """Freeze the policy as it stood at claim opening. Provided data (trial or
    insurer-sent) is frozen verified, with policy_no added so it has the seed
    snapshots' shape; nothing is ever written to policies."""
    if provided is not None:
        data_json = canonical({"policy_no": policy_no, **provided})
        snapshot_id = uuid.uuid4().hex
        db.execute(
            "INSERT INTO policy_snapshots (id, org_id, policy_no, verified, data_json, sha256)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (
                snapshot_id,
                org_id,
                policy_no,
                1,
                data_json,
                hashlib.sha256(data_json.encode()).hexdigest(),
            ),
        )
        return db.execute("SELECT * FROM policy_snapshots WHERE id=%s", (snapshot_id,)).fetchone()
    row = db.execute(
        "SELECT data_json FROM policies WHERE org_id=%s AND policy_no=%s", (org_id, policy_no)
    ).fetchone()
    data_json = row["data_json"] if row else None
    snapshot_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO policy_snapshots (id, org_id, policy_no, verified, data_json, sha256)"
        " VALUES (%s, %s, %s, %s, %s, %s)",
        (
            snapshot_id,
            org_id,
            policy_no,
            int(row is not None),
            data_json,
            hashlib.sha256((data_json or "").encode()).hexdigest(),
        ),
    )
    return db.execute("SELECT * FROM policy_snapshots WHERE id=%s", (snapshot_id,)).fetchone()


def snapshot_public(row: dict) -> dict:
    """The claim detail's policy shape, plus where it came from: on_file
    (a verified seed snapshot, which carries no source key), insurer, trial,
    or None when no policy was found."""
    data = json.loads(row["data_json"]) if row["data_json"] else None
    if not row["verified"] or data is None:
        source = None
    else:
        source = data.get("source") or "on_file"
    return {
        "id": row["id"],
        "policy_no": row["policy_no"],
        "verified": bool(row["verified"]),
        "data": data,
        "sha256": row["sha256"],
        "source": source,
    }


def validate_policy(data: dict, today: date, prefix: str) -> dict:
    """A normalised copy of insurer-sent or trial policy data (the seed shape
    without policy_no). Unknown keys are dropped, strings trimmed, empty
    optionals dropped; anything else raises CaptureError(f"{prefix}{path}: {reason}", 400)."""
    if not isinstance(data, dict):
        raise CaptureError(f"{prefix.rstrip('.') or 'policy'}: must be an object", 400)

    def fail(path: str, reason: str) -> None:
        raise CaptureError(f"{prefix}{path}: {reason}", 400)

    def clean_str(value, *, path: str, limit: int, required: bool = True) -> str | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            if required:
                fail(path, "is required")
            return None
        if not isinstance(value, str):
            fail(path, "must be a string")
        text = value.strip()
        if len(text) > limit:
            fail(path, f"must be {limit} characters or fewer")
        return text

    raw_state = data.get("state")
    state = raw_state.strip().upper() if isinstance(raw_state, str) else ""
    if not state:
        fail("state", "is required")
    if state not in US_STATES:
        fail("state", "must be a two-letter US state")

    def clean_day(key: str) -> date:
        raw = data.get(key)
        text = raw.strip() if isinstance(raw, str) else ""
        if not text:
            fail(key, "is required")
        try:
            return date.fromisoformat(text)
        except ValueError:
            fail(key, "must be a valid date (YYYY-MM-DD)")
        raise AssertionError("unreachable")

    effective = clean_day("effective")
    expires = clean_day("expires")
    if not effective < expires:
        fail("expires", "must be after effective")

    holder = data.get("holder")
    if not isinstance(holder, dict):
        fail("holder", "is required")
    holder_out = {"name": clean_str(holder.get("name"), path="holder.name", limit=80)}
    for key in ("phone", "email"):
        value = clean_str(holder.get(key), path=f"holder.{key}", limit=80, required=False)
        if value:
            holder_out[key] = value

    drivers_out: list[dict] = []
    raw_drivers = data.get("drivers")
    if raw_drivers is not None:
        if not isinstance(raw_drivers, list):
            fail("drivers", "must be a list of up to 10 drivers")
        if len(raw_drivers) > 10:
            fail("drivers", "must have 10 or fewer drivers")
        for i, entry in enumerate(raw_drivers):
            base = f"drivers[{i}]"
            if not isinstance(entry, dict):
                fail(base, "must be an object")
            driver = {"name": clean_str(entry.get("name"), path=f"{base}.name", limit=80)}
            for key in ("licence_no", "licence_state"):
                value = clean_str(entry.get(key), path=f"{base}.{key}", limit=80, required=False)
                if value:
                    driver[key] = value
            drivers_out.append(driver)

    raw_vehicles = data.get("vehicles")
    if not isinstance(raw_vehicles, list) or not raw_vehicles:
        fail("vehicles", "is required")
    if len(raw_vehicles) > 5:
        fail("vehicles", "must have 1-5 vehicles")
    top_year = today.year + 1
    vehicles_out: list[dict] = []
    for i, vehicle in enumerate(raw_vehicles):
        base = f"vehicles[{i}]"
        if not isinstance(vehicle, dict):
            fail(base, "must be an object")
        raw_vin = vehicle.get("vin")
        vin = raw_vin.strip().upper() if isinstance(raw_vin, str) else ""
        if not vin:
            fail(f"{base}.vin", "is required")
        reason = vin_error(vin)
        if reason is not None:
            fail(f"{base}.vin", reason)
        raw_plate = vehicle.get("plate")
        plate = (raw_plate.strip().upper() if isinstance(raw_plate, str) else "")
        plate = plate.replace(" ", "").replace("-", "")
        if not plate:
            fail(f"{base}.plate", "is required")
        if not re.fullmatch(r"[A-Z0-9]{1,8}", plate):
            fail(f"{base}.plate", "must be 1-8 letters or digits")
        year = vehicle.get("year")
        if year is None or (isinstance(year, str) and not year.strip()):
            fail(f"{base}.year", "is required")
        if isinstance(year, bool) or not isinstance(year, int) or not 1981 <= year <= top_year:
            fail(f"{base}.year", f"must be between 1981 and {top_year}")
        vehicles_out.append({
            "vin": vin,
            "plate": plate,
            "year": year,
            "make": clean_str(vehicle.get("make"), path=f"{base}.make", limit=30),
            "model": clean_str(vehicle.get("model"), path=f"{base}.model", limit=40),
        })

    raw_coverages = data.get("coverages")
    if not isinstance(raw_coverages, dict):
        fail("coverages", "is required")
    coverages_out: dict = {}
    for key in ("collision", "comprehensive"):
        if key not in raw_coverages:
            continue
        entry = raw_coverages[key]
        if not isinstance(entry, dict):
            fail(f"coverages.{key}", "must be an object")
        deductible = entry.get("deductible")
        if deductible is None:
            fail(f"coverages.{key}.deductible", "is required")
        if isinstance(deductible, bool) or not isinstance(deductible, int) or not 0 <= deductible <= 10000:
            fail(f"coverages.{key}.deductible", "must be between 0 and 10000")
        coverages_out[key] = {"deductible": deductible}
    if "liability" in raw_coverages:
        entry = raw_coverages["liability"]
        if not isinstance(entry, dict):
            fail("coverages.liability", "must be an object")
        coverages_out["liability"] = {
            "limits": clean_str(entry.get("limits"), path="coverages.liability.limits", limit=20)
        }
    if not coverages_out:
        fail("coverages", "is required")

    out = {
        "state": state,
        "effective": effective.isoformat(),
        "expires": expires.isoformat(),
        "holder": holder_out,
        "drivers": drivers_out,
        "vehicles": vehicles_out,
        "coverages": coverages_out,
    }
    lienholder = clean_str(data.get("lienholder"), path="lienholder", limit=80, required=False)
    if lienholder:
        out["lienholder"] = lienholder
    return out
