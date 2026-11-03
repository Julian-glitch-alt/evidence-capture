"""T13 coverage check (contract: tasks/t13-design.md): five rule checks on the policy snapshot, stored at submit."""

import json
from pathlib import Path

import pytest

from evidence_server import coverage
from test_loss_notice import DAMAGE_ANSWERS, capture_required, detail, open_claim, save

SEED = json.loads((Path(__file__).parents[1] / "src/evidence_server/seed/policies.json").read_text())
POLICIES = {p["policy_no"]: p for p in SEED["policies"]}
P1001 = POLICIES["DEMO-AUTO-1001"]  # CA, 2026-03-01 → 2027-03-01, collision $500, comprehensive $250
P1002 = POLICIES["DEMO-AUTO-1002"]  # two drivers: Sam Rivera, Alex Rivera
P1003 = POLICIES["DEMO-AUTO-1003"]  # liability only
P1004 = POLICIES["DEMO-AUTO-1004"]  # expired 2026-09-01

THEFT = {"loss_type": "vehicle_theft", "description": "Car stolen from the driveway."}  # T43a: a type of its own
OTHER = {"loss_type": "something_else", "what_happened": "Someone was hurt", "description": "My passenger was hurt."}


def run(policy, answers, verified=True):
    return {r["check_id"]: r for r in coverage.check(policy, verified, answers)}


def answers(**changes):
    return {**DAMAGE_ANSWERS, **changes}


# --- the pure function ---


def test_clean_claim_passes_and_reads_the_deductible():
    results = coverage.check(P1001, True, answers())
    assert [r["check_id"] for r in results] == list(coverage.CHECK_ORDER)
    by_id = {r["check_id"]: r for r in results}
    assert [by_id[c]["result"] for c in coverage.CHECK_ORDER[:4]] == ["pass"] * 4
    assert by_id["coverage.deductible"]["result"] == "value"
    assert by_id["coverage.deductible"]["value"] == 500
    assert by_id["coverage.coverage_bought"]["value"] == "collision"
    assert all(r["reason"] for r in results)
    assert all(r["reason_code"] is None for r in results)


def test_same_inputs_give_the_same_output():
    assert coverage.check(P1001, True, answers()) == coverage.check(P1001, True, answers())


@pytest.mark.parametrize(
    "loss_date, expected",
    [
        ("2026-03-01", "pass"),  # effective day
        ("2027-02-28", "pass"),
        ("2027-03-01", "fail"),  # expiry day: policy no longer in force
        ("2026-02-28", "fail"),  # before the policy started
    ],
)
def test_in_force_window(loss_date, expected):
    r = run(P1001, answers(loss_date=loss_date))["coverage.in_force"]
    assert r["result"] == expected
    if expected == "fail":
        assert r["reason_code"] == "POLICY_NOT_IN_FORCE"
    assert r["inputs"] == {"form.loss_date": loss_date, "policy.effective": "2026-03-01", "policy.expires": "2027-03-01"}


def test_expired_policy_fails():
    r = run(P1004, answers(loss_date="2026-10-20", vin="JTDKARFU1PA186091"))
    assert r["coverage.in_force"]["result"] == "fail"
    assert r["coverage.in_force"]["reason_code"] == "POLICY_NOT_IN_FORCE"


@pytest.mark.parametrize("loss_date", [None, "", "yesterday", "2026-13-40"])
def test_missing_or_bad_loss_date_is_unclear(loss_date):
    a = answers()
    if loss_date is None:
        a.pop("loss_date")
    else:
        a["loss_date"] = loss_date
    r = run(P1001, a)["coverage.in_force"]
    assert (r["result"], r["reason_code"]) == ("unclear", "LOSS_DATE_NOT_PROVIDED")


@pytest.mark.parametrize(
    "vin, result, code",
    [
        ("1HGCV1F39LA526018", "pass", None),
        ("1hgcv1f39la526018", "pass", None),
        (" 1HGCV1F39LA526018 ", "pass", None),
        ("JTDKARFU1PA186091", "fail", "VIN_MISMATCH"),
        ("", "unclear", "VIN_NOT_PROVIDED"),
    ],
)
def test_vehicle_listed(vin, result, code):
    r = run(P1001, answers(vin=vin))["coverage.vehicle_listed"]
    assert (r["result"], r["reason_code"]) == (result, code)


