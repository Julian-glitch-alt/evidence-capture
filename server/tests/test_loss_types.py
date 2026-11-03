"""T43a (tasks/t43a-design.md): ten US auto claim types, each asking what a carrier asks for it."""

import pytest

from conftest import Phone
from evidence_server import coverage, loss_notice
from test_coverage import P1001
from test_loss_notice import MEDIA, VALID_VIN, capture_required, detail, open_claim, save

TYPES = ["collision_vehicle", "hit_and_run", "single_vehicle", "animal", "vandalism", "glass", "weather",
         "parts_theft", "fire", "vehicle_theft"]
GROUPS = {"collision_vehicle": "Crash", "hit_and_run": "Crash", "single_vehicle": "Crash", "animal": "Crash",
          "vandalism": "Damage while parked", "glass": "Damage while parked", "weather": "Damage while parked",
          "parts_theft": "Damage while parked", "fire": "Damage while parked", "vehicle_theft": "Stolen",
          "other_damage": "Something else", "something_else": "Something else"}

# type, extra answers -> section ids in order
SECTIONS = [
    ("collision_vehicle", {}, ["policy", "loss", "other_party", "vehicle", "driver", "damage", "witnesses", "documents", "declaration"]),
    ("hit_and_run", {"car_was": "Parked"}, ["policy", "loss", "details", "other_party", "vehicle", "damage", "witnesses", "documents", "declaration"]),
    ("hit_and_run", {"car_was": "Being driven"}, ["policy", "loss", "details", "other_party", "vehicle", "driver", "damage", "witnesses", "documents", "declaration"]),
    ("single_vehicle", {}, ["policy", "loss", "details", "vehicle", "driver", "damage", "other_party", "witnesses", "documents", "declaration"]),
    ("animal", {}, ["policy", "loss", "details", "vehicle", "driver", "damage", "other_party", "witnesses", "documents", "declaration"]),
    ("vandalism", {}, ["policy", "loss", "details", "vehicle", "damage", "witnesses", "documents", "declaration"]),
    ("parts_theft", {}, ["policy", "loss", "details", "vehicle", "damage", "witnesses", "documents", "declaration"]),
    ("fire", {}, ["policy", "loss", "details", "vehicle", "damage", "witnesses", "documents", "declaration"]),
    ("glass", {}, ["policy", "loss", "details", "vehicle", "damage", "documents", "declaration"]),
    ("weather", {}, ["policy", "loss", "details", "vehicle", "damage", "documents", "declaration"]),
    ("vehicle_theft", {}, ["policy", "loss", "details", "witnesses", "documents", "declaration"]),
    ("other_damage", {}, ["policy", "loss", "vehicle", "driver", "damage", "other_party", "witnesses", "documents", "declaration"]),
]

FULL_PHOTOS = {"plate", "vin_photo", "odometer", "driver_license", "walkaround", "damage_closeup", "damage_wide", "signature"}
NO_DRIVER = FULL_PHOTOS - {"driver_license"}
# type, extra answers -> (every slot shown, the required ones)
SLOTS = [
    ("collision_vehicle", {}, FULL_PHOTOS | {"documents"}, FULL_PHOTOS),
    ("hit_and_run", {"car_was": "Parked"}, NO_DRIVER | {"documents"}, NO_DRIVER),
    ("hit_and_run", {"car_was": "Being driven"}, FULL_PHOTOS | {"documents"}, FULL_PHOTOS),
    ("single_vehicle", {}, FULL_PHOTOS | {"documents"}, FULL_PHOTOS),
    ("animal", {}, FULL_PHOTOS | {"documents"}, FULL_PHOTOS),
    ("vandalism", {}, NO_DRIVER | {"documents"}, NO_DRIVER),
    ("fire", {}, NO_DRIVER | {"documents"}, NO_DRIVER),
    ("weather", {}, NO_DRIVER | {"documents"}, NO_DRIVER),
    ("parts_theft", {}, NO_DRIVER | {"documents"}, NO_DRIVER - {"walkaround"}),
    ("glass", {}, {"plate", "vin_photo", "odometer", "damage_closeup", "damage_wide", "documents", "signature"},
     {"plate", "vin_photo", "damage_closeup", "damage_wide", "signature"}),
    ("vehicle_theft", {}, {"keys", "documents", "signature"}, {"keys", "signature"}),
]

