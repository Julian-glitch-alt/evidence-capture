"""T43b (tasks/t43b-design.md): the verification follows the claim type; the damage reading stays neutral."""

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from evidence_server import claim_type, consistency, consistency_prompts, damage, damage_prompts, loss_notice, photo_prompts, proposal, report, vocab
from test_loss_types import submit

GLASS = {"area": "Windshield", "damage_type": "GLASS_DAMAGE", "severity": "moderate", "age": "new", "box": [300, 200, 700, 500],
         "observed": "Crack across the windshield"}
BUMPER = {"area": "Front bumper", "damage_type": "BUMPER_DAMAGE", "severity": "moderate", "age": "new", "box": [100, 600, 900, 900],
          "observed": "Bumper cover pushed in"}
HAIL = {"area": "Roof", "damage_type": "HAIL_DENTS", "severity": "moderate", "age": "new", "box": [100, 100, 900, 500],
        "observed": "Many small round dents"}
WATER = {"area": "Interior", "damage_type": "WATER_DAMAGE", "severity": "severe", "age": "new", "box": [0, 400, 1000, 1000],
         "observed": "Mud line on the seats"}
FIRE = {"area": "Hood", "damage_type": "FIRE_DAMAGE", "severity": "severe", "age": "new", "box": [100, 100, 900, 600],
        "observed": "Burnt paint on the hood"}
UNDER = {"area": "Underneath", "damage_type": "UNDERCARRIAGE_DAMAGE", "severity": "moderate", "age": "new", "box": [200, 300, 800, 700],
         "observed": "Exhaust pipe cut on both sides"}
REAR = {"area": "Rear bumper", "damage_type": "BUMPER_DAMAGE", "severity": "minor", "age": "new", "box": [200, 450, 800, 700],
        "observed": "Rear bumper cover cracked"}


def reading(*damages, status="done"):
    rows = [{"item_id": "item-1", "slot_id": "damage_closeup", **d} for d in damages]
    return {"status": status, "damages": rows, "merged": damage.merge(rows)}


def fit(loss_type, *damages, status="done", **answers):
    return claim_type.fit({"loss_type": loss_type, **answers}, reading(*damages, status=status))


# --- the fit rule, per type ---


def test_glass_only_passes_with_glass_damage():
    row = fit("glass", GLASS)
    assert (row["check_id"], row["result"], row["reason_code"]) == ("claimtype.fit", "pass", None)


def test_glass_with_more_than_glass_is_a_mismatch():
    row = fit("glass", GLASS, BUMPER)
    assert (row["result"], row["reason_code"]) == ("fail", "TYPE_DAMAGE_MISMATCH")
    assert row["reason"] == "Damage beyond the glass is visible: bumper damage."


def test_glass_with_no_glass_damage():
    row = fit("glass", BUMPER)
    assert (row["result"], row["reason"]) == ("fail", "No glass damage is visible in the photos.")


def test_old_damage_does_not_count_against_the_type():
    assert fit("glass", GLASS, {**BUMPER, "age": "old"})["result"] == "pass"


@pytest.mark.parametrize("kind, damages, result", [
    ("Hail", [HAIL], "pass"),
    ("Hail", [BUMPER], "fail"),
    ("Flood or high water", [WATER], "pass"),
    ("Flood or high water", [{**WATER, "damage_type": "INTERIOR_DAMAGE"}], "pass"),
    ("Flood or high water", [HAIL], "fail"),
])
def test_weather_follows_the_kind(kind, damages, result):
    assert fit("weather", *damages, weather_kind=kind)["result"] == result


def test_weather_reasons():
    assert fit("weather", BUMPER, weather_kind="Hail")["reason"] == "No hail dents are visible in the photos."
    assert fit("weather", BUMPER, weather_kind="Flood or high water")["reason"] == "No water damage is visible in the photos."


@pytest.mark.parametrize("kind", ["Fallen tree or branch", "Wind or another storm"])
def test_other_weather_has_no_rule(kind):
    assert fit("weather", BUMPER, weather_kind=kind) is None


def test_fire_needs_fire_damage():
    assert fit("fire", FIRE)["result"] == "pass"
    row = fit("fire", BUMPER)
    assert (row["result"], row["reason_code"], row["reason"]) == ("fail", "TYPE_DAMAGE_MISMATCH", "No fire damage is visible in the photos.")


