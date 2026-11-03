"""T20 damage reading, the pure part (contract: tasks/t20-design.md): the model reads, this code keeps what fits."""

import copy
from datetime import UTC, datetime, timedelta

import pytest

from conftest import GOOD_DAMAGE
from evidence_server import damage, photo_prompts, vocab

NOW = datetime(2026, 10, 28, 15, 0, tzinfo=UTC)
STARTED = NOW.isoformat()


def answer(**changes):
    return {**copy.deepcopy(GOOD_DAMAGE), **changes}


def entry(**changes):
    return {**copy.deepcopy(GOOD_DAMAGE["damages"][0]), **changes}


def kept(*entries):
    return damage.shape(answer(damages=list(entries)))


# --- shape: the answer as a whole ---


def test_good_answer_is_kept_as_is():
    assert damage.shape(answer()) == {
        "is_vehicle": True, "viewpoint": "unknown", "unsure": False, "dropped": 0,  # T49: no angle in the answer
        "damages": [{"area": "Rear bumper", "damage_type": "BUMPER_DAMAGE", "severity": "moderate", "age": "new",
                     "box": [200, 450, 800, 700], "observed": "Rear bumper cover pushed in"}],
    }


@pytest.mark.parametrize("bad", [
    None, "text", [], {"damages": []}, {"is_vehicle": "yes", "damages": []},
    {"is_vehicle": True}, {"is_vehicle": True, "damages": "none"},
])
def test_unusable_answer_is_none(bad):
    assert damage.shape(bad) is None


@pytest.mark.parametrize("value, expected", [(True, True), (False, False), ("true", False), (None, False)])
def test_unsure_is_a_bool_or_false(value, expected):
    assert damage.shape(answer(unsure=value))["unsure"] is expected
    a = answer()
    del a["unsure"]
    assert damage.shape(a)["unsure"] is False


def test_no_vehicle_keeps_no_damage_and_counts_what_was_listed():
    r = damage.shape(answer(is_vehicle=False, damages=[entry(), entry()]))
    assert (r["is_vehicle"], r["damages"], r["dropped"]) == (False, [], 2)


def test_vehicle_without_damage():
    assert damage.shape(answer(damages=[])) == {
        "is_vehicle": True, "viewpoint": "unknown", "unsure": False, "damages": [], "dropped": 0}


# --- shape: each damage (D2) ---


@pytest.mark.parametrize("changes", [
    {"area": "Bonnet"}, {"area": "front bumper"}, {"area": None},
    {"damage_type": "DENT"}, {"damage_type": "bumper_damage"},
    {"severity": "medium"}, {"severity": None},
])
def test_values_outside_the_fixed_lists_are_dropped(changes):
    r = kept(entry(**changes), entry(area="Hood", damage_type="BODY_PANEL_DENT"))
    assert [d["area"] for d in r["damages"]] == ["Hood"]
    assert r["dropped"] == 1


def test_missing_keys_or_a_non_object_are_dropped():
    no_area = entry()
    del no_area["area"]
    r = kept(no_area, "Front bumper", None, entry())
    assert (len(r["damages"]), r["dropped"]) == (1, 3)


def test_every_area_and_type_of_the_fixed_lists_is_accepted():
    entries = [entry(area=a) for a in photo_prompts.AREAS] + [entry(damage_type=t) for t in sorted(vocab.DAMAGE_TYPES)]
    for start in range(0, len(entries), damage.MAX_DAMAGES):
        assert damage.shape(answer(damages=entries[start : start + damage.MAX_DAMAGES]))["dropped"] == 0


@pytest.mark.parametrize("age, expected", [("new", "new"), ("old", "old"), ("unsure", "unsure"), ("fresh", "unsure"), (None, "unsure")])
def test_age_outside_the_list_is_unsure(age, expected):
    assert kept(entry(age=age))["damages"][0]["age"] == expected


def test_missing_age_and_observed_get_defaults():
    e = entry()
    del e["age"], e["observed"]
    d = kept(e)["damages"][0]
    assert (d["age"], d["observed"]) == ("unsure", "")


