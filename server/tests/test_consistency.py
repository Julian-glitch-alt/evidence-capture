"""T21 statement vs photos, the pure part (contract: tasks/t21-design.md)."""

import copy
from datetime import UTC, datetime

import pytest

from conftest import GOOD_DAMAGE, GOOD_STORY
from evidence_server import consistency, damage, vocab
from test_loss_notice import DAMAGE_ANSWERS

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)


def dmg(area="Rear bumper", age="new", damage_type="BUMPER_DAMAGE", severity="moderate", observed="Rear bumper cover pushed in"):
    return {**GOOD_DAMAGE["damages"][0], "area": area, "age": age, "damage_type": damage_type, "severity": severity, "observed": observed}


def reading(*photos, statuses=None):
    """photos: one list of damages per photo; photo n is item "i{n}", close-up first then wide."""
    rows = []
    for n, damages in enumerate(photos):
        status = (statuses or ["done"] * len(photos))[n]
        result = damage.shape({**GOOD_DAMAGE, "damages": damages}) if status == "done" else None
        rows.append({"item_id": f"i{n}", "slot_id": "damage_closeup" if n == 0 else "damage_wide", "status": status,
                     "result": result, "error": None if status == "done" else "timeout", "started_at": NOW.isoformat()})
    return damage.view(rows, NOW)


def answers(**changes):
    return {**copy.deepcopy(DAMAGE_ANSWERS), **changes}


def results(a=None, r=None, story=None):
    out = consistency.check(a if a is not None else answers(), r if r is not None else reading([dmg()]), story)
    return {x["check_id"]: x for x in out}


STORY = consistency.shape_story(GOOD_STORY, 1)


# --- usable reading ---


@pytest.mark.parametrize("statuses, expected", [(["done"], True), (["done", "failed"], True), (["failed"], False), (["pending"], False)])
def test_usable_only_when_something_was_read(statuses, expected):
    assert consistency.usable(reading(*[[dmg()]] * len(statuses), statuses=statuses)) is expected
    assert consistency.usable(damage.view([], NOW)) is False


def test_no_usable_reading_makes_every_check_unclear():
    out = consistency.check(answers(), reading([dmg()], statuses=["failed"]), None)
    assert [x["check_id"] for x in out] == list(consistency.CHECK_ORDER)
    for x in out:
        assert (x["result"], x["reason_code"], x["value"]) == ("unclear", "DAMAGE_NOT_READ", None)
        assert x["reason"] == "The damage photos were not analysed, so the statement could not be compared with them."
        assert x["inputs"] == {"reading.status": "failed"}


def test_every_reason_code_is_in_the_vocabulary():
    codes = {"DAMAGE_NOT_READ", "DAMAGE_OUTSIDE_STATEMENT", "PRIOR_DAMAGE_SUSPECTED", "STORY_INCONSISTENT", "STORY_UNCLEAR", "STORY_NOT_CHECKED"}
    assert codes <= vocab.REASON_CODES


# --- areas ---


def test_declared_and_seen_areas_match():
    r = results()["consistency.areas"]
    assert (r["result"], r["reason_code"]) == ("pass", None)
    assert r["reason"] == "Every damaged area in the photos is listed in the claim."
    assert r["value"] == {"undeclared": [], "not_seen": [], "evidence": []}
    assert r["inputs"] == {"form.damaged_areas": ["Rear bumper"], "photo.areas": ["Rear bumper"]}


def test_neighbouring_area_counts_as_listed():
    assert results(r=reading([dmg(area="Trunk or tailgate")]))["consistency.areas"]["result"] == "pass"


def test_new_damage_outside_the_statement_needs_a_look_with_evidence():
    r = results(r=reading([dmg()], [dmg(area="Hood"), dmg(area="Front bumper")]))["consistency.areas"]
    assert (r["result"], r["reason_code"]) == ("unclear", "DAMAGE_OUTSIDE_STATEMENT")
    assert r["reason"] == "The photos show new damage on Front bumper, Hood, which the claim does not list."
    assert r["value"]["undeclared"] == ["Front bumper", "Hood"]
    assert r["value"]["evidence"] == [
        {"item_id": "i1", "slot_id": "damage_wide", "number": 1}, {"item_id": "i1", "slot_id": "damage_wide", "number": 2},
    ]