def test_parts_theft_is_unclear_when_the_spot_is_not_seen():
    assert fit("parts_theft", UNDER)["result"] == "pass"
    assert fit("parts_theft", {**BUMPER, "area": "Underneath"})["result"] == "pass"
    row = fit("parts_theft", BUMPER)
    assert (row["result"], row["reason_code"], row["reason"]) == (
        "unclear", "TYPE_DAMAGE_NOT_SEEN", "The photos do not show where the part was taken.")


def test_animal_strike_only_at_the_rear():
    assert fit("animal", BUMPER)["result"] == "pass"
    assert fit("animal", REAR, BUMPER)["result"] == "pass"
    row = fit("animal", REAR)
    assert (row["result"], row["reason"]) == ("fail", "All damage is at the rear; animal strikes are usually at the front or sides.")


@pytest.mark.parametrize("loss_type", ["collision_vehicle", "hit_and_run", "single_vehicle", "vandalism", "vehicle_theft",
                                       "something_else", "vehicle_damage"])
def test_types_without_a_rule(loss_type):
    assert fit(loss_type, BUMPER) is None


@pytest.mark.parametrize("status", ["none", "pending", "failed"])
def test_nothing_read_means_no_row(status):
    assert fit("glass", status=status) is None


def test_partial_reading_is_used():
    assert fit("glass", GLASS, status="partial")["result"] == "pass"


def test_codes_causes_and_levels():
    assert {"TYPE_DAMAGE_MISMATCH", "TYPE_DAMAGE_NOT_SEEN"} <= vocab.REASON_CODES
    assert proposal.ROOT_CAUSES["TYPE_DAMAGE_MISMATCH"] == "claim_type"
    assert proposal.ROOT_CAUSES["TYPE_DAMAGE_NOT_SEEN"] == "claim_type"
    assert "claim_type" not in proposal.MODEL_ONLY


# --- two more areas, and the reading stays neutral ---


def test_interior_and_underneath_are_areas():
    assert {"Interior", "Underneath"} <= set(photo_prompts.AREAS)
    assert '"Interior"' in damage_prompts.DAMAGE and '"Underneath"' in damage_prompts.DAMAGE
    assert damage_prompts.VERSION == "damage-prompts/v3"
    options = next(f for f in loss_notice.template()["sections"]["damage"]["fields"] if f["id"] == "damaged_areas")["options"]
    assert options[-2:] == ["Interior", "Underneath"]


def test_the_damage_reading_is_never_told_the_claim_type():
    for word in ("claim type", "hail claim", "glass claim", "loss_type"):
        assert word not in damage_prompts.DAMAGE.lower()


# --- the statement check knows the type ---


def test_story_block_names_the_type_and_its_details():
    block = consistency.story_block({"loss_type": "weather", "weather_kind": "Hail", "cause": "Weather"}, reading(HAIL))
    lines = block.splitlines()
    assert lines[1] == "Claim type: Falling object, hail or flood"
    assert lines[2] == "Details: Hail"
    glass = consistency.story_block({"loss_type": "glass", "glass_which": ["Windshield"], "glass_kind": "A crack",
                                     "glass_cause": "A rock or road debris"}, reading(GLASS))
    assert "Details: Windshield; A crack; A rock or road debris" in glass.splitlines()


def test_story_block_without_details_or_for_the_old_type():
    block = consistency.story_block({"loss_type": "collision_vehicle", "cause": "Hit another vehicle"}, reading(BUMPER))
    assert "Claim type: Crash with another vehicle" in block and "Details:" not in block
    old = consistency.story_block({"loss_type": "vehicle_damage", "cause": "Hit an object"}, reading(BUMPER))
    assert "Claim type:" not in old and "Details:" not in old


def test_story_prompt_explains_each_type():
    assert consistency_prompts.VERSION == "consistency-prompts/v3"
    text = consistency_prompts.STORY
    assert "What each claim type usually looks like" in text
    for word in ("hail", "flood", "glass", "animal", "fire", "vandalism"):
        assert word in text.lower()


# --- what to verify ---


