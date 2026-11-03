"""T52 (tasks/t52-design.md, part A): review steps and answer fields per finding, claim and queue extras,
the diary, and the AI summary in the retelling style."""

from datetime import timedelta

import pytest

from evidence_server import ai_summary, report, review_steps
from test_damage_flow import captured_claim, send
from test_loss_notice import detail
from test_loss_notice_police import PARKED, submitted
from test_users import browser, signed_in

HIT_AND_RUN = PARKED | {"other_driver_left": "Yes", "police_contacted": "Not yet"}


def entry(code, source="submit"):
    return {"reason_code": code, "source": source}


def queue_entry(client, claim_id):
    q = client.get("/api/admin/queue").json()
    return next(c for g in q["groups"] for c in g["claims"] if c["id"] == claim_id)


def today(clock):
    return clock.now().date()


# --- review steps (pure) ---


def test_the_four_steps_follow_the_adjusters_order():
    assert review_steps.STEPS == ("coverage", "liability", "damage", "estimate")


@pytest.mark.parametrize("code, source, step", [
    ("VIN_MISMATCH", "photo.vin_photo", "coverage"),
    ("POLICY_NOT_IN_FORCE", "coverage.in_force", "coverage"),
    ("DRIVER_NOT_LISTED", "submit", "coverage"),
    ("ODOMETER_IMPLAUSIBLE", "photo.odometer", "coverage"),
    ("POLICE_REPORT_NEEDED", "submit", "liability"),
    ("MULTI_PARTY", "submit", "liability"),
    ("STORY_UNCLEAR", "consistency.story", "liability"),
    ("PRIOR_DAMAGE_SUSPECTED", "damage", "damage"),
    ("DAMAGE_AREA_MISMATCH", "photo.damage_closeup", "damage"),
    ("POSSIBLE_TOTAL_LOSS", "estimate", "estimate"),
    ("SOMETHING_NEW", "estimate", "estimate"),
    ("PHOTO_UNREADABLE", "photo.vin_photo", "coverage"),
    ("PHOTO_UNREADABLE", "photo.damage_wide", "damage"),
    ("PHOTO_UNCHECKED", "photo.driver_license", "coverage"),
    ("PHOTO_UNCHECKED", "photo.damage_closeup", "damage"),
    ("SOMETHING_NEW", "submit", "liability"),
])
def test_each_finding_lands_in_one_step(code, source, step):
    assert review_steps.step_of(entry(code, source)) == step


def test_findings_point_to_the_answers_they_are_about():
    answers = {"vin": "1HGCV1F39LA526018", "police_contacted": "Not yet", "description": "x", "damaged_areas": ["Hood"]}
    assert review_steps.fields_of(entry("VIN_MISMATCH"), answers) == ["vin"]
    assert review_steps.fields_of(entry("POLICE_REPORT_NEEDED"), answers) == ["police_contacted"]
    assert review_steps.fields_of(entry("STORY_UNCLEAR"), answers) == ["description"]
    assert review_steps.fields_of(entry("DAMAGE_AREA_MISMATCH"), answers) == ["damaged_areas"]
    assert review_steps.fields_of(entry("ODOMETER_IMPLAUSIBLE"), answers) == []


def test_an_answer_the_claimant_never_gave_is_not_pointed_at():
    assert review_steps.fields_of(entry("VIN_MISMATCH"), {"police_contacted": "No"}) == []
    assert review_steps.fields_of(entry("INJURY_REPORTED"), {}) == []


# --- claim detail ---


def test_every_finding_in_the_detail_carries_its_step_and_fields(client):
    d = detail(client, submitted(client, HIT_AND_RUN))
    entries = d["proposal"]["flags"] + d["proposal"]["notes"]
    assert entries
    for e in entries:
        assert e["step"] in review_steps.STEPS
        assert isinstance(e["fields"], list)
    police = next(f for f in d["proposal"]["flags"] if f["reason_code"] == "POLICE_REPORT_NEEDED")
    assert (police["step"], police["fields"]) == ("liability", ["police_contacted"])


