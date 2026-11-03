"""T49 (tasks/t49-design.md): the model reports the angle and the photo side; code works out the vehicle side."""

import pytest

from evidence_server import damage, damage_prompts, photo_check, photo_prompts, viewpoint

GOOD = {"is_vehicle": True, "damage_visible": True, "unsure": False, "problem": "none"}


# --- viewpoint.py ---


@pytest.mark.parametrize("view, side, expected", [
    ("front", "left", "right"), ("front", "right", "left"),  # facing the front, the car's left is on the photo's right
    ("rear", "left", "left"), ("rear", "right", "right"),
    ("left side", "unknown", "left"), ("right side", "left", "right"),
    ("front left corner", "right", None), ("rear left corner", "middle", None),  # corners: area by area, in resolve
    ("front right corner", "unknown", None), ("rear right corner", "left", None),
    ("front", "middle", None), ("rear", "unknown", None), ("inside", "left", None), ("underneath", "right", None),
    ("unknown", "left", None), ("sideways", "left", None), (None, None, None),
])
def test_vehicle_side_from_the_angle_and_the_photo_side(view, side, expected):
    assert viewpoint.vehicle_side(view, side) == expected


@pytest.mark.parametrize("box, expected", [
    ([100, 200, 300, 400], "left"), ([450, 0, 550, 100], "middle"), ([700, 10, 900, 20], "right"),
    ([350, 0, 450, 10], "middle"),  # a centre of exactly 400 is the middle
    (None, "unknown"), ([1, 2], "unknown"), ("x", "unknown"), ([0, 0, "a", 1], "unknown"),
])
def test_photo_side_of_a_box(box, expected):
    assert viewpoint.image_side_of_box(box) == expected


@pytest.mark.parametrize("area, view, side, expected", [
    ("Front left", "front", "left", ("Front right", True)),  # the classic mirror slip
    ("Front right", "front", "left", ("Front right", False)),
    ("Rear left", "rear", "right", ("Rear right", True)),
    ("Left side", "right side", "unknown", ("Right side", True)),
    ("Right side", "front left corner", "middle", ("Left side", True)),  # the far side cannot show from a corner
    ("Rear right", "front left corner", "unknown", ("Rear left", True)),
    ("Front right", "front left corner", "unknown", ("Front right", False)),  # both front corners can show
    ("Front left", "rear right corner", "unknown", ("Front right", True)),
    ("Rear left", "rear right corner", "left", ("Rear left", False)),
    ("Left side", "front right corner", "right", ("Right side", True)),
    ("Front right", "rear left corner", "unknown", ("Front left", True)),
    ("Hood", "front", "left", ("Hood", False)),  # not a sided area
    ("Front left", "rear", "left", ("Front left", False)),  # the side agrees
    ("Rear right", "unknown", "left", ("Rear right", False)),  # no angle, no change
    ("Front left", "front", "middle", ("Front left", False)),
])
def test_resolve_swaps_only_a_side_the_angle_contradicts(area, view, side, expected):
    assert viewpoint.resolve(area, view, side) == expected


def test_the_lists_match_the_contract():
    assert viewpoint.VIEWPOINTS == (
        "front", "rear", "left side", "right side", "front left corner", "front right corner",
        "rear left corner", "rear right corner", "inside", "underneath", "unknown",
    )
    assert viewpoint.IMAGE_SIDES == ("left", "right", "middle", "unknown")


# --- prompts ---


def test_prompts_ask_for_the_angle_first_and_explain_the_mirror():
    rechecks = photo_prompts.RECHECKS
    assert set(rechecks) == {"damage_closeup", "damage_wide"}
    assert rechecks["damage_closeup"] != photo_prompts.DAMAGE_CLOSEUP
    assert rechecks["damage_wide"] != photo_prompts.DAMAGE_WIDE
    for prompt in (photo_prompts.DAMAGE_CLOSEUP, photo_prompts.DAMAGE_WIDE, damage_prompts.DAMAGE, *rechecks.values()):
        assert "driver's side" in prompt
        listed = '"parts"' if '"parts"' in prompt else '"damages"'
        assert prompt.index('"viewpoint"') < prompt.index(listed)
        for v in viewpoint.VIEWPOINTS:
            assert f'"{v}"' in prompt, v
    for prompt in (photo_prompts.DAMAGE_CLOSEUP, photo_prompts.DAMAGE_WIDE, *rechecks.values()):
        assert '"image_side"' in prompt
    assert (photo_prompts.VERSION, damage_prompts.VERSION, damage.VERSION, photo_check.VERSION) == (
        "prompts/v4", "damage-prompts/v3", "damage/v2", "photo/v4")