DETAILS = {
    "hit_and_run": ["car_was"],
    "single_vehicle": ["hit_what"],
    "animal": ["animal"],
    "vandalism": ["vandalism_kind"],
    "glass": ["glass_which", "glass_kind", "glass_cause"],
    "weather": ["weather_kind"],
    "parts_theft": ["parts_taken"],
    "fire": ["fire_start", "fire_dept"],
    "vehicle_theft": ["last_seen_place", "last_seen_time", "keys_count", "keys_all", "car_locked", "ownership", "items_in_car"],
}

COMMON = {"loss_date": "2026-10-27", "loss_city": "San Jose", "loss_state": "CA", "description": "It happened in a parking lot.",
          "police_contacted": "No", "drivable": "Yes", "driver_is_holder": "Yes", "damaged_areas": ["Front left"],
          "prior_damage": "No", "injuries": "No", "full_name": "Jordan Avery", "agree": True}


def layout_for(client, loss_type, **extra):
    claim = open_claim(client)
    return claim, save(client, claim["token"], {"loss_type": loss_type, **extra})


def sections(layout):
    return {s["id"]: s for s in layout["sections"]}


def fields(layout, section):
    return [f["id"] for f in sections(layout)[section]["fields"]]


def slots(layout):
    return {slot["id"]: slot for s in layout["sections"] for slot in s["slots"]}


def submit(client, loss_type, **extra):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": loss_type, **COMMON, **extra})
    assert layout["complete"] or all(m in {s["title"] for s in slots(layout).values()} for m in layout["missing"]), layout["missing"]
    capture_required(client, claim["token"], layout)
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 200, r.text
    return detail(client, claim)


# --- the picker ---


def test_the_picker_lists_ten_types_in_groups_and_hides_the_old_one(client):
    claim = open_claim(client)
    layout = client.get(f"/api/c/{claim['token']}").json()["layout"]
    assert [t["id"] for t in layout["loss_types"]] == TYPES + ["other_damage", "something_else"]
    assert {t["id"]: t["group"] for t in layout["loss_types"]} == GROUPS
    assert all(t["label"] and t["description"] for t in layout["loss_types"])


def test_the_old_type_still_works_when_a_draft_names_it(client):
    _, layout = layout_for(client, "vehicle_damage")
    assert "cause" in fields(layout, "loss") and "other_vehicle" in fields(layout, "other_party")


def test_something_else_no_longer_offers_theft_fire_or_weather(client):
    _, layout = layout_for(client, "something_else")
    what = next(f for f in sections(layout)["contact"]["fields"] if f["id"] == "what_happened")
    assert what["options"] == ["Someone was hurt", "Something else"]


# --- sections, questions and photos per type ---


@pytest.mark.parametrize("loss_type, extra, expected", SECTIONS)
def test_sections_per_type(client, loss_type, extra, expected):
    _, layout = layout_for(client, loss_type, **extra)
    assert [s["id"] for s in layout["sections"]] == expected


@pytest.mark.parametrize("loss_type, extra, shown, required", SLOTS)
def test_photos_per_type(client, loss_type, extra, shown, required):
    _, layout = layout_for(client, loss_type, **extra)
    s = slots(layout)
    assert set(s) == shown
    assert {k for k, v in s.items() if v["required"]} == required


@pytest.mark.parametrize("loss_type", list(DETAILS))
def test_type_questions_live_in_details(client, loss_type):
    _, layout = layout_for(client, loss_type)
    assert fields(layout, "details") == DETAILS[loss_type]


