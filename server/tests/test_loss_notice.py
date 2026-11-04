"""The claimant's loss notice: layout per loss type, drafts, and what submit requires."""

import json
from pathlib import Path

import pytest

from conftest import JPEG, MP4, PNG, Phone

OVERLAYS = sorted(
    p.stem for p in (Path(__file__).parents[1] / "src/evidence_server/templates/loss_notice").glob("*.json") if p.stem != "base"
)

MEDIA = {"video": (MP4, "video/mp4"), "signature": (PNG, "image/png")}
VALID_VIN = "1HGCV1F39LA526018"  # DEMO-AUTO-1001's vehicle

DAMAGE_ANSWERS = {
    "loss_type": "vehicle_damage",
    "loss_date": "2026-10-27",
    "loss_time": "17:30",
    "loss_city": "San Jose",
    "loss_state": "CA",
    "cause": "Hit an object",
    "description": "Backed into a bollard in a parking lot.",
    "police_contacted": "No",
    "vin": VALID_VIN,
    "drivable": "Yes",
    "driver_is_holder": "Yes",
    "damaged_areas": ["Rear bumper"],
    "prior_damage": "No",
    "other_vehicle": "No",
    "injuries": "No",
    "full_name": "Jordan Avery",
    "agree": True,
}


def open_claim(client, policy="DEMO-AUTO-1001"):
    return client.post("/api/admin/claims", json={"policy_number": policy}).json()


def save(client, token, answers):
    r = client.put(f"/api/c/{token}/draft", json={"answers": answers})
    assert r.status_code == 200, r.text
    return r.json()["layout"]


def capture_required(client, token, layout):
    phone = Phone(client, token)
    for section in layout["sections"]:
        for slot in section["slots"]:
            if slot["required"]:
                data, mime = MEDIA.get(slot["kind"], (JPEG, "image/jpeg"))
                assert phone.upload(slot["id"], data, mime).status_code == 200


def detail(client, claim):
    return client.get(f"/api/admin/claims/{claim['id']}").json()


def test_new_claim_asks_what_happened_first(client):
    claim = open_claim(client)
    layout = client.get(f"/api/c/{claim['token']}").json()["layout"]
    assert layout["loss_type"] is None
    assert [t["id"] for t in layout["loss_types"]] == [  # T43a: ten types, T60: other_damage; vehicle_damage is hidden
        "collision_vehicle", "hit_and_run", "single_vehicle", "animal", "vandalism", "glass", "weather",
        "parts_theft", "fire", "vehicle_theft", "other_damage", "something_else",
    ]
    assert layout["complete"] is False


def test_vehicle_damage_has_the_nine_acord_sections(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "vehicle_damage"})
    assert [s["id"] for s in layout["sections"]] == [
        "policy", "loss", "vehicle", "driver", "damage", "other_party", "witnesses", "documents", "declaration",
    ]
    slots = {slot["id"]: slot for s in layout["sections"] for slot in s["slots"]}
    required = {k for k, v in slots.items() if v["required"]}
    assert required == {"plate", "vin_photo", "odometer", "driver_license", "walkaround", "damage_closeup", "damage_wide", "signature"}


def test_something_else_is_short_and_has_no_photo_slots(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "something_else"})
    assert [s["id"] for s in layout["sections"]] == ["policy", "contact", "declaration"]
    camera_slots = [x for s in layout["sections"] for x in s["slots"] if x["kind"] in ("photo", "video")]
    assert not camera_slots
    assert [x["id"] for x in layout["sections"][-1]["slots"]] == ["signature"]


@pytest.mark.parametrize("overlay", OVERLAYS)
def test_every_overlay_lists_sections_that_exist(client, overlay):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": overlay})
    assert layout["sections"], overlay


def test_policy_section_is_prefilled_from_the_snapshot(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "vehicle_damage"})
    policy = layout["sections"][0]["policy"]
    assert policy["vehicle"] == "2020 Honda Accord"
    assert policy["vin"] == VALID_VIN
    assert policy["coverages"]["collision"]["deductible"] == 500


def test_draft_survives_reopening_the_link(client):
    claim = open_claim(client)
    save(client, claim["token"], {"loss_type": "vehicle_damage", "loss_city": "San Jose"})
    state = client.get(f"/api/c/{claim['token']}").json()
    assert state["answers"]["loss_city"] == "San Jose"
    assert state["layout"]["loss_type"] == "vehicle_damage"


def test_draft_ignores_unknown_fields(client):
    claim = open_claim(client)
    save(client, claim["token"], {"loss_type": "vehicle_damage", "is_admin": True, "loss_city": "x" * 5000})
    answers = client.get(f"/api/c/{claim['token']}").json()["answers"]
    assert "is_admin" not in answers
    assert len(answers["loss_city"]) <= 2000


@pytest.mark.parametrize(
    "vin, ok",
    [(VALID_VIN, True), ("1HGCV1F39LA526019", False), ("1HGCV1F39LA52601", False), ("1HGCV1F39LA52601O", False)],
)
def test_vin_check_digit(client, vin, ok):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "vehicle_damage", "vin": vin})
    field = next(f for f in layout["sections"][2]["fields"] if f["id"] == "vin")
    assert (field["error"] is None) == ok


def test_vin_matching_the_policy_is_noted(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "vehicle_damage", "vin": VALID_VIN})
    field = next(f for f in layout["sections"][2]["fields"] if f["id"] == "vin")
    assert field["note"].startswith("From your policy")  # T43a: the policy's own VIN reads as prefilled


