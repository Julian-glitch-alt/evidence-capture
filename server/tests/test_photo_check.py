"""T34 photo check, the pure part (contract: tasks/t34-design.md): the model reads, this code decides."""

import copy
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from conftest import GOOD_READS
from evidence_server import photo_check, photo_prompts, vocab
from test_loss_notice import DAMAGE_ANSWERS

SEED = json.loads((Path(__file__).parents[1] / "src/evidence_server/seed/policies.json").read_text())
P1001 = {p["policy_no"]: p for p in SEED["policies"]}["DEMO-AUTO-1001"]  # 2020 Honda Accord, DEMO101, Jordan Avery
TODAY = date(2026, 10, 28)
NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)

HINT_UNREADABLE_VIN = "We could not read the VIN. Get close, avoid glare, and keep all 17 characters in the frame."


def ctx(policy=P1001, **answers):
    return {"policy": policy, "answers": {**DAMAGE_ANSWERS, **answers}, "today": TODAY}


def good(slot):
    return copy.deepcopy(GOOD_READS[slot])


def reads(slot, **changes):
    """The good answer(s) of a slot with some keys changed (on every read)."""
    return [{**r, **changes} for r in good(slot)]


def decide(slot, readings, c=None, attempt=1):
    return photo_check.decide(slot, readings, c or ctx(), attempt)


def vin_reads(first, second=None):
    second = first if second is None else second
    return [
        {"is_vin": True, "vin": first, "unsure": False, "problem": "none"},
        {"is_vin": True, "characters": list(second), "unsure": False, "problem": "none"},
    ]


# --- the clean claim ---


@pytest.mark.parametrize("slot", photo_check.CHECKED_SLOTS)
def test_good_shot_passes(slot):
    r = decide(slot, good(slot))
    assert r["result"] == "pass"
    assert r["reason_code"] is None and r["hint"] is None
    assert r["reason"]


def test_checked_slots_and_prompts_line_up():
    assert set(photo_check.CHECKED_SLOTS) == set(photo_prompts.READS)
    assert len(photo_prompts.READS["vin_photo"]) == 2
    assert photo_check.AREAS == photo_prompts.AREAS


def test_same_inputs_give_the_same_output():
    assert decide("vin_photo", good("vin_photo")) == decide("vin_photo", good("vin_photo"))


def test_every_reason_code_is_in_the_vocabulary():
    codes = {
        "PHOTO_UNCHECKED", "PHOTO_WRONG_SUBJECT", "PHOTO_UNREADABLE", "PHOTO_READS_DISAGREE", "VIN_READ_INVALID",
        "VIN_MISMATCH", "PLATE_MISMATCH", "ODOMETER_IMPLAUSIBLE", "LICENSE_NAME_MISMATCH", "LICENSE_EXPIRED",
        "DAMAGE_NOT_VISIBLE", "DAMAGE_PART_NOT_FOUND", "DAMAGE_AREA_MISMATCH", "PHOTO_POOR_QUALITY",
    }
    assert codes <= vocab.REASON_CODES


# --- common rules (P3) ---


@pytest.mark.parametrize("slot", photo_check.CHECKED_SLOTS)
def test_no_reading_is_unchecked_never_retake(slot):
    r = decide(slot, None)
    assert (r["result"], r["reason_code"], r["hint"], r["value"]) == ("unchecked", "PHOTO_UNCHECKED", None, None)
    assert r["reason"] == "The photo could not be checked automatically."


@pytest.mark.parametrize(
    "slot, readings",
    [
        ("plate", []),
        ("plate", ["not a dict"]),
        ("plate", [{"is_plate": True}]),  # keys missing
        ("plate", [{"is_plate": "yes", "text": "DEMO101", "state": None, "unsure": False, "problem": "none"}]),
        ("vin_photo", [GOOD_READS["vin_photo"][0]]),  # only one read
        ("vin_photo", [GOOD_READS["vin_photo"][0], GOOD_READS["vin_photo"][0]]),  # read 2 has no "characters"
        ("odometer", [{"is_odometer": True, "reading": "56947", "unit": "mi", "unsure": False, "problem": "none"}]),
        ("odometer", [{"is_odometer": True, "reading": True, "unit": "mi", "unsure": False, "problem": "none"}]),
        ("damage_wide", [{"is_vehicle": True, "damage_visible": True, "parts": "Rear bumper", "unsure": False, "problem": "none"}]),
    ],
)
def test_wrong_shape_is_unchecked(slot, readings):
    assert decide(slot, readings)["result"] == "unchecked"


