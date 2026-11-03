"""Rule-based coverage check: the policy snapshot against the claimant's answers.

Pure: no database, no clock, no network. A result never rejects a claim;
a `fail` only sends it to an adjuster later.
"""

import json
import uuid
from datetime import date

import psycopg

from . import loss_notice
from .policies import canonical

VERSION = "coverage/v1"
CHECK_ORDER = (
    "coverage.in_force",
    "coverage.vehicle_listed",
    "coverage.driver_listed",
    "coverage.coverage_bought",
    "coverage.deductible",
)

# Losses under "something else" that map to comprehensive coverage.
COMPREHENSIVE_LOSSES = {"Theft", "Fire", "Flood or weather"}
# Photo-lane causes that a US policy covers under comprehensive, not collision. The phone
# offers them only behind loss_notice.PROPOSED_OPTIONS_ENV (track 4, Julian decides).
COMPREHENSIVE_CAUSES = {"Hit an animal", "Vandalism"}


def check(policy: dict | None, verified: bool, answers: dict) -> list[dict]:
    """policy = the snapshot's parsed data_json (None when not found); answers = the submitted form."""
    if policy is None or not verified:
        return [
            _result(check_id, "unclear", "POLICY_NOT_VERIFIED", "No policy on file to check against.", None, {})
            for check_id in CHECK_ORDER
        ]
    bought = _coverage_bought(policy, answers)
    rows = [
        _in_force(policy, answers),
        _vehicle_listed(policy, answers),
    ]
    # No driver was driving (parked hit and run, vandalism, glass, weather, parts
    # theft, fire, theft): not applicable, not a pass and not missing.
    driver = _driver_listed(policy, answers)
    if driver is not None:
        rows.append(driver)
    return rows + [
        bought,
        _deductible(policy, bought),
    ]


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


def _in_force(policy: dict, answers: dict) -> dict:
    raw = answers.get("loss_date")
    inputs = {
        "form.loss_date": raw,
        "policy.effective": policy["effective"],
        "policy.expires": policy["expires"],
    }
    try:
        day = date.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        day = None
    if day is None:
        return _result(
            "coverage.in_force", "unclear", "LOSS_DATE_NOT_PROVIDED",
            "No usable loss date to check against the policy period.", None, inputs,
        )
    start, end = date.fromisoformat(policy["effective"]), date.fromisoformat(policy["expires"])
    if start <= day < end:
        return _result(
            "coverage.in_force", "pass", None,
            f"Loss date {raw} falls within the policy period.", None, inputs,
        )
    return _result(
        "coverage.in_force", "fail", "POLICY_NOT_IN_FORCE",
        f"Loss date {raw} falls outside the policy period.", None, inputs,
    )


def _vehicle_listed(policy: dict, answers: dict) -> dict:
    raw = answers.get("vin")
    vin = raw.strip().upper() if isinstance(raw, str) else ""
    listed = sorted({v["vin"].upper() for v in policy["vehicles"]})
    inputs = {"form.vin": vin, "policy.vehicles": listed}
    if not vin:
        return _result(
            "coverage.vehicle_listed", "unclear", "VIN_NOT_PROVIDED",
            "No VIN provided to check against the policy.", None, inputs,
        )
    if vin in listed:
        return _result(
            "coverage.vehicle_listed", "pass", None,
            f"VIN {vin} is listed on the policy.", None, inputs,
        )
    return _result(
        "coverage.vehicle_listed", "fail", "VIN_MISMATCH",
        f"VIN {vin} is not listed on the policy.", None, inputs,
    )


def _collapse(name: str) -> str:
    return " ".join(name.split()).casefold()