@pytest.mark.parametrize("box, expected", [
    ([200, 450, 800, 700], [200, 450, 800, 700]),
    ([199.6, 450.4, 800.5, 700], [200, 450, 800, 700]),  # rounded to ints
    ([-50, 0, 1200, 999], [0, 0, 1000, 999]),            # clamped to 0-1000
    ([800, 450, 200, 700], None),                        # x1 >= x2
    ([200, 700, 800, 700], None),                        # y1 >= y2
    ([1100, 0, 1200, 500], None),                        # both clamp to 1000
    ([200, 450, 800], None),
    ([200, 450, 800, 700, 5], None),
    ([True, 450, 800, 700], None),
    (["200", 450, 800, 700], None),
    ("200,450,800,700", None),
    (None, None),
])
def test_box_is_four_numbers_clamped_and_ordered(box, expected):
    assert kept(entry(box=box))["damages"][0]["box"] == expected


def test_observed_is_stripped_and_cut():
    assert kept(entry(observed="  dent  "))["damages"][0]["observed"] == "dent"
    assert len(kept(entry(observed="x" * 500))["damages"][0]["observed"]) == 200
    assert kept(entry(observed=42))["damages"][0]["observed"] == ""


def test_kept_damage_has_exactly_the_contract_keys():
    d = kept(entry(extra="ignored", confidence=0.9))["damages"][0]
    assert set(d) == {"area", "damage_type", "severity", "age", "box", "observed"}


def test_at_most_twelve_damages_the_first_ones():
    entries = [entry(observed=str(n)) for n in range(15)] + [entry(area="Nowhere")]
    r = kept(*entries)
    assert [d["observed"] for d in r["damages"]] == [str(n) for n in range(12)]
    assert r["dropped"] == 4


# --- view ---


def row(item_id, slot="damage_closeup", status="done", result=None, error=None, started=STARTED):
    return {"item_id": item_id, "slot_id": slot, "status": status, "error": error, "started_at": started,
            "result": result if result is not None or status != "done" else damage.shape(answer())}


EMPTY = {"is_vehicle": None, "unsure": False, "damages": [], "dropped": 0}


def test_no_rows_is_none():
    assert damage.view([], NOW) == {"status": "none", "photos": [], "damages": [], "merged": [], "areas": [], "old_damage": False}


def test_done_photos_list_their_damages_in_photo_order():
    hood = damage.shape(answer(damages=[entry(area="Hood", damage_type="SCRATCH_SCUFF", severity="minor")]))
    v = damage.view([row("a"), row("b", "damage_wide", result=hood)], NOW)
    assert v["status"] == "done"
    assert [(p["item_id"], p["status"], p["error"], p["is_vehicle"]) for p in v["photos"]] == [("a", "done", None, True), ("b", "done", None, True)]
    assert [(d["item_id"], d["slot_id"], d["area"]) for d in v["damages"]] == [("a", "damage_closeup", "Rear bumper"), ("b", "damage_wide", "Hood")]
    assert list(v["damages"][0])[:2] == ["item_id", "slot_id"]


def test_areas_follow_the_form_order_without_old_damage():
    mixed = damage.shape(answer(damages=[
        entry(area="Rear bumper"), entry(area="Front bumper"), entry(area="Hood", age="old"), entry(area="Rear bumper", age="unsure"),
    ]))
    v = damage.view([row("a", result=mixed)], NOW)
    assert v["areas"] == ["Front bumper", "Rear bumper"]
    assert v["old_damage"] is True


def test_pending_failed_and_stale():
    fresh = (NOW - damage.STALE_AFTER).isoformat()
    stale = (NOW - damage.STALE_AFTER - timedelta(seconds=1)).isoformat()
    v = damage.view([
        row("a", status="pending", started=fresh),
        row("b", status="pending", started=stale),
        row("c", status="failed", error="timeout"),
    ], NOW)
    assert [(p["status"], p["error"]) for p in v["photos"]] == [("pending", None), ("failed", "stale"), ("failed", "timeout")]
    for p in v["photos"]:
        assert {k: p[k] for k in EMPTY} == EMPTY
    assert v["status"] == "pending"