@pytest.mark.parametrize(
    "driver, result, code",
    [
        ({"driver_is_holder": "Yes"}, "pass", None),
        ({"driver_is_holder": "No", "driver_name": "Alex Rivera"}, "pass", None),
        ({"driver_is_holder": "No", "driver_name": "  alex   RIVERA "}, "pass", None),
        ({"driver_is_holder": "No", "driver_name": "Sam Rivera"}, "pass", None),
        ({"driver_is_holder": "No", "driver_name": "Jamie Lee"}, "unclear", "DRIVER_NOT_LISTED"),
    ],
)
def test_driver_listed_never_fails(driver, result, code):
    a = answers(vin="5YJ3E1EA8MA159083", **driver)
    r = run(P1002, a)["coverage.driver_listed"]
    assert (r["result"], r["reason_code"]) == (result, code)


def test_liability_only_policy_has_no_collision():
    r = run(P1003, answers(vin="1FTFW1E5XNA016613"))
    assert (r["coverage.coverage_bought"]["result"], r["coverage.coverage_bought"]["reason_code"]) == ("fail", "COVERAGE_NOT_FOUND")
    assert r["coverage.coverage_bought"]["value"] == "collision"
    assert (r["coverage.deductible"]["result"], r["coverage.deductible"]["reason_code"]) == ("unclear", "COVERAGE_NOT_FOUND")
    assert r["coverage.deductible"]["value"] is None


@pytest.mark.parametrize("cause", ["Hit another vehicle", "Hit an object", "Hit while parked", "Scrape or other"])
def test_every_vehicle_damage_cause_needs_collision(cause):
    assert run(P1001, answers(cause=cause))["coverage.coverage_bought"]["value"] == "collision"


@pytest.mark.parametrize("what", ["vehicle_theft", "fire", "weather"])
def test_theft_fire_weather_need_comprehensive(what):
    r = run(P1001, {**THEFT, "loss_type": what, "loss_date": "2026-10-20"})
    assert (r["coverage.coverage_bought"]["result"], r["coverage.coverage_bought"]["value"]) == ("pass", "comprehensive")
    assert (r["coverage.deductible"]["result"], r["coverage.deductible"]["value"]) == ("value", 250)


def test_theft_on_a_liability_only_policy_fails():
    r = run(P1003, THEFT)
    assert (r["coverage.coverage_bought"]["result"], r["coverage.coverage_bought"]["reason_code"]) == ("fail", "COVERAGE_NOT_FOUND")


@pytest.mark.parametrize("what", ["Someone was hurt", "Something else", None])
def test_injury_or_other_is_unclear(what):
    a = {**OTHER, "what_happened": what} if what else {k: v for k, v in OTHER.items() if k != "what_happened"}
    r = run(P1001, a)
    assert (r["coverage.coverage_bought"]["result"], r["coverage.coverage_bought"]["reason_code"]) == ("unclear", "COVERAGE_UNCLEAR")
    assert (r["coverage.deductible"]["result"], r["coverage.deductible"]["reason_code"]) == ("unclear", "COVERAGE_UNCLEAR")


def test_something_else_has_no_vin_or_driver_to_check():
    r = run(P1001, OTHER)
    assert (r["coverage.in_force"]["result"], r["coverage.in_force"]["reason_code"]) == ("unclear", "LOSS_DATE_NOT_PROVIDED")
    assert (r["coverage.vehicle_listed"]["result"], r["coverage.vehicle_listed"]["reason_code"]) == ("unclear", "VIN_NOT_PROVIDED")
    assert (r["coverage.driver_listed"]["result"], r["coverage.driver_listed"]["reason_code"]) == ("unclear", "DRIVER_NOT_PROVIDED")


@pytest.mark.parametrize("policy, verified", [(None, False), (P1001, False)])
def test_unverified_policy_is_all_unclear(policy, verified):
    results = coverage.check(policy, verified, answers())
    assert [(r["result"], r["reason_code"]) for r in results] == [("unclear", "POLICY_NOT_VERIFIED")] * 5
    assert results[-1]["value"] is None


