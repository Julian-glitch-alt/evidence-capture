"""Track 4: the loss notice asks what a US auto insurer asks. Police details, hit and
run, the other party, and the proposed comprehensive causes; code checks that flag
for a person and never block (docs/research/claim-documents-1-nov.md)."""

import hashlib
import json
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from evidence_server import coverage, loss_notice, proposal, report, report_pdf
from test_loss_notice import DAMAGE_ANSWERS, capture_required, detail, open_claim, save

PARKED = {"cause": "Hit while parked", "other_vehicle": "Yes"}
POLICE = {"police_contacted": "Yes", "police_agency": "Los Angeles Police Department", "police_report_no": "24-118803"}


def fields(layout):
    return {f["id"]: f for s in layout["sections"] for f in s["fields"]}


def submitted(client, answers):
    claim = open_claim(client)
    layout = save(client, claim["token"], DAMAGE_ANSWERS | answers)
    capture_required(client, claim["token"], layout)
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 200, r.text
    return claim


@pytest.fixture
def proposed_causes(monkeypatch):
    monkeypatch.setenv(loss_notice.PROPOSED_OPTIONS_ENV, "on")


# --- the form ---


def test_police_has_a_not_yet_answer_and_a_reason(client):
    claim = open_claim(client)
    f = fields(save(client, claim["token"], DAMAGE_ANSWERS))
    assert f["police_contacted"]["options"] == ["Yes", "No", "Not yet"]
    assert f["police_contacted"]["hint"] == "Your insurer may confirm the report with the department."
    assert not {"police_agency", "police_report_no", "police_officer"} & set(f)


def test_police_yes_asks_department_report_and_officer(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], DAMAGE_ANSWERS | {"police_contacted": "Yes"})
    f = fields(layout)
    assert [(k, f[k]["required"]) for k in ("police_agency", "police_report_no", "police_officer")] == [
        ("police_agency", True), ("police_report_no", False), ("police_officer", False),
    ]
    assert "Which police department?" in layout["missing"]
    layout = save(client, claim["token"], DAMAGE_ANSWERS | {"police_contacted": "Yes", "police_agency": "LAPD"})
    assert "Which police department?" not in layout["missing"]


@pytest.mark.parametrize("cause, asked", [
    ("Hit another vehicle", True), ("Hit while parked", True), ("Hit an object", False), ("Scrape or other", False),
])
def test_hit_and_run_question_follows_the_cause(client, cause, asked):
    claim = open_claim(client)
    layout = save(client, claim["token"], DAMAGE_ANSWERS | {"cause": cause})
    f = fields(layout)
    assert ("other_driver_left" in f) is asked
    if asked:
        assert f["other_driver_left"]["required"] is True
        assert f["other_driver_left"]["label"] in layout["missing"]


def test_other_party_details_when_the_driver_stayed(client):
    claim = open_claim(client)
    f = fields(save(client, claim["token"], DAMAGE_ANSWERS | PARKED | {"other_driver_left": "No"}))
    shown = [k for k in f if k.startswith("other_") and k not in ("other_vehicle", "other_driver_left")]
    assert shown == ["other_driver_name", "other_driver_phone", "other_plate", "other_vehicle_desc",
                     "other_insurer", "other_policy_no"]
    assert not any(f[k]["required"] for k in shown)


def test_a_driver_who_left_hides_what_nobody_could_know(client):
    claim = open_claim(client)
    f = fields(save(client, claim["token"], DAMAGE_ANSWERS | PARKED | {"other_driver_left": "Yes"}))
    shown = [k for k in f if k.startswith("other_") and k not in ("other_vehicle", "other_driver_left")]
    assert shown == ["other_plate", "other_vehicle_desc"]  # what a witness or the claimant may have seen


# --- code checks: review, never a block ---


def test_hit_and_run_without_a_report_number_flags_for_review_and_still_submits(client):
    claim = submitted(client, PARKED | {"other_driver_left": "Yes", "police_contacted": "Not yet"})
    d = detail(client, claim)
    assert "POLICE_REPORT_NEEDED" in d["reason_codes"] and d["human_review_required"] is True
    flag = next(f for f in d["proposal"]["flags"] if f["reason_code"] == "POLICE_REPORT_NEEDED")
    assert (flag["level"], flag["cause"], flag["source"]) == ("review", "police_report", "submit")
    assert flag["reason"] == "A police report is usually required for a hit-and-run claim; none was given."
    assert d["proposal"]["lane"] == "adjuster_review"
    assert {"cause": "police_report", "label": "police report", "level": "review", "model_only": False, "rows": 1,
            "codes": ["POLICE_REPORT_NEEDED"]} \
        in d["proposal"]["causes"]


def test_a_report_number_clears_the_police_finding(client):
    d = detail(client, submitted(client, PARKED | {"other_driver_left": "Yes"} | POLICE))
    assert "POLICE_REPORT_NEEDED" not in d["reason_codes"]


def test_a_number_typed_before_the_answer_changed_does_not_count(client):
    stale = PARKED | {"other_driver_left": "Yes"} | POLICE | {"police_contacted": "Not yet"}
    d = detail(client, submitted(client, stale))
    assert "POLICE_REPORT_NEEDED" in d["reason_codes"]
    assert d["to_verify"] == []


def test_the_driver_staying_asks_for_no_report(client):
    d = detail(client, submitted(client, PARKED | {"other_driver_left": "No", "police_contacted": "No"}))
    assert "POLICE_REPORT_NEEDED" not in d["reason_codes"]