def test_integral_float_reading_is_accepted():
    r = decide("odometer", reads("odometer", reading=56947.0))
    assert (r["result"], r["value"]) == ("pass", 56947)


@pytest.mark.parametrize(
    "slot, key, hint",
    [
        ("plate", "is_plate", "This does not look like a license plate. Photograph the plate on this vehicle."),
        ("vin_photo", "is_vin", "This does not look like a VIN. Photograph the VIN at the bottom of the windshield or on the driver's door frame."),
        ("odometer", "is_odometer", "This does not look like the odometer. Switch on the ignition and photograph the mileage."),
        ("driver_license", "is_license", "This does not look like a driver's license. Photograph the front of the license."),
        ("damage_closeup", "is_vehicle", "We could not see a vehicle in this photo. Photograph the damaged vehicle."),
        ("damage_wide", "is_vehicle", "We could not see a vehicle in this photo. Photograph the damaged vehicle."),
    ],
)
def test_wrong_subject_asks_for_a_retake(slot, key, hint):
    r = decide(slot, reads(slot, **{key: False}))
    assert (r["result"], r["reason_code"], r["hint"], r["value"]) == ("retake", "PHOTO_WRONG_SUBJECT", hint, None)


@pytest.mark.parametrize(
    "problem, hint",
    [
        ("blurry", "The photo is blurry. Hold the phone still and tap the screen to focus."),
        ("too_dark", "The photo is too dark. Move to a brighter spot or turn on more light."),
        ("glare", "There is glare on the photo. Tilt the phone so the light does not reflect."),
        ("too_far", "Move closer so it fills more of the frame."),
        ("cut_off", "Part of it is cut off. Keep all of it inside the frame."),
        ("other", "We could not read the plate. Photograph it straight on so every character is sharp."),
        ("none", "We could not read the plate. Photograph it straight on so every character is sharp."),
        ("smudged", "We could not read the plate. Photograph it straight on so every character is sharp."),
    ],
)
def test_unsure_reading_asks_for_a_retake_with_a_concrete_hint(problem, hint):
    r = decide("plate", reads("plate", unsure=True, problem=problem))
    assert (r["result"], r["reason_code"], r["hint"]) == ("retake", "PHOTO_UNREADABLE", hint)


@pytest.mark.parametrize(
    "slot, key",
    [("plate", "text"), ("vin_photo", "vin"), ("odometer", "reading"), ("driver_license", "name")],
)
def test_null_value_is_unreadable(slot, key):
    readings = good(slot)
    readings[0][key] = None
    r = decide(slot, readings)
    assert (r["result"], r["reason_code"]) == ("retake", "PHOTO_UNREADABLE")


def test_unreadable_vin_on_the_second_read_only():
    readings = good("vin_photo")
    readings[1]["characters"] = None
    r = decide("vin_photo", readings)
    assert (r["result"], r["reason_code"], r["hint"]) == ("retake", "PHOTO_UNREADABLE", HINT_UNREADABLE_VIN)


# --- clear photos only, like KYC (T34b) ---

HINT_BLURRY = "The photo is blurry. Hold the phone still and tap the screen to focus."
HINT_DARK = "The photo is too dark. Move to a brighter spot or turn on more light."
HINT_GLARE = "There is glare on the photo. Tilt the phone so the light does not reflect."


@pytest.mark.parametrize("slot", ["plate", "vin_photo", "odometer", "driver_license"])
@pytest.mark.parametrize("problem, hint, words", [
    ("blurry", HINT_BLURRY, "blurry"), ("too_dark", HINT_DARK, "too dark"), ("glare", HINT_GLARE, "affected by glare"),
])
def test_readable_but_unclear_text_photo_is_retaken(slot, problem, hint, words):
    r = decide(slot, reads(slot, problem=problem))
    assert (r["result"], r["reason_code"], r["hint"], r["value"]) == ("retake", "PHOTO_POOR_QUALITY", hint, None)
    assert r["reason"] == f"The photo could be read but is {words}, so the reading is not trusted."
    assert r["inputs"] == {"photo.problems": [problem] * len(good(slot))}