def test_new_types_never_ask_the_cause_again(client):
    for loss_type in TYPES:
        _, layout = layout_for(client, loss_type)
        assert "cause" not in fields(layout, "loss") and "other_driver_left" not in fields(layout, "loss"), loss_type


def test_police_questions_except_for_glass_and_weather(client):
    for loss_type in TYPES:
        _, layout = layout_for(client, loss_type)
        asked = "police_contacted" in fields(layout, "loss")
        assert asked == (loss_type not in ("glass", "weather")), loss_type


def test_other_party_follows_the_type(client):
    _, layout = layout_for(client, "collision_vehicle")
    assert fields(layout, "other_party") == [
        "other_driver_name", "other_driver_phone", "other_plate", "other_vehicle_desc", "other_insurer", "other_policy_no", "injuries"]
    _, layout = layout_for(client, "hit_and_run", car_was="Parked")
    assert fields(layout, "other_party") == ["other_plate", "other_vehicle_desc", "injuries"]
    _, layout = layout_for(client, "single_vehicle")
    assert fields(layout, "other_party") == ["injuries"]


def test_conditional_follow_ups(client):
    _, layout = layout_for(client, "vandalism", vandalism_kind=["Scratched or keyed"])
    assert "items_stolen" not in fields(layout, "details")
    _, layout = layout_for(client, "vandalism", vandalism_kind=["Scratched or keyed", "Broken into"])
    assert "items_stolen" in fields(layout, "details")
    _, layout = layout_for(client, "weather", weather_kind="Hail")
    assert "water_level" not in fields(layout, "details")
    _, layout = layout_for(client, "weather", weather_kind="Flood or high water")
    level = next(f for f in sections(layout)["details"]["fields"] if f["id"] == "water_level")
    assert level["required"] is True
    _, layout = layout_for(client, "vehicle_theft", ownership="It has a loan")
    lender = next(f for f in sections(layout)["details"]["fields"] if f["id"] == "lender")
    assert lender["required"] is True
    _, layout = layout_for(client, "vehicle_theft", ownership="I own it outright")
    assert "lender" not in fields(layout, "details")


def test_copy_per_type(client):
    _, layout = layout_for(client, "glass")
    assert sections(layout)["details"]["title"] == "About the glass"
    assert slots(layout)["damage_closeup"]["title"] == "Glass close-up"
    assert "coin" in slots(layout)["damage_closeup"]["instructions"]
    assert slots(layout)["damage_wide"]["title"] == "The whole window"
    _, layout = layout_for(client, "collision_vehicle")
    assert slots(layout)["documents"]["instructions"] == (  # T56: a photo of a paper, never an upload
        "Take a photo of a police report slip, a repair estimate or a towing receipt. You can skip this.")
    assert slots(layout)["documents"]["title"] == "Photo of a paper (optional)"
    assert sections(layout)["documents"]["title"] == "Photos of papers"
    _, layout = layout_for(client, "vehicle_theft")
    assert sections(layout)["details"]["title"] == "About the theft"


def test_form_fixes_from_the_live_test(client):
    _, layout = layout_for(client, "collision_vehicle")
    assert "odometer_reading" not in fields(layout, "vehicle")
    witnesses = sections(layout)["witnesses"]["fields"][0]
    assert "(optional)" not in witnesses["label"]
    areas = next(f for f in sections(layout)["damage"]["fields"] if f["id"] == "damaged_areas")
    assert "driver's side" in areas["hint"]
    description = next(f for f in sections(layout)["loss"]["fields"] if f["id"] == "description")
    assert description["hint"] == "A few sentences are fine: where you were, what happened and which part of the car."


# --- the VIN comes from the policy ---


def test_vin_is_prefilled_from_a_single_vehicle_policy(client):
    _, layout = layout_for(client, "glass")
    vin = next(f for f in sections(layout)["vehicle"]["fields"] if f["id"] == "vin")
    assert vin["value"] == VALID_VIN and vin["prefilled"] is True
    assert vin["note"].startswith("From your policy")
    assert "VIN" not in layout["missing"]