def test_the_detail_names_who_to_call(client):
    d = detail(client, submitted(client, {}))
    assert d["contact"] == {"name": "Jordan Avery", "phone": "+1 415 555 0101", "email": "jordan.avery@example.com"}


def test_the_detail_shows_the_coverage_that_applies(client):
    d = detail(client, submitted(client, {}))
    assert d["coverage"] == {"type": "collision", "deductible": 500, "lienholder": None}


def test_the_review_is_due_two_business_days_after_the_report(client, clock):
    claim, _ = captured_claim(client, clock)
    assert detail(client, claim)["review_due"] is None
    d = send(client, claim)
    expected = report.business_days_after(today(clock), report.REVIEW_BUSINESS_DAYS).isoformat()
    assert d["review_due"] == expected
    assert d["diaries"] == []


# --- queue ---


def test_queue_entries_carry_type_due_date_and_overdue(client, clock):
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    q = queue_entry(client, claim["id"])
    assert isinstance(q["claim_type"], str) and q["claim_type"]
    assert q["review_due"] == d["review_due"]
    assert q["overdue"] is False
    assert (q["diary_due"], q["diary_reason"]) == (None, None)

    clock.advance(days=7)
    assert queue_entry(client, claim["id"])["overdue"] is True

    r = client.post(f"/api/admin/claims/{claim['id']}/decision", json={"action": "approve", "reason": "Checked the photos"})
    assert r.status_code == 200, r.text
    assert queue_entry(client, claim["id"])["overdue"] is False


def test_a_claim_still_capturing_has_no_due_date(client):
    claim = client.post("/api/admin/claims", json={"policy_number": "DEMO-AUTO-1001"}).json()
    q = queue_entry(client, claim["id"])
    assert (q["review_due"], q["overdue"], q["claim_type"]) == (None, False, None)


# --- diary ---


def set_diary(client, claim_id, due_on, reason="Call the claimant about the VIN"):
    return client.post(f"/api/admin/claims/{claim_id}/diaries", json={"due_on": due_on, "reason": reason})


def test_a_diary_is_set_and_shows_on_the_claim_and_the_queue(client, clock):
    claim = submitted(client, {})
    due = (today(clock) + timedelta(days=2)).isoformat()
    r = set_diary(client, claim["id"], due)
    assert r.status_code == 201, r.text
    diary = r.json()
    me = client.get("/api/admin/me").json()
    assert diary["due_on"] == due and diary["reason"] == "Call the claimant about the VIN"
    assert diary["created_by"] == me["name"] and diary["done_at"] is None and diary["done_by"] is None
    assert [x["id"] for x in detail(client, claim)["diaries"]] == [diary["id"]]
    q = queue_entry(client, claim["id"])
    assert (q["diary_due"], q["diary_reason"]) == (due, "Call the claimant about the VIN")


def test_the_earliest_open_diary_leads(client, clock):
    claim = submitted(client, {})
    later = set_diary(client, claim["id"], (today(clock) + timedelta(days=5)).isoformat(), "Later one").json()
    sooner = set_diary(client, claim["id"], (today(clock) + timedelta(days=1)).isoformat(), "Sooner one").json()
    assert [x["id"] for x in detail(client, claim)["diaries"]] == [sooner["id"], later["id"]]
    assert queue_entry(client, claim["id"])["diary_reason"] == "Sooner one"


def test_marking_a_diary_done(client, clock):
    claim = submitted(client, {})
    diary = set_diary(client, claim["id"], today(clock).isoformat()).json()
    r = client.post(f"/api/admin/diaries/{diary['id']}/done")
    assert r.status_code == 200, r.text
    done = r.json()
    assert done["done_at"] and done["done_by"]
    clock.advance(minutes=5)
    again = client.post(f"/api/admin/diaries/{diary['id']}/done").json()
    assert (again["done_at"], again["done_by"]) == (done["done_at"], done["done_by"])
    q = queue_entry(client, claim["id"])
    assert (q["diary_due"], q["diary_reason"]) == (None, None)
    assert detail(client, claim)["diaries"][0]["done_at"] == done["done_at"]
    assert client.post("/api/admin/diaries/nope/done").status_code == 404