def test_to_verify_per_type():
    weather = loss_notice.to_verify({"loss_type": "weather", "weather_kind": "Hail", "loss_city": "San Jose",
                                     "loss_state": "CA", "loss_date": "2026-10-27"})
    assert "NOAA Storm Events: hail near San Jose, CA on 2026-10-27: confirm the event" in weather
    fire = loss_notice.to_verify({"loss_type": "fire", "fire_start": "Engine", "fire_dept": "Yes", "police_contacted": "No"})
    assert "Fire department report: confirm with the department" in fire
    assert not any("Fire department" in x for x in loss_notice.to_verify({"loss_type": "fire", "fire_dept": "No"}))
    theft = loss_notice.to_verify({"loss_type": "vehicle_theft", "vin": "1HGCV1F39LA526018", "police_contacted": "No"})
    assert "NICB VINCheck for VIN 1HGCV1F39LA526018: confirm no earlier theft or salvage record" in theft


# --- through the API ---


def flags(d):
    return {f["reason_code"]: f for f in d["proposal"]["flags"]}


def test_glass_claim_showing_a_bumper_is_flagged_for_review(client, model):
    model.set("damage", {"is_vehicle": True, "damages": [GLASS, BUMPER], "unsure": False})
    d = submit(client, "glass", glass_which=["Windshield"], glass_kind="A crack", glass_cause="A rock or road debris",
               damaged_areas=["Windshield"])
    row = next(c for c in d["checks"] if c["check_id"] == "claimtype.fit")
    assert (row["result"], row["reason_code"]) == ("fail", "TYPE_DAMAGE_MISMATCH")
    flag = flags(d)["TYPE_DAMAGE_MISMATCH"]
    assert (flag["level"], flag["cause"]) == ("review", "claim_type")
    assert d["proposal"]["lane"] == "adjuster_review"
    assert d["claim_type"] == {"id": "glass", "label": "Glass only"}


def test_glass_claim_with_glass_damage_has_no_type_flag(client, model):
    model.set("damage", {"is_vehicle": True, "damages": [GLASS], "unsure": False})
    d = submit(client, "glass", glass_which=["Windshield"], glass_kind="A crack", glass_cause="A rock or road debris",
               damaged_areas=["Windshield"])
    row = next(c for c in d["checks"] if c["check_id"] == "claimtype.fit")
    assert row["result"] == "pass"
    assert "TYPE_DAMAGE_MISMATCH" not in flags(d)


def test_a_stolen_car_has_no_statement_check(client, model):
    d = submit(client, "vehicle_theft", last_seen_place="Driveway at home", keys_count="2",
               keys_all="Yes, I have all of them", car_locked="Yes", ownership="I own it outright")
    assert not [c for c in model.calls if c["slot"] == "story"]
    assert "STORY_NOT_CHECKED" not in flags(d)
    assert not [c for c in d["checks"] if c["check_id"] == "claimtype.fit"]
    assert d["claim_type"]["label"] == "Car stolen"


def test_report_carries_the_claim_type(client, model):
    model.set("damage", {"is_vehicle": True, "damages": [HAIL], "unsure": False})
    d = submit(client, "weather", weather_kind="Hail")
    built = report.build(d, "Demo Mutual Insurance", datetime(2026, 10, 30, 14, tzinfo=UTC), ZoneInfo("America/New_York"))
    assert built["claim_type"] == {"id": "weather", "label": "Falling object, hail or flood"}
    assert next(c for c in d["checks"] if c["check_id"] == "claimtype.fit")["result"] == "pass"


# --- T44: presence rules count old damage (a burnt wreck reads as old) ---


def test_old_damage_of_the_right_kind_still_shows_the_type():
    old_fire = {**FIRE, "age": "old"}
    assert fit("fire", old_fire)["result"] == "pass"
    assert fit("weather", {**HAIL, "age": "old"}, weather_kind="Hail")["result"] == "pass"
    assert fit("glass", {**GLASS, "age": "old"})["result"] == "pass"
    assert fit("animal", {**BUMPER, "age": "old"})["result"] == "pass"
    assert fit("parts_theft", {**UNDER, "age": "old"})["result"] == "pass"


def test_old_damage_beyond_the_glass_still_does_not_count():
    assert fit("glass", GLASS, {**BUMPER, "age": "old"})["result"] == "pass"