@pytest.mark.parametrize("slot", ["damage_closeup", "damage_wide"])
def test_unclear_damage_photo_is_retaken_but_glare_on_paint_is_not(slot):
    assert decide(slot, reads(slot, problem="blurry"))["reason_code"] == "PHOTO_POOR_QUALITY"
    assert decide(slot, reads(slot, problem="too_dark"))["hint"] == HINT_DARK
    assert decide(slot, reads(slot, problem="glare"))["result"] == "pass"


@pytest.mark.parametrize("problem", ["too_far", "cut_off", "other", "none"])
def test_other_problems_do_not_block_a_good_reading(problem):
    assert decide("plate", reads("plate", problem=problem))["result"] == "pass"


def test_one_unclear_vin_read_is_enough_and_the_first_unclear_read_gives_the_hint():
    readings = good("vin_photo")
    readings[1]["problem"] = "glare"
    r = decide("vin_photo", readings)
    assert (r["reason_code"], r["hint"], r["inputs"]) == ("PHOTO_POOR_QUALITY", HINT_GLARE, {"photo.problems": ["none", "glare"]})
    readings[0]["problem"] = "blurry"
    assert decide("vin_photo", readings)["hint"] == HINT_BLURRY


def test_unclear_photo_comes_after_wrong_subject_and_unreadable():
    assert decide("plate", reads("plate", is_plate=False, problem="blurry"))["reason_code"] == "PHOTO_WRONG_SUBJECT"
    assert decide("plate", reads("plate", unsure=True, problem="blurry"))["reason_code"] == "PHOTO_UNREADABLE"


def test_unclear_photo_is_judged_before_the_values():
    # A blurry plate that reads as another plate: the photo is retaken for the blur, not compared.
    assert decide("plate", reads("plate", text="XYZ999", problem="blurry"))["reason_code"] == "PHOTO_POOR_QUALITY"


def test_third_unclear_shot_is_flagged_for_the_adjuster():
    r = decide("odometer", reads("odometer", problem="too_dark"), attempt=3)
    assert (r["result"], r["reason_code"], r["hint"]) == ("flagged", "PHOTO_POOR_QUALITY", None)


# --- retake limit (P4) ---


@pytest.mark.parametrize("attempt, expected", [(1, "retake"), (2, "retake"), (3, "flagged"), (4, "flagged")])
def test_third_shot_that_still_fails_is_flagged(attempt, expected):
    r = decide("plate", reads("plate", text="XYZ999"), attempt=attempt)
    assert r["result"] == expected
    assert r["reason_code"] == "PLATE_MISMATCH"
    assert (r["hint"] is None) == (expected == "flagged")


def test_a_pass_stays_a_pass_on_any_attempt():
    assert decide("plate", good("plate"), attempt=3)["result"] == "pass"


# --- plate ---


@pytest.mark.parametrize("text", ["DEMO101", "demo 101", "DEMO-101", " D.E.M.O 1 0 1 "])
def test_plate_normalised_before_comparing(text):
    r = decide("plate", reads("plate", text=text))
    assert (r["result"], r["value"]) == ("pass", "DEMO101")
    assert r["inputs"]["policy.plates"] == ["DEMO101"]


def test_other_plate_asks_for_a_retake():
    r = decide("plate", reads("plate", text="8ABC123"))
    assert (r["result"], r["reason_code"]) == ("retake", "PLATE_MISMATCH")
    assert r["hint"] == "Retake this photo."  # identity mismatches give a plain hint (judge review 1 Nov, #4)
    assert r["inputs"]["photo.plate"] == "8ABC123"


def test_plate_without_a_policy_is_unclear_never_pass():
    """Judge review 1 Nov (Claude #7): no policy on file used to pass and read as "matches"."""
    r = decide("plate", reads("plate", text="8ABC123"), ctx(policy=None))
    assert (r["result"], r["reason_code"], r["value"], r["hint"]) == ("unclear", "POLICY_NOT_VERIFIED", "8ABC123", None)
    assert r["reason"] == "Plate 8ABC123 was read, but there is no verified policy on file to match it against."


# --- VIN: read twice, code checks ---