def test_conditional_field_appears_and_becomes_required(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "vehicle_damage", "driver_is_holder": "Yes"})
    ids = [f["id"] for f in layout["sections"][3]["fields"]]
    assert "driver_name" not in ids

    layout = save(client, claim["token"], {"loss_type": "vehicle_damage", "driver_is_holder": "No"})
    driver_name = next(f for f in layout["sections"][3]["fields"] if f["id"] == "driver_name")
    assert driver_name["required"] is True


def test_complete_damage_claim_submits_and_records_facts(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], DAMAGE_ANSWERS)
    capture_required(client, claim["token"], layout)
    state = client.get(f"/api/c/{claim['token']}").json()
    assert state["layout"]["complete"] is True, state["layout"]["missing"]

    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 200, r.text
    d = detail(client, claim)
    assert d["status"] == "submitted"
    assert d["reason_codes"] == []
    assert d["human_review_required"] is False
    assert d["facts"]["incident_date"] == {"value": "2026-10-27", "source": "REPORTED", "evidence": ["form:loss.loss_date"]}
    assert d["facts"]["loss_event_type"]["value"] == "SINGLE_VEHICLE_COLLISION"


@pytest.mark.parametrize("slot", ["plate", "vin_photo", "odometer", "driver_license", "walkaround", "damage_closeup", "damage_wide", "signature"])
def test_submit_refused_while_a_required_slot_is_missing(client, slot):
    claim = open_claim(client)
    layout = save(client, claim["token"], DAMAGE_ANSWERS)
    phone = Phone(client, claim["token"])
    for section in layout["sections"]:
        for s in section["slots"]:
            if s["required"] and s["id"] != slot:
                data, mime = MEDIA.get(s["kind"], (JPEG, "image/jpeg"))
                phone.upload(s["id"], data, mime)

    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 400
    assert detail(client, claim)["status"] == "capturing"


def test_submit_refused_with_invalid_vin(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {**DAMAGE_ANSWERS, "vin": "1HGCV1F39LA526019"})
    capture_required(client, claim["token"], layout)
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 400 and "VIN" in r.json()["detail"]


def test_other_vehicle_flags_claim_for_a_person(client):
    claim = open_claim(client)
    answers = {**DAMAGE_ANSWERS, "other_vehicle": "Yes", "other_driver_name": "Pat Lee"}
    layout = save(client, claim["token"], answers)
    capture_required(client, claim["token"], layout)
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 200

    d = detail(client, claim)
    assert d["reason_codes"] == ["MULTI_PARTY"]
    assert d["human_review_required"] is True
    assert d["facts"]["loss_event_type"]["value"] == "MULTI_VEHICLE"
    flagged = [e for e in d["events"] if e["type"] == "claim.flagged"]
    assert flagged and flagged[0]["data"]["reason_codes"] == ["MULTI_PARTY"]


def test_something_else_submits_without_photos_and_goes_to_a_person(client):
    claim = open_claim(client)
    save(client, claim["token"], {
        "loss_type": "something_else", "what_happened": "Someone was hurt", "description": "My passenger hurt her neck.",
        "callback_phone": "+1 415 555 0101", "full_name": "Jordan Avery", "agree": True,
    })
    Phone(client, claim["token"]).upload("signature", *MEDIA["signature"])
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 200, r.text

    d = detail(client, claim)
    assert d["reason_codes"] == ["LOSS_TYPE_OUT_OF_PHOTO_LANE"]
    assert d["human_review_required"] is True
    assert d["facts"]["loss_event_type"]["value"] == "INJURY"


def test_unverified_policy_is_flagged_on_submit(client):
    claim = open_claim(client, "NOPE-1")
    layout = save(client, claim["token"], DAMAGE_ANSWERS)
    capture_required(client, claim["token"], layout)
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 200
    assert "POLICY_NOT_VERIFIED" in detail(client, claim)["reason_codes"]


@pytest.mark.parametrize("policy, expected", [("DEMO-AUTO-1001", "state prison"), ("DEMO-AUTO-1003", "civil penalty"), ("NOPE-1", "may be guilty")])
def test_fraud_warning_follows_the_policy_state(client, policy, expected):
    claim = open_claim(client, policy)
    layout = save(client, claim["token"], {"loss_type": "vehicle_damage"})
    assert expected in layout["sections"][-1]["notice"]


def test_unknown_loss_type_is_refused(client):
    claim = open_claim(client)
    r = client.put(f"/api/c/{claim['token']}/draft", json={"answers": {"loss_type": "alien_abduction"}})
    assert r.status_code == 400


def test_answers_are_frozen_after_submit(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], DAMAGE_ANSWERS)
    capture_required(client, claim["token"], layout)
    client.post(f"/api/c/{claim['token']}/submit", json={})
    r = client.put(f"/api/c/{claim['token']}/draft", json={"answers": {**DAMAGE_ANSWERS, "loss_city": "Elsewhere"}})
    assert r.status_code == 409


@pytest.mark.parametrize("field, label", [("description", "What happened, in your own words?"), ("loss_date", "What day did it happen?"), ("agree", "I confirm")])
def test_submit_refused_while_a_required_field_is_missing(client, field, label):
    claim = open_claim(client)
    answers = {k: v for k, v in DAMAGE_ANSWERS.items() if k != field}
    capture_required(client, claim["token"], save(client, claim["token"], answers))
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 400
    assert label in r.json()["detail"]


def test_conditionally_required_field_blocks_submit(client):
    claim = open_claim(client)
    answers = {**DAMAGE_ANSWERS, "driver_is_holder": "No"}  # driver_name now required, not given
    capture_required(client, claim["token"], save(client, claim["token"], answers))
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 400 and "Driver's full name" in r.json()["detail"]