@pytest.mark.parametrize("statuses, expected", [
    (["done"], "done"), (["done", "done"], "done"), (["failed"], "failed"), (["failed", "failed"], "failed"),
    (["done", "failed"], "partial"), (["pending", "done"], "pending"), (["pending", "failed"], "pending"),
])
def test_overall_status(statuses, expected):
    rows = [row(str(n), status=s, error=None if s != "failed" else "timeout") for n, s in enumerate(statuses)]
    assert damage.view(rows, NOW)["status"] == expected


def test_failed_photos_add_no_damages():
    v = damage.view([row("a", status="failed", error="bad_shape"), row("b")], NOW)
    assert [d["item_id"] for d in v["damages"]] == ["b"]
    assert v["status"] == "partial"


# --- merge: one physical damage counts once (judge review 1 Nov, Claude #2) ---


def ref(item, number, slot="damage_closeup"):
    return {"item_id": item, "slot_id": slot, "number": number}


def listed(item, slot="damage_closeup", **kw):
    e = {"area": "Rear bumper", "damage_type": "BUMPER_DAMAGE", "severity": "moderate", "age": "new",
         "box": [200, 450, 800, 700], "observed": "dent"} | kw
    return {"item_id": item, "slot_id": slot, **e}


def test_merge_same_area_and_type_across_photos():
    [one] = damage.merge([listed("a"), listed("b", "damage_wide", box=[0, 0, 100, 100])])
    assert one["evidence"] == [ref("a", 1), ref("b", 1, "damage_wide")]
    assert one["keys"] == ["a:1", "b:1"]
    assert (one["item_id"], one["box"]) == ("a", [200, 450, 800, 700])  # primary keeps its own photo's box


def test_merge_keeps_the_highest_severity_and_its_photo_as_primary():
    [one] = damage.merge([listed("a", severity="minor", observed="scuff"), listed("b", "damage_wide", severity="severe", observed="torn")])
    assert (one["severity"], one["item_id"], one["slot_id"], one["observed"]) == ("severe", "b", "damage_wide", "torn")


def test_merge_keeps_different_types_or_areas_apart():
    out = damage.merge([listed("a"), listed("b", "damage_wide", damage_type="SCRATCH_SCUFF"), listed("b", "damage_wide", area="Hood")])
    assert len(out) == 3


def test_same_photo_needs_overlapping_boxes():
    apart = damage.merge([listed("a", box=[0, 0, 100, 100]), listed("a", box=[500, 500, 600, 600])])
    assert len(apart) == 2  # two separate dents on one panel stay two
    nested = damage.merge([listed("a", box=[0, 0, 400, 400]), listed("a", box=[100, 100, 300, 300])])
    assert len(nested) == 1 and nested[0]["evidence"] == [ref("a", 1), ref("a", 2)]
    unboxed = damage.merge([listed("a", box=None), listed("a", box=None)])
    assert len(unboxed) == 2  # no position: cannot tell, keep both


def test_a_photo_joins_a_damage_once_so_the_count_is_the_most_any_photo_shows():
    out = damage.merge([
        listed("a", box=[0, 0, 100, 100]), listed("a", box=[500, 500, 600, 600]),  # close-up: two dents
        listed("b", "damage_wide"),  # wide: one of them
    ])
    assert [len(d["evidence"]) for d in out] == [2, 1]


def test_old_and_not_old_never_merge_and_age_is_new_when_any_photo_says_new():
    out = damage.merge([listed("a", age="unsure"), listed("b", "damage_wide", age="new"), listed("c", "damage_wide", age="old")])
    assert [(d["age"], len(d["evidence"])) for d in out] == [("new", 2), ("old", 1)]


def test_view_carries_the_merged_list_and_keeps_per_photo_boxes():
    v = damage.view([row("a"), row("b", "damage_wide")], NOW)
    assert len(v["damages"]) == 2 and len(v["merged"]) == 1
    assert v["merged"] == damage.merge(v["damages"])