def test_vin_two_matching_reads_of_the_policy_vin_pass():
    r = decide("vin_photo", vin_reads("1HGCV1F39LA526018", "1HGCV1F39LA526018"))
    assert (r["result"], r["value"]) == ("pass", "1HGCV1F39LA526018")
    assert r["inputs"]["photo.vin"] == ["1HGCV1F39LA526018", "1HGCV1F39LA526018"]
    assert r["inputs"]["policy.vins"] == ["1HGCV1F39LA526018"]


def test_vin_reads_are_normalised():
    readings = vin_reads("1hgcv1f39la526018")
    readings[1]["characters"] = list("1HGCV1F39LA526018")
    readings[0]["vin"] = "1HG CV1F3-9LA526018"
    assert decide("vin_photo", readings)["result"] == "pass"


def test_vin_reads_that_disagree_ask_for_a_retake():
    r = decide("vin_photo", vin_reads("1HGCV1F39LA526018", "1HGCV1F39LA5260I8"))
    assert (r["result"], r["reason_code"], r["hint"], r["value"]) == ("retake", "PHOTO_READS_DISAGREE", HINT_UNREADABLE_VIN, None)


@pytest.mark.parametrize(
    "vin",
    [
        "1HGCV1F39LA5260O8",  # letter O never appears in a VIN, even when both reads agree on it
        "1HGCV1F38LA526018",  # check digit wrong
        "1HGCV1F39LA52601",  # 16 characters
    ],
)
def test_vin_that_both_reads_agree_on_but_cannot_be_valid(vin):
    r = decide("vin_photo", vin_reads(vin))
    assert (r["result"], r["reason_code"], r["hint"]) == ("retake", "VIN_READ_INVALID", HINT_UNREADABLE_VIN)


def test_valid_vin_of_another_vehicle_asks_for_a_retake():
    r = decide("vin_photo", vin_reads("5YJ3E1EA8MA159083"))  # DEMO-AUTO-1002's Tesla
    assert (r["result"], r["reason_code"]) == ("retake", "VIN_MISMATCH")
    assert r["hint"] == "Retake this photo."  # identity mismatches give a plain hint (judge review 1 Nov, #4)


def test_photo_vin_is_compared_with_the_policy_not_the_typed_vin():
    # The typed VIN vs the policy is T13's check; the photo must not count it again (P12).
    r = decide("vin_photo", good("vin_photo"), ctx(vin="5YJ3E1EA8MA159083"))
    assert r["result"] == "pass"


def test_without_a_policy_the_photo_vin_is_compared_with_the_typed_vin():
    r = decide("vin_photo", vin_reads("5YJ3E1EA8MA159083"), ctx(policy=None))
    assert (r["result"], r["reason_code"]) == ("retake", "VIN_MISMATCH")
    assert r["hint"] == "Retake this photo."  # identity mismatches give a plain hint (judge review 1 Nov, #4)
    assert r["inputs"]["form.vin"] == "1HGCV1F39LA526018"
    same = decide("vin_photo", vin_reads("5YJ3E1EA8MA159083"), ctx(policy=None, vin="5YJ3E1EA8MA159083"))
    assert (same["result"], same["reason_code"]) == ("unclear", "POLICY_NOT_VERIFIED")


def test_without_a_policy_or_a_typed_vin_a_valid_vin_is_unclear():
    c = ctx(policy=None)
    c["answers"].pop("vin")
    r = decide("vin_photo", vin_reads("5YJ3E1EA8MA159083"), c)
    assert (r["result"], r["reason_code"], r["value"]) == ("unclear", "POLICY_NOT_VERIFIED", "5YJ3E1EA8MA159083")


def test_the_claimant_sees_unclear_as_saved():
    entries = [{"item_id": "p", "slot_id": "plate", "result": "unclear", "hint": None, "shots_left": 2}]
    assert photo_check.public(entries) == [{"item_id": "p", "slot_id": "plate", "result": "unchecked", "hint": None, "shots_left": 2}]


# --- odometer ---


def test_s0_misread_is_caught():
    # S0 (28 Oct): the model read 56,947 as 569,475. A 2020 car in 2026: limit 7 × 30,000 = 210,000 miles.
    r = decide("odometer", reads("odometer", reading=569475))
    assert (r["result"], r["reason_code"]) == ("retake", "ODOMETER_IMPLAUSIBLE")
    assert r["hint"] == "We read 569,475 miles, which is very high for a 2020 vehicle. Photograph the main odometer, not the trip meter."
    assert r["inputs"]["policy.vehicle_year"] == 2020
    assert r["inputs"]["photo.odometer"] == 569475