def test_open_diaries_come_before_done_ones(client, clock):
    claim = submitted(client, {})
    first = set_diary(client, claim["id"], today(clock).isoformat(), "First").json()
    client.post(f"/api/admin/diaries/{first['id']}/done")
    second = set_diary(client, claim["id"], (today(clock) + timedelta(days=3)).isoformat(), "Second").json()
    assert [x["id"] for x in detail(client, claim)["diaries"]] == [second["id"], first["id"]]


@pytest.mark.parametrize("due_on, reason", [
    ("2026-02-30", "A real reason"),
    ("next week", "A real reason"),
    (None, "A real reason"),
    ("YESTERDAY", "A real reason"),
    ("FAR", "A real reason"),
    ("TODAY", "no"),
    ("TODAY", "x" * 201),
    ("TODAY", None),
])
def test_bad_diaries_are_refused_with_a_plain_message(client, clock, due_on, reason):
    claim = submitted(client, {})
    dates = {"YESTERDAY": today(clock) - timedelta(days=1), "FAR": today(clock) + timedelta(days=366), "TODAY": today(clock)}
    due = dates[due_on].isoformat() if due_on in dates else due_on
    r = client.post(f"/api/admin/claims/{claim['id']}/diaries", json={"due_on": due, "reason": reason})
    assert r.status_code == 400, r.text
    assert r.json().get("detail")


def test_a_diary_on_an_unknown_claim_is_404(client, clock):
    assert set_diary(client, "CLM-20990101-NOPE00", today(clock).isoformat()).status_code == 404


def test_diaries_are_written_to_the_claim_timeline(client, clock):
    claim = submitted(client, {})
    diary = set_diary(client, claim["id"], today(clock).isoformat()).json()
    client.post(f"/api/admin/diaries/{diary['id']}/done")
    events = {e["type"]: e["data"] for e in detail(client, claim)["events"] if e["type"].startswith("diary.")}
    assert set(events) == {"diary.set", "diary.done"}
    for data in events.values():
        assert data["diary_id"] == diary["id"] and data["due_on"] == diary["due_on"] and data["reason"] == diary["reason"]
        assert data["user_id"] and data["user_name"]


def test_an_adjuster_can_set_and_finish_diaries(anon, client, clock):
    claim = submitted(client, {})
    adjuster, made = signed_in(anon, client)
    r = adjuster.post(f"/api/admin/claims/{claim['id']}/diaries", json={"due_on": today(clock).isoformat(), "reason": "Ask for the repair estimate"})
    assert r.status_code == 201, r.text
    assert r.json()["created_by"] == made["name"]
    assert adjuster.post(f"/api/admin/diaries/{r.json()['id']}/done").json()["done_by"] == made["name"]


def test_diaries_need_a_signed_in_user(anon, client, clock):
    claim = submitted(client, {})
    stranger = browser(anon)  # a fresh browser with no session
    assert stranger.post(f"/api/admin/claims/{claim['id']}/diaries", json={"due_on": today(clock).isoformat(), "reason": "Call"}).status_code == 401


# --- AI summary in the retelling style ---


def test_the_summary_prompt_asks_for_the_retelling():
    text = ai_summary.prompt({"S": "Backed into a bollard."})
    assert ai_summary.VERSION == "ai-summary/v3"
    assert ai_summary.MAX_WORDS == 90
    assert "what the claimant says happened" in text
    assert "90 words" in text


def sentences(n):
    words = ["first", "second", "third", "fourth", "fifth"]
    return {"sentences": [{"text": f"The claimant says they backed into a bollard, the {w} time.", "refs": ["S"]} for w in words[:n]]}


def test_four_sentences_are_accepted_and_five_are_not():
    facts = {"S": "Backed into a bollard."}
    assert ai_summary.verify(sentences(4), facts)["status"] == "verified"
    five = ai_summary.verify(sentences(5), facts)
    assert five["status"] == "rejected" and five["reasons"][0].startswith("shape")