def test_unsure_age_counts_as_new_but_old_damage_is_not_an_undeclared_area():
    assert results(r=reading([dmg(), dmg(area="Hood", age="unsure")]))["consistency.areas"]["value"]["undeclared"] == ["Hood"]
    assert results(r=reading([dmg(), dmg(area="Hood", age="old")]))["consistency.areas"]["result"] == "pass"


def test_declared_but_not_seen_is_only_a_note():
    r = results(a=answers(damaged_areas=["Rear bumper", "Roof"]))["consistency.areas"]
    assert (r["result"], r["value"]["not_seen"]) == ("pass", ["Roof"])
    assert r["reason"] == ("Every damaged area in the photos is listed in the claim."
                           " Roof is listed but not seen in the photos; they may not cover it.")
    r = results(a=answers(damaged_areas=["Rear bumper", "Roof", "Windshield"]))["consistency.areas"]
    assert r["reason"].endswith(" Roof, Windshield are listed but not seen in the photos; they may not cover it.")


@pytest.mark.parametrize("declared", [None, "Rear bumper", [], [3, None]])
def test_missing_declared_areas_make_every_seen_area_undeclared(declared):
    r = results(a=answers(damaged_areas=declared))["consistency.areas"]
    assert (r["result"], r["value"]["undeclared"], r["inputs"]["form.damaged_areas"]) == ("unclear", ["Rear bumper"], [])


def test_no_damage_seen_is_a_pass_with_a_note():
    r = results(r=reading([]))["consistency.areas"]
    assert (r["result"], r["value"]["not_seen"]) == ("pass", ["Rear bumper"])


# --- earlier damage ---


def test_old_damage_while_the_claimant_said_none_needs_a_look():
    r = results(r=reading([dmg(), dmg(area="Hood", age="old")]))["consistency.prior_damage"]
    assert (r["result"], r["reason_code"]) == ("unclear", "PRIOR_DAMAGE_SUSPECTED")
    assert r["reason"] == "The photos suggest older damage (1 item), but the claimant said none of the damage was there before."
    assert r["value"] == {"old": [{"item_id": "i0", "slot_id": "damage_closeup", "number": 2}]}
    assert r["inputs"] == {"form.prior_damage": "No", "photo.old_damage": 1}
    two = results(r=reading([dmg(age="old")], [dmg(area="Hood", age="old")]))["consistency.prior_damage"]
    assert "(2 items)" in two["reason"]


@pytest.mark.parametrize("said", ["Yes", "Not sure"])
def test_old_damage_the_claimant_mentioned_passes(said):
    r = results(a=answers(prior_damage=said), r=reading([dmg(age="old")]))["consistency.prior_damage"]
    assert (r["result"], r["reason"]) == ("pass", "The claimant said some damage may be older; the photos agree.")


def test_unsure_age_is_not_old_damage():
    r = results(r=reading([dmg(age="unsure")]))["consistency.prior_damage"]
    assert (r["result"], r["reason"], r["value"]) == ("pass", "No older damage seen in the photos.", {"old": []})


def test_old_damage_without_an_answer_needs_a_look():
    a = answers()
    del a["prior_damage"]
    r = results(a=a, r=reading([dmg(age="old")]))["consistency.prior_damage"]
    assert (r["result"], r["reason_code"]) == ("unclear", "PRIOR_DAMAGE_SUSPECTED")
    assert r["reason"] == "The photos suggest older damage, and the claimant did not say whether any damage was there before."


# --- story ---


def test_story_that_fits_passes_with_reasons_and_evidence():
    r = results(story=STORY)["consistency.story"]
    assert (r["result"], r["reason_code"], r["reason"]) == ("pass", None, "The statement fits the damage in the photos.")
    assert r["value"] == {
        "consistent": "yes", "impact_from_story": "rear", "impact_from_damage": "rear",
        "reasons": [{"text": "Backing into a bollard fits the dented rear bumper.",
                     "evidence": [{"item_id": "i0", "slot_id": "damage_closeup", "number": 1}]}],
    }
    assert r["inputs"] == {"form.cause": "Hit an object", "form.statement_chars": 39, "reading.damages": 1}