@pytest.mark.parametrize("reading, unit, expected", [(210000, "mi", "pass"), (210001, "mi", "retake"), (0, "mi", "pass"), (300000, "km", "pass"), (340000, "km", "retake")])
def test_odometer_limit_by_model_year(reading, unit, expected):
    assert decide("odometer", reads("odometer", reading=reading, unit=unit))["result"] == expected


def test_km_reading_is_stored_in_miles():
    assert decide("odometer", reads("odometer", reading=100000, unit="km"))["value"] == 62137


def test_odometer_uses_the_vehicle_of_the_typed_vin():
    policy = copy.deepcopy(P1001)
    policy["vehicles"].append({"vin": "JTDKARFU1PA186091", "plate": "NEW1", "year": 2026, "make": "Toyota", "model": "Prius"})
    # 2026 vehicle: limit 1 × 30,000
    assert decide("odometer", reads("odometer", reading=56947), ctx(policy=policy, vin="JTDKARFU1PA186091"))["result"] == "retake"
    assert decide("odometer", reads("odometer", reading=56947), ctx(policy=policy))["result"] == "pass"


def test_odometer_without_a_policy_passes():
    assert decide("odometer", reads("odometer", reading=569475), ctx(policy=None))["result"] == "pass"


# --- driver's licence ---


@pytest.mark.parametrize("name", ["JORDAN AVERY", "Avery, Jordan", "JORDAN M AVERY", "jordan  avery"])
def test_licence_name_matches_the_holder_who_drove(name):
    r = decide("driver_license", reads("driver_license", name=name))
    assert (r["result"], r["value"]) == ("pass", name)
    assert r["inputs"]["expected.name"] == "Jordan Avery"


def test_licence_of_someone_else_asks_for_a_retake():
    r = decide("driver_license", reads("driver_license", name="SAM RIVERA"))
    assert (r["result"], r["reason_code"]) == ("retake", "LICENSE_NAME_MISMATCH")
    assert r["hint"] == photo_check.IDENTITY_HINT == "Retake this photo."


def test_licence_is_compared_with_the_driver_named_in_the_claim():
    c = ctx(driver_is_holder="No", driver_name="Alex Rivera")
    assert decide("driver_license", reads("driver_license", name="ALEX RIVERA"), c)["result"] == "pass"
    assert decide("driver_license", reads("driver_license", name="JORDAN AVERY"), c)["result"] == "retake"


def test_licence_without_an_expected_name_is_not_compared():
    c = ctx()
    c["answers"].pop("driver_is_holder")
    assert decide("driver_license", reads("driver_license", name="SOMEONE ELSE"), c)["result"] == "pass"


def test_expired_licence_is_flagged_at_once():
    # Retaking cannot fix an expired licence: no retake, straight to the adjuster.
    r = decide("driver_license", reads("driver_license", expires="2026-10-26"))  # loss date 2026-10-27
    assert (r["result"], r["reason_code"], r["hint"]) == ("flagged", "LICENSE_EXPIRED", None)


@pytest.mark.parametrize("expires", ["2026-10-27", None, "10/2029", "2029-13-45"])
def test_licence_valid_on_the_loss_date_or_expiry_unreadable_passes(expires):
    assert decide("driver_license", reads("driver_license", expires=expires))["result"] == "pass"


def test_licence_expiry_falls_back_to_today_without_a_loss_date():
    c = ctx()
    c["answers"].pop("loss_date")
    assert decide("driver_license", reads("driver_license", expires="2026-10-27"), c)["result"] == "flagged"


# --- damage ---


def test_closeup_without_visible_damage_asks_for_a_retake():
    r = decide("damage_closeup", reads("damage_closeup", damage_visible=False, parts=[]))
    assert (r["result"], r["reason_code"]) == ("retake", "DAMAGE_NOT_VISIBLE")
    assert r["hint"] == "We could not see the damage clearly. Get closer to the damaged area."


def test_closeup_with_an_unknown_part_passes():
    assert decide("damage_closeup", reads("damage_closeup", parts=[]))["result"] == "pass"
    assert decide("damage_closeup", reads("damage_closeup", parts=["Bumper thing"]))["value"] == []