def test_police_report_needed_is_its_own_root_cause():
    assert proposal.ROOT_CAUSES["POLICE_REPORT_NEEDED"] == "police_report"
    assert "police_report" not in proposal.MODEL_ONLY


# --- to verify with the department ---


@pytest.mark.parametrize("answers, line", [
    (POLICE, "Police report 24-118803, Los Angeles Police Department: confirm with the department"),
    (POLICE | {"police_officer": "Ofc. Reyes #4412"},
     "Police report 24-118803, Los Angeles Police Department, officer Ofc. Reyes #4412: confirm with the department"),
    ({"police_contacted": "Yes", "police_agency": "San Jose Police Department"},
     "Police contacted, no report number given, San Jose Police Department: confirm with the department"),
])
def test_to_verify_line(answers, line):
    assert loss_notice.to_verify(DAMAGE_ANSWERS | answers) == [line]


@pytest.mark.parametrize("contacted", ["No", "Not yet"])
def test_nothing_to_verify_without_police(contacted):
    assert loss_notice.to_verify(DAMAGE_ANSWERS | POLICE | {"police_contacted": contacted}) == []


def test_police_details_reach_the_console_report_and_seal(client, monkeypatch):
    claim = submitted(client, PARKED | {"other_driver_left": "Yes"} | POLICE | {"police_officer": "Badge 4412"})
    d = detail(client, claim)
    line = "Police report 24-118803, Los Angeles Police Department, officer Badge 4412: confirm with the department"
    assert d["to_verify"] == [line]
    assert d["report"]["url"].startswith("/api/r/")  # the stored PDF rendered with the line
    # Every answer is in the sealed form, like the others.
    with client.app.state.store.db.tx() as conn:
        row = conn.execute("SELECT form_json FROM claims WHERE id=%s", (claim["id"],)).fetchone()
        manifest = json.loads(conn.execute("SELECT manifest_json FROM seals WHERE claim_id=%s",
                                           (claim["id"],)).fetchone()["manifest_json"])
    form = json.loads(row["form_json"])
    assert {k: form[k] for k in ("other_driver_left", "police_agency", "police_report_no", "police_officer")} == {
        "other_driver_left": "Yes", "police_agency": "Los Angeles Police Department",
        "police_report_no": "24-118803", "police_officer": "Badge 4412",
    }
    assert manifest["answers_sha256"] == hashlib.sha256(row["form_json"].encode()).hexdigest()
    # One line in the PDF's claim block.
    data = report.build(d, "Demo Mutual", datetime(2026, 10, 28, 16, tzinfo=UTC), ZoneInfo("America/Los_Angeles"))
    assert data["to_verify"] == [line]
    rows = []
    monkeypatch.setattr(report_pdf, "_claim_row", lambda pdf, cells: rows.append(cells))
    report_pdf.render(data, {})
    assert [("To verify", line, 4, False)] in rows


# --- proposed causes (behind LOSS_NOTICE_COMPREHENSIVE_CAUSES, Julian decides) ---


def test_proposed_causes_are_off_by_default(client):
    claim = open_claim(client)
    f = fields(save(client, claim["token"], DAMAGE_ANSWERS))
    assert f["cause"]["options"] == ["Hit another vehicle", "Hit an object", "Hit while parked", "Scrape or other"]
    assert "proposed_options" not in f["cause"]
    assert loss_notice.clean_answers(DAMAGE_ANSWERS | {"cause": "Vandalism"}).get("cause") is None


def test_proposed_causes_offered_when_switched_on(client, proposed_causes):
    claim = open_claim(client)
    f = fields(save(client, claim["token"], DAMAGE_ANSWERS | {"cause": "Hit an animal"}))
    assert f["cause"]["options"][-2:] == ["Hit an animal", "Vandalism"]
    assert f["cause"]["value"] == "Hit an animal"


@pytest.mark.parametrize("cause, needed", [
    ("Hit an animal", "comprehensive"), ("Vandalism", "comprehensive"),
    ("Hit another vehicle", "collision"), ("Hit an object", "collision"),
    ("Hit while parked", "collision"), ("Scrape or other", "collision"),
])
def test_cause_maps_to_the_coverage_a_us_policy_uses(cause, needed):
    assert coverage._needed_coverage({"loss_type": "vehicle_damage", "cause": cause}) == needed


def test_vandalism_claim_checks_comprehensive_and_needs_a_report(client, proposed_causes):
    d = detail(client, submitted(client, {"cause": "Vandalism", "police_contacted": "No"}))
    bought = next(c for c in d["checks"] if c["check_id"] == "coverage.coverage_bought")
    assert (bought["result"], bought["value"], bought["inputs"]["form.cause"]) == ("pass", "comprehensive", "Vandalism")
    deductible = next(c for c in d["checks"] if c["check_id"] == "coverage.deductible")
    assert (deductible["result"], deductible["value"]) == ("value", 250)
    assert "POLICE_REPORT_NEEDED" in d["reason_codes"]
    assert d["facts"]["loss_event_type"]["value"] == "VANDALISM"


def test_animal_claim_is_comprehensive_without_a_police_finding(client, proposed_causes):
    d = detail(client, submitted(client, {"cause": "Hit an animal", "police_contacted": "No"}))
    bought = next(c for c in d["checks"] if c["check_id"] == "coverage.coverage_bought")
    assert (bought["result"], bought["value"]) == ("pass", "comprehensive")
    assert "POLICE_REPORT_NEEDED" not in d["reason_codes"]
    assert d["facts"]["loss_event_type"]["value"] == "SINGLE_VEHICLE_COLLISION"