@pytest.mark.parametrize("said, result, code, reason", [
    ("no", "fail", "STORY_INCONSISTENT", "The statement does not fit the damage in the photos."),
    ("unsure", "unclear", "STORY_UNCLEAR", "The statement could not be matched with confidence to the damage."),
])
def test_story_that_does_not_fit_or_is_unclear(said, result, code, reason):
    r = results(story=consistency.shape_story({**GOOD_STORY, "consistent": said}, 1))["consistency.story"]
    assert (r["result"], r["reason_code"], r["reason"]) == (result, code, reason)


def test_no_story_answer_is_not_checked_but_the_code_checks_still_run():
    out = results(r=reading([dmg(), dmg(area="Hood", age="old")]), story=None)
    s = out["consistency.story"]
    assert (s["result"], s["reason_code"], s["value"]) == ("unclear", "STORY_NOT_CHECKED", None)
    assert s["reason"] == "The statement could not be compared with the damage automatically."
    assert out["consistency.prior_damage"]["reason_code"] == "PRIOR_DAMAGE_SUSPECTED"


def test_evidence_numbers_count_per_photo():
    r = reading([dmg()], [dmg(area="Hood"), dmg(area="Front bumper")])
    story = consistency.shape_story({**GOOD_STORY, "reasons": [{"text": "Front damage.", "damages": [3, 1]}]}, 3)
    v = results(r=r, story=story)["consistency.story"]["value"]
    assert v["reasons"][0]["evidence"] == [
        {"item_id": "i1", "slot_id": "damage_wide", "number": 2}, {"item_id": "i0", "slot_id": "damage_closeup", "number": 1},
    ]


# --- shape_story ---


@pytest.mark.parametrize("bad", [None, "yes", [], {}, {**GOOD_STORY, "consistent": "maybe"}, {**GOOD_STORY, "consistent": True}])
def test_unusable_story_answer_is_none(bad):
    assert consistency.shape_story(bad, 1) is None


def test_impacts_outside_the_list_are_unknown():
    s = consistency.shape_story({**GOOD_STORY, "impact_from_story": "back", "impact_from_damage": None}, 1)
    assert (s["impact_from_story"], s["impact_from_damage"]) == ("unknown", "unknown")
    s = consistency.shape_story({k: v for k, v in GOOD_STORY.items() if not k.startswith("impact")}, 1)
    assert (s["impact_from_story"], s["impact_from_damage"]) == ("unknown", "unknown")


def test_reasons_are_cleaned():
    s = consistency.shape_story({**GOOD_STORY, "reasons": [
        {"text": "  one  ", "damages": [1, 1, 0, 5, True, "2", 2]},
        {"text": "", "damages": [1]},
        "not a dict",
        {"text": "x" * 300, "damages": "1"},
        {"text": "three", "damages": []},
        {"text": "four", "damages": []},
    ]}, 2)
    assert s["reasons"] == [
        {"text": "one", "damages": [1, 2]},
        {"text": "x" * 200, "damages": []},
        {"text": "three", "damages": []},
    ]
    assert consistency.shape_story({**GOOD_STORY, "reasons": "none"}, 1)["reasons"] == []


# --- the block the model reads ---


def test_story_block_lists_the_statement_and_numbered_damage():
    r = reading([dmg()], [dmg(area="Hood", damage_type="BODY_PANEL_DENT", severity="severe", age="old", observed="")])
    assert consistency.story_block(answers(), r) == (
        "Cause: Hit an object\n"
        "Another vehicle involved: No\n"
        "Declared damaged areas: Rear bumper\n"
        "Prior damage declared: No\n"
        "Statement: Backed into a bollard in a parking lot.\n"
        "Damage seen in the photos:\n"
        "1. Rear bumper, bumper damage, moderate, new: Rear bumper cover pushed in (damage_closeup photo)\n"
        "2. Hood, body panel dent, severe, old (damage_wide photo)"
    )


def test_story_block_with_nothing_given():
    a = {"loss_type": "vehicle_damage", "damaged_areas": "Hood", "description": 12}
    assert consistency.story_block(a, reading([])) == (
        "Cause: not given\n"
        "Another vehicle involved: not given\n"
        "Declared damaged areas: none\n"
        "Prior damage declared: not given\n"
        "Statement: none\n"
        "Damage seen in the photos:\n"
        "none"
    )


def test_story_block_cuts_a_long_statement():
    block = consistency.story_block(answers(description="  " + "a" * 1500), reading([dmg()]))
    assert "Statement: " + "a" * consistency.MAX_STATEMENT + "\n" in block