def _driver_listed(policy: dict, answers: dict) -> dict | None:
    if _no_driver_section(answers):
        return None
    is_holder = answers.get("driver_is_holder")
    holder = policy["holder"]["name"]
    drivers = [d["name"] for d in policy.get("drivers", [])]
    known = {_collapse(n) for n in [holder, *drivers] if isinstance(n, str)}
    listed = {"policy.holder": holder, "policy.drivers": drivers}
    if is_holder == "Yes":
        return _result(
            "coverage.driver_listed", "pass", None, "The policy holder was driving.", None,
            {"form.driver_is_holder": is_holder, **listed},
        )
    if is_holder == "No":
        raw = answers.get("driver_name")
        name = raw.strip() if isinstance(raw, str) else ""
        inputs = {"form.driver_is_holder": is_holder, "form.driver_name": name, **listed}
        if name and _collapse(name) in known:
            return _result(
                "coverage.driver_listed", "pass", None,
                f"{name} is listed as a driver on the policy.", None, inputs,
            )
        return _result(
            "coverage.driver_listed", "unclear", "DRIVER_NOT_LISTED",
            "The driver is not listed on the policy; permissive use may still cover them.", None, inputs,
        )
    return _result(
        "coverage.driver_listed", "unclear", "DRIVER_NOT_PROVIDED",
        "No driver information to check against the policy.", None,
        {"form.driver_is_holder": is_holder, **listed},
    )


def _no_driver_section(answers: dict) -> bool:
    """The new types that show no driver section state it in their overlay; the old
    types (and something_else) keep today's row."""
    overlay = loss_notice.template()["overlays"].get(answers.get("loss_type"))
    return bool(overlay and overlay.get("coverage") and not loss_notice.shows_section(answers, "driver"))


def _needed_coverage(answers: dict) -> str | None:
    loss_type = answers.get("loss_type")
    if loss_type == "vehicle_damage":
        return "comprehensive" if answers.get("cause") in COMPREHENSIVE_CAUSES else "collision"
    if loss_type == "something_else" and answers.get("what_happened") in COMPREHENSIVE_LOSSES:
        return "comprehensive"
    overlay = loss_notice.template()["overlays"].get(loss_type)
    return overlay.get("coverage") if overlay is not None else None


def _coverage_bought(policy: dict, answers: dict) -> dict:
    needed = _needed_coverage(answers)
    keys = sorted(policy.get("coverages", {}))
    inputs = {"form.loss_type": answers.get("loss_type"), "policy.coverages": keys}
    if answers.get("loss_type") == "something_else":
        inputs["form.what_happened"] = answers.get("what_happened")
    elif answers.get("cause") in COMPREHENSIVE_CAUSES:
        inputs["form.cause"] = answers.get("cause")
    if needed is None:
        return _result(
            "coverage.coverage_bought", "unclear", "COVERAGE_UNCLEAR",
            "No coverage maps to this kind of loss.", None, inputs,
        )
    if needed in policy["coverages"]:
        return _result(
            "coverage.coverage_bought", "pass", None,
            f"The policy includes {needed} coverage.", needed, inputs,
        )
    return _result(
        "coverage.coverage_bought", "fail", "COVERAGE_NOT_FOUND",
        f"The policy does not include {needed} coverage.", needed, inputs,
    )


def _deductible(policy: dict, bought: dict) -> dict:
    keys = sorted(policy.get("coverages", {}))
    if bought["result"] == "unclear":
        return _result(
            "coverage.deductible", "unclear", "COVERAGE_UNCLEAR",
            "No coverage was matched, so no deductible applies.", None, {"policy.coverages": keys},
        )
    needed = bought["value"]
    if bought["result"] == "fail":
        return _result(
            "coverage.deductible", "unclear", "COVERAGE_NOT_FOUND",
            f"The policy does not include {needed} coverage, so no deductible applies.",
            None, {"policy.coverages": keys},
        )
    amount = policy["coverages"][needed].get("deductible")
    if amount is None:
        return _result(
            "coverage.deductible", "unclear", "COVERAGE_UNCLEAR",
            f"The policy lists no deductible for {needed}.", None, {"policy.coverages": keys},
        )
    return _result(
        "coverage.deductible", "value", None, f"{needed.capitalize()} deductible from the policy.",
        amount, {f"policy.coverages.{needed}.deductible": amount},
    )