def test_new_reason_codes_are_in_the_vocabulary():
    from evidence_server import vocab

    assert {"LOSS_DATE_NOT_PROVIDED", "VIN_NOT_PROVIDED", "DRIVER_NOT_LISTED", "DRIVER_NOT_PROVIDED", "COVERAGE_UNCLEAR"} <= vocab.REASON_CODES


# --- stored at submit ---


def submit(client, policy="DEMO-AUTO-1001", **changes):
    claim = open_claim(client, policy)
    layout = save(client, claim["token"], answers(**changes))
    capture_required(client, claim["token"], layout)
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 200, r.text
    return detail(client, claim)


def test_submit_stores_five_checks_one_event_and_policy_facts(client):
    d = submit(client)
    rows = [c for c in d["checks"] if c["check_id"].startswith("coverage.")]  # T34 adds photo.* rows after these
    assert [c["check_id"] for c in rows] == list(coverage.CHECK_ORDER)
    assert [c["result"] for c in rows] == ["pass", "pass", "pass", "pass", "value"]
    assert all(c["source"] == coverage.VERSION and c["created_at"] == d["submitted_at"] for c in rows)
    types = [e["type"] for e in d["events"]]
    assert types.count("coverage.checked") == 1
    assert types.index("coverage.checked") > types.index("claim.sealed")
    assert d["facts"]["deductible_amount"]["value"] == 500
    assert d["facts"]["deductible_amount"]["source"] == "VERIFIED"
    assert d["facts"]["coverage_type"]["value"] == "collision"


def test_checks_keep_the_event_chain_valid(client):
    d = submit(client)
    assert client.app.state.store.verify_chain(d["id"])["ok"] is True


def test_no_checks_before_submit(client):
    claim = open_claim(client)
    assert detail(client, claim)["checks"] == []


def test_failed_check_does_not_add_submit_reason_codes_or_decisions(client):
    d = submit(client, policy="DEMO-AUTO-1003", vin="1FTFW1E5XNA016613")
    bought = next(c for c in d["checks"] if c["check_id"] == "coverage.coverage_bought")
    assert (bought["result"], bought["reason_code"]) == ("fail", "COVERAGE_NOT_FOUND")
    assert "COVERAGE_NOT_FOUND" not in d["reason_codes"]
    assert set(d["decisions"].values()) == {"UNDETERMINED"}
    assert "deductible_amount" not in d["facts"]


def test_vin_mismatch_keeps_the_existing_submit_code_once(client):
    d = submit(client, vin="JTDKARFU1PA186091")
    assert d["reason_codes"].count("VIN_MISMATCH") == 1
    listed = next(c for c in d["checks"] if c["check_id"] == "coverage.vehicle_listed")
    assert (listed["result"], listed["reason_code"]) == ("fail", "VIN_MISMATCH")


# --- loss date in the future (the test clock is 2026-10-28 15:00 UTC, i.e. 2026-10-29 at UTC+14) ---


@pytest.mark.parametrize("loss_date, accepted", [("2026-10-28", True), ("2026-10-29", True), ("2026-10-30", False)])
def test_loss_date_in_the_future_is_refused(client, loss_date, accepted):
    claim = open_claim(client)
    layout = save(client, claim["token"], answers(loss_date=loss_date))
    field = next(f for s in layout["sections"] for f in s["fields"] if f["id"] == "loss_date")
    if accepted:
        assert field["error"] is None
        assert "What day did it happen?" not in layout["missing"]
    else:
        assert field["error"] == "The date of the loss cannot be in the future."
        assert "What day did it happen?" in layout["missing"]
        capture_required(client, claim["token"], layout)
        r = client.post(f"/api/c/{claim['token']}/submit", json={})
        assert r.status_code == 400
        assert "What day did it happen?" in r.json()["detail"]


def test_malformed_loss_date_is_a_field_error(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], answers(loss_date="next tuesday"))
    field = next(f for s in layout["sections"] for f in s["fields"] if f["id"] == "loss_date")
    assert field["error"] == "Enter a valid date."