# --- the damage reading after submit ---


def reading(view, damages):
    return {"is_vehicle": True, "viewpoint": view, "unsure": False, "damages": damages}


def dent(area, box):
    return {"area": area, "damage_type": "BODY_PANEL_DENT", "severity": "moderate", "age": "new", "box": box,
            "observed": "A dent in the panel"}


def test_the_damage_reading_corrects_a_mirrored_side_and_keeps_the_models_word():
    shaped = damage.shape(reading("front", [dent("Front left", [100, 300, 300, 500]), dent("Hood", [400, 100, 600, 300])]))
    first, second = shaped["damages"]
    assert (first["area"], first["area_read"]) == ("Front right", "Front left")
    assert second["area"] == "Hood" and "area_read" not in second
    assert shaped["viewpoint"] == "front"


def test_without_a_usable_angle_the_damage_reading_is_unchanged():
    no_angle = reading("front", [dent("Front left", [100, 300, 300, 500])])
    del no_angle["viewpoint"]
    for answer in (no_angle, reading("from the moon", [dent("Front left", [100, 300, 300, 500])])):
        shaped = damage.shape(answer)
        [d] = shaped["damages"]
        assert d["area"] == "Front left" and "area_read" not in d
        assert shaped["viewpoint"] == "unknown"


def test_a_central_box_keeps_the_models_side():
    shaped = damage.shape(reading("rear", [dent("Rear left", [420, 300, 560, 500])]))
    assert shaped["damages"][0]["area"] == "Rear left"


# --- the photo check before signing ---


def closeup(parts, view="unknown", side="unknown", **extra):
    return {**GOOD, "parts": parts, "viewpoint": view, "image_side": side, **extra}


def ctx(*declared):
    return {"policy": None, "answers": {"damaged_areas": list(declared)}, "today": None}


def test_the_photo_check_records_the_worked_out_side_and_the_models_words():
    d = photo_check.decide("damage_closeup", [closeup(["Front left"], "front", "left")], ctx("Front right"))
    assert d["result"] == "pass"
    assert d["value"] == ["Front right"]
    inputs = d["inputs"]
    assert (inputs["photo.viewpoint"], inputs["photo.image_side"], inputs["photo.parts_read"]) == (
        "front", "left", ["Front left"])
    assert inputs["photo.parts"] == ["Front right"]


def test_an_uncorrected_reading_has_no_parts_read():
    d = photo_check.decide("damage_wide", [closeup(["Front right"], "front", "left")], ctx("Front right"))
    assert d["result"] == "pass" and "photo.parts_read" not in d["inputs"]


def test_older_answers_without_an_angle_still_decide():
    old = {**GOOD, "parts": ["Rear bumper"]}  # cached before prompts/v4
    d = photo_check.decide("damage_closeup", [old], ctx("Rear bumper"))
    assert d["result"] == "pass" and d["inputs"]["photo.viewpoint"] == "unknown"


def test_two_damage_readings_that_disagree_make_it_unclear_not_a_retake():
    for slot in ("damage_closeup", "damage_wide"):
        d = photo_check.decide(slot, [closeup(["Rear bumper"]), closeup(["Windshield"])], ctx("Windshield"))
        assert (d["result"], d["reason_code"], d["hint"]) == ("unclear", "DAMAGE_AREA_UNCLEAR", None), slot


def test_two_damage_readings_that_agree_keep_the_retake():
    d = photo_check.decide("damage_closeup", [closeup(["Rear bumper"]), closeup(["Rear bumper"])], ctx("Windshield"))
    assert (d["result"], d["reason_code"]) == ("retake", "DAMAGE_AREA_MISMATCH")
    assert "rear bumper" in d["hint"]


def test_one_damage_reading_behaves_as_before():
    d = photo_check.decide("damage_closeup", [closeup(["Rear bumper"])], ctx("Windshield"))
    assert (d["result"], d["reason_code"]) == ("retake", "DAMAGE_AREA_MISMATCH")


def test_an_unusable_or_unsure_second_reading_is_ignored():
    for second in ({"nonsense": True}, closeup(["Windshield"], unsure=True)):
        d = photo_check.decide("damage_closeup", [closeup(["Rear bumper"]), second], ctx("Windshield"))
        assert (d["result"], d["reason_code"]) == ("retake", "DAMAGE_AREA_MISMATCH"), second