def test_a_typed_vin_wins_over_the_policy(client):
    other = "1FTFW1E5XNA016613"
    claim, layout = layout_for(client, "glass", vin=other)
    vin = next(f for f in sections(layout)["vehicle"]["fields"] if f["id"] == "vin")
    assert vin["value"] == other and not vin.get("prefilled")
    assert client.get(f"/api/c/{claim['token']}").json()["answers"]["vin"] == other


def test_no_policy_means_no_prefill(client):
    claim = open_claim(client, "NOPE-1")
    layout = save(client, claim["token"], {"loss_type": "glass"})
    vin = next(f for f in sections(layout)["vehicle"]["fields"] if f["id"] == "vin")
    assert not vin.get("value") and "VIN" in layout["missing"]


# --- implied answers ---


def test_implied_answers_are_stored_with_the_draft(client):
    claim, _ = layout_for(client, "collision_vehicle", cause="Glass", other_vehicle="No")
    answers = client.get(f"/api/c/{claim['token']}").json()["answers"]
    assert (answers["cause"], answers["other_vehicle"], answers["other_driver_left"]) == ("Hit another vehicle", "Yes", "No")
    assert answers["vin"] == VALID_VIN


def test_switching_type_leaves_nothing_stale(client):
    claim, _ = layout_for(client, "hit_and_run", car_was="Parked")
    save(client, claim["token"], {"loss_type": "vandalism"})
    answers = client.get(f"/api/c/{claim['token']}").json()["answers"]
    assert answers["cause"] == "Vandalism" and answers["other_vehicle"] == "No"
    assert "other_driver_left" not in answers


def test_effective_is_a_pure_function():
    out = loss_notice.effective({"loss_type": "single_vehicle", "cause": "Fire", "hit_what": "A pole or sign"}, None)
    assert (out["cause"], out["other_vehicle"], out["hit_what"]) == ("Hit an object", "No", "A pole or sign")
    assert "vin" not in out
    assert loss_notice.effective({"loss_type": "vehicle_damage", "cause": "Hit an object"}, None)["cause"] == "Hit an object"


# --- coverage ---


@pytest.mark.parametrize("loss_type, needed", [
    ("collision_vehicle", "collision"), ("hit_and_run", "collision"), ("single_vehicle", "collision"),
    ("animal", "comprehensive"), ("vandalism", "comprehensive"), ("glass", "comprehensive"),
    ("weather", "comprehensive"), ("parts_theft", "comprehensive"), ("fire", "comprehensive"),
    ("vehicle_theft", "comprehensive"),
])
def test_coverage_per_type(loss_type, needed):
    rows = {r["check_id"]: r for r in coverage.check(P1001, True, {"loss_type": loss_type, "loss_date": "2026-10-20"})}
    assert rows["coverage.coverage_bought"]["value"] == needed
    assert rows["coverage.deductible"]["value"] == (500 if needed == "collision" else 250)


@pytest.mark.parametrize("loss_type, extra, has_driver_row", [
    ("collision_vehicle", {"driver_is_holder": "Yes"}, True),
    ("hit_and_run", {"car_was": "Being driven", "driver_is_holder": "Yes"}, True),
    ("hit_and_run", {"car_was": "Parked"}, False),
    ("vandalism", {}, False), ("glass", {}, False), ("weather", {}, False), ("vehicle_theft", {}, False),
])
def test_no_driver_check_when_nobody_was_driving(loss_type, extra, has_driver_row):
    rows = {r["check_id"] for r in coverage.check(P1001, True, {"loss_type": loss_type, "loss_date": "2026-10-20", **extra})}
    assert ("coverage.driver_listed" in rows) == has_driver_row


# --- what submit records ---


def test_collision_with_another_vehicle(client):
    d = submit(client, "collision_vehicle")
    assert d["facts"]["loss_event_type"]["value"] == "MULTI_VEHICLE"
    assert "MULTI_PARTY" in d["reason_codes"]
    assert d["answers"]["cause"] == "Hit another vehicle"