def test_wide_shot_needs_a_part_but_not_visible_damage():
    assert decide("damage_wide", reads("damage_wide", damage_visible=False))["result"] == "pass"
    r = decide("damage_wide", reads("damage_wide", parts=[]))
    assert (r["result"], r["reason_code"]) == ("retake", "DAMAGE_PART_NOT_FOUND")
    assert r["hint"] == "Step back so the damage and the surrounding part of the vehicle are both visible."


@pytest.mark.parametrize("slot", ["damage_closeup", "damage_wide"])
def test_damage_on_another_area_asks_to_check_the_form_or_retake(slot):
    r = decide(slot, reads(slot, parts=["Front left", "Hood"]))
    assert (r["result"], r["reason_code"]) == ("retake", "DAMAGE_AREA_MISMATCH")
    assert r["hint"] == (
        "This photo seems to show the front left, hood, but you said the damage is on the rear bumper. "
        "Check the damaged areas in the form, or retake the photo."
    )
    assert r["inputs"]["photo.parts"] == ["Front left", "Hood"]
    assert r["inputs"]["form.damaged_areas"] == ["Rear bumper"]


@pytest.mark.parametrize(
    "part, declared, ok",
    [
        ("Rear bumper", ["Rear bumper"], True),
        ("Rear left", ["Rear bumper"], True),  # adjacent
        ("Rear right", ["Rear left"], True),  # left and right are never compared
        ("Wheels or tires", ["Front right"], True),
        ("Front bumper", ["Rear bumper"], False),
        ("Roof", ["Front bumper"], False),
    ],
)
def test_area_match_allows_neighbours(part, declared, ok):
    r = decide("damage_closeup", reads("damage_closeup", parts=[part]), ctx(damaged_areas=declared))
    assert (r["result"] == "pass") == ok


def test_no_declared_areas_means_no_area_comparison():
    assert decide("damage_wide", reads("damage_wide", parts=["Roof"]), ctx(damaged_areas=[]))["result"] == "pass"


def test_adjacency_is_symmetric_and_covers_every_area():
    assert set(photo_check.ADJACENT) == set(photo_check.AREAS)
    for area, neighbours in photo_check.ADJACENT.items():
        assert area not in neighbours
        for other in neighbours:
            assert area in photo_check.ADJACENT[other], (area, other)


# --- view: pending, stale, attempts ---


def row(item_id, slot="plate", received="2026-10-28T14:00:00+00:00", superseded=False, status="done", readings=None, started=None):
    return {
        "item_id": item_id,
        "slot_id": slot,
        "received_at": received,
        "superseded": superseded,
        "status": status,
        "readings": readings if readings is not None or status != "done" else good(slot),
        "started_at": started or received,
    }


BAD_PLATE = [{**GOOD_READS["plate"][0], "text": "XYZ999"}]


def test_view_lists_current_items_only_in_upload_order():
    rows = [
        row("b", "vin_photo", received="2026-10-28T14:02:00+00:00"),
        row("a", received="2026-10-28T14:01:00+00:00"),
        row("old", received="2026-10-28T14:00:00+00:00", superseded=True),
    ]
    out = photo_check.view(rows, ctx(), NOW)
    assert [e["item_id"] for e in out] == ["a", "b"]
    assert [e["result"] for e in out] == ["pass", "pass"]
    assert out[0]["attempt"] == 1 and out[0]["shots_left"] == 2


def test_fresh_pending_shows_pending_and_stale_pending_is_unchecked():
    started = NOW - photo_check.STALE_AFTER
    fresh = photo_check.view([row("a", status="pending", started=started.isoformat())], ctx(), NOW)[0]
    assert (fresh["result"], fresh["hint"]) == ("pending", None)
    stale = photo_check.view([row("a", status="pending", started=(started - timedelta(seconds=1)).isoformat())], ctx(), NOW)[0]
    assert (stale["result"], stale["reason_code"]) == ("unchecked", "PHOTO_UNCHECKED")


def test_failed_check_is_unchecked():
    assert photo_check.view([row("a", status="failed")], ctx(), NOW)[0]["result"] == "unchecked"


def test_attempts_count_earlier_shots_that_needed_a_retake():
    rows = [
        row("1", received="2026-10-28T14:00:00+00:00", superseded=True, readings=BAD_PLATE),
        row("2", received="2026-10-28T14:01:00+00:00", superseded=True, readings=BAD_PLATE),
        row("3", received="2026-10-28T14:02:00+00:00", readings=BAD_PLATE),
    ]
    entry = photo_check.view(rows, ctx(), NOW)[0]
    assert (entry["item_id"], entry["attempt"], entry["shots_left"], entry["result"]) == ("3", 3, 0, "flagged")


def test_good_or_unchecked_earlier_shots_do_not_use_up_tries():
    rows = [
        row("1", received="2026-10-28T14:00:00+00:00", superseded=True),  # a good shot, retaken anyway
        row("2", received="2026-10-28T14:01:00+00:00", superseded=True, status="failed"),
        row("3", received="2026-10-28T14:02:00+00:00", readings=BAD_PLATE),
    ]
    entry = photo_check.view(rows, ctx(), NOW)[0]
    assert (entry["attempt"], entry["result"], entry["shots_left"]) == (1, "retake", 2)


def test_attempts_are_counted_per_slot():
    rows = [
        row("v1", "vin_photo", received="2026-10-28T14:00:00+00:00", superseded=True, readings=vin_reads("5YJ3E1EA8MA159083")),
        row("p1", received="2026-10-28T14:01:00+00:00", readings=BAD_PLATE),
    ]
    assert photo_check.view(rows, ctx(), NOW)[0]["attempt"] == 1


def test_public_view_hides_readings_and_reasons():
    entries = photo_check.view([row("a", readings=BAD_PLATE)], ctx(), NOW)
    assert photo_check.public(entries) == [
        {
            "item_id": "a",
            "slot_id": "plate",
            "result": "retake",
            "hint": "Retake this photo.",  # identity mismatches give a plain hint (judge review 1 Nov, #4)
            "shots_left": 2,
        }
    ]


# --- form edits made after a hint (T34b) ---


def entry(result="retake", reason_code="DAMAGE_AREA_MISMATCH", item_id="a", slot="damage_wide"):
    return {"item_id": item_id, "slot_id": slot, "result": result, "reason_code": reason_code}


def test_changing_the_areas_after_the_area_hint_is_recorded():
    before = {**DAMAGE_ANSWERS, "damaged_areas": ["Rear bumper"]}
    after = {**DAMAGE_ANSWERS, "damaged_areas": ["Front left"]}
    assert photo_check.hint_edits([entry()], before, after) == [{
        "item_id": "a", "slot": "damage_wide", "reason_code": "DAMAGE_AREA_MISMATCH",
        "field": "damaged_areas", "before": ["Rear bumper"], "after": ["Front left"],
    }]


def test_no_edit_no_record_and_unrelated_edits_are_ignored():
    assert photo_check.hint_edits([entry()], DAMAGE_ANSWERS, dict(DAMAGE_ANSWERS)) == []
    assert photo_check.hint_edits([entry()], DAMAGE_ANSWERS, {**DAMAGE_ANSWERS, "description": "changed"}) == []


@pytest.mark.parametrize("result", ["pass", "flagged", "unchecked", "pending"])
def test_only_a_hint_the_customer_saw_counts(result):
    after = {**DAMAGE_ANSWERS, "damaged_areas": ["Front left"]}
    assert photo_check.hint_edits([entry(result=result)], DAMAGE_ANSWERS, after) == []


def test_reason_codes_without_a_form_hint_are_ignored():
    after = {**DAMAGE_ANSWERS, "damaged_areas": ["Front left"], "vin": "X"}
    assert photo_check.hint_edits([entry(reason_code="PLATE_MISMATCH", slot="plate")], DAMAGE_ANSWERS, after) == []


def test_vin_and_licence_hints_watch_their_fields_and_a_missing_key_is_none():
    before = {k: v for k, v in DAMAGE_ANSWERS.items() if k != "driver_name"}
    after = {**before, "vin": "5YJ3E1EA8MA159083", "driver_name": "Sam Lee", "driver_is_holder": "No"}
    entries = [entry(reason_code="VIN_MISMATCH", item_id="v", slot="vin_photo"),
               entry(reason_code="LICENSE_NAME_MISMATCH", item_id="l", slot="driver_license")]
    edits = photo_check.hint_edits(entries, before, after)
    assert [(e["item_id"], e["field"]) for e in edits] == [("v", "vin"), ("l", "driver_is_holder"), ("l", "driver_name")]
    assert edits[2]["before"] is None and edits[2]["after"] == "Sam Lee"