def test_parked_hit_and_run_without_a_report_number(client):
    d = submit(client, "hit_and_run", car_was="Parked", police_contacted="Yes", police_agency="San Jose Police Department")
    assert d["facts"]["loss_event_type"]["value"] == "HIT_AND_RUN"
    assert {"POLICE_REPORT_NEEDED", "MULTI_PARTY"} <= set(d["reason_codes"])
    assert (d["answers"]["other_vehicle"], d["answers"]["other_driver_left"]) == ("Yes", "Yes")
    assert "coverage.driver_listed" not in {c["check_id"] for c in d["checks"]}


def test_glass_claim_is_clean_and_comprehensive(client):
    d = submit(client, "glass", glass_which=["Windshield"], glass_kind="A crack", glass_cause="A rock or road debris",
               damaged_areas=["Windshield"])
    assert d["facts"]["loss_event_type"]["value"] == "GLASS"
    assert "POLICE_REPORT_NEEDED" not in d["reason_codes"]
    bought = next(c for c in d["checks"] if c["check_id"] == "coverage.coverage_bought")
    assert bought["value"] == "comprehensive"


def test_flood_up_to_the_seats_may_be_a_total_loss(client):
    d = submit(client, "weather", weather_kind="Flood or high water", water_level="Up to the seats")
    assert d["facts"]["loss_event_type"]["value"] == "FLOOD"
    assert "POSSIBLE_TOTAL_LOSS" in d["reason_codes"]


@pytest.mark.parametrize("weather_kind, event", [("Hail", "HAIL"), ("Fallen tree or branch", "FALLING_OBJECT"),
                                                 ("Wind or another storm", "WEATHER")])
def test_weather_event_types(client, weather_kind, event):
    d = submit(client, "weather", weather_kind=weather_kind)
    assert d["facts"]["loss_event_type"]["value"] == event


@pytest.mark.parametrize("loss_type, extra", [
    ("vandalism", {"vandalism_kind": ["Scratched or keyed"]}),
    ("parts_theft", {"parts_taken": ["Catalytic converter"]}),
])
def test_police_report_needed_for_vandalism_and_parts_theft(client, loss_type, extra):
    d = submit(client, loss_type, **extra)
    assert "POLICE_REPORT_NEEDED" in d["reason_codes"]


def test_stolen_car_goes_to_a_person_and_flags_missing_keys(client):
    d = submit(client, "vehicle_theft", last_seen_place="Driveway at home", keys_count="2",
               keys_all="No, some are missing", car_locked="Yes", ownership="I own it outright")
    assert d["facts"]["loss_event_type"]["value"] == "THEFT"
    assert {"LOSS_TYPE_OUT_OF_PHOTO_LANE", "KEYS_MISSING", "POLICE_REPORT_NEEDED"} <= set(d["reason_codes"])
    assert d["answers"]["vin"] == VALID_VIN  # the stolen car is the insured car
    listed = next(c for c in d["checks"] if c["check_id"] == "coverage.vehicle_listed")
    assert listed["result"] == "pass"


def test_keys_missing_is_a_known_cause_with_a_weight():
    from evidence_server import proposal, vocab
    assert "KEYS_MISSING" in vocab.REASON_CODES
    assert proposal.ROOT_CAUSES["KEYS_MISSING"] == "keys"


# --- the claimant sees the insurer's claim number ---


def test_session_state_carries_the_insurer_claim_number(client):
    claim = open_claim(client)
    assert client.get(f"/api/c/{claim['token']}").json()["insurer_claim_number"] is None
    from test_integration_api import auth, order
    r = order(client, number="DM-2026-0777")
    assert r.status_code == 201, r.text
    token = r.json()["capture_url"].rstrip("/").split("/")[-1]
    assert client.get(f"/api/c/{token}").json()["insurer_claim_number"] == "DM-2026-0777"
