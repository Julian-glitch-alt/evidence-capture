"""T22 rough range, pure (contract: tasks/t22-t25-design.md): public ballparks, PERT 80%, never an estimate."""

import pytest

from evidence_server import damage, estimate, vocab
from test_consistency import NOW, dmg, reading

HOOD = dmg(area="Hood", damage_type="BODY_PANEL_DENT", severity="severe")
BUMPER = dmg(area="Front bumper", damage_type="BUMPER_DAMAGE", severity="moderate")
MIRROR = dmg(area="Right side", damage_type="MIRROR_DAMAGE", severity="severe")


def test_the_demo_claim():
    e = estimate.rough_range(reading([MIRROR], [HOOD, BUMPER]))
    assert (e["status"], e["full"], e["likely"], e["midpoint"]) == ("complete", [1300, 5900], [2950, 4250], 3600)
    assert e["priced"] == [
        {"item_id": "i0", "slot_id": "damage_closeup", "number": 1, "area": "Right side", "damage_type": "MIRROR_DAMAGE",
         "severity": "severe", "evidence": [{"item_id": "i0", "slot_id": "damage_closeup", "number": 1}], "low": 200, "high": 1000},
        {"item_id": "i1", "slot_id": "damage_wide", "number": 1, "area": "Hood", "damage_type": "BODY_PANEL_DENT",
         "severity": "severe", "evidence": [{"item_id": "i1", "slot_id": "damage_wide", "number": 1}], "low": 800, "high": 3500},
        {"item_id": "i1", "slot_id": "damage_wide", "number": 2, "area": "Front bumper", "damage_type": "BUMPER_DAMAGE",
         "severity": "moderate", "evidence": [{"item_id": "i1", "slot_id": "damage_wide", "number": 2}], "low": 300, "high": 1400},
    ]
    assert (e["unpriced"], e["excluded_old"], e["version"]) == ([], [], estimate.VERSION)
    assert e["basis"].startswith("PERT 80% interval from public repair-cost ballparks")


def test_one_item_rounds_lows_down_highs_up_and_the_midpoint_half_up():
    e = estimate.rough_range(reading([dmg(damage_type="BODY_PANEL_DENT", severity="minor")]))  # [75, 500]
    assert (e["full"], e["likely"], e["midpoint"]) == ([50, 500], [150, 400], 300)
    assert all(isinstance(v, int) for v in e["full"] + e["likely"] + [e["midpoint"]])


def test_old_damage_is_never_priced():
    e = estimate.rough_range(reading([BUMPER, dmg(area="Hood", age="old", damage_type="SCRATCH_SCUFF", severity="minor")]))
    assert [p["damage_type"] for p in e["priced"]] == ["BUMPER_DAMAGE"]
    assert e["excluded_old"] == [{"item_id": "i0", "slot_id": "damage_closeup", "number": 2, "area": "Hood", "damage_type": "SCRATCH_SCUFF",
                                  "severity": "minor", "evidence": [{"item_id": "i0", "slot_id": "damage_closeup", "number": 2}]}]
    assert (e["status"], e["full"]) == ("complete", [300, 1400])


def test_unsure_age_is_priced():
    assert len(estimate.rough_range(reading([dmg(age="unsure")]))["priced"]) == 1


def test_damage_without_a_ballpark_makes_the_range_incomplete():
    fire = dmg(area="Hood", damage_type="FIRE_DAMAGE", severity="moderate")
    e = estimate.rough_range(reading([BUMPER, fire]))
    assert e["status"] == "incomplete"
    assert e["unpriced"] == [{"item_id": "i0", "slot_id": "damage_closeup", "number": 2, "area": "Hood", "damage_type": "FIRE_DAMAGE",
                              "severity": "moderate", "evidence": [{"item_id": "i0", "slot_id": "damage_closeup", "number": 2}]}]
    assert e["full"] == [300, 1400]


def test_no_usable_reading_has_no_range():
    e = estimate.rough_range(reading([BUMPER], statuses=["failed"]))
    assert e == {"status": "none", "priced": [], "unpriced": [], "excluded_old": [], "full": None, "likely": None,
                 "midpoint": None, "basis": estimate.BASIS, "version": estimate.VERSION}
    assert estimate.rough_range(damage.view([], NOW))["status"] == "none"


def test_no_damage_seen_is_zero():
    e = estimate.rough_range(reading([]))
    assert (e["status"], e["full"], e["likely"], e["midpoint"]) == ("complete", [0, 0], [0, 0], 0)


def test_a_failed_photo_beside_a_read_one_prices_what_was_read():
    e = estimate.rough_range(reading([BUMPER], [HOOD], statuses=["done", "failed"]))
    assert (e["status"], e["full"]) == ("complete", [300, 1400])


def test_another_table_can_be_passed():
    table = {"BUMPER_DAMAGE": {"moderate": [1000, 1000]}}
    e = estimate.rough_range(reading([BUMPER, MIRROR]), table)
    assert (e["full"], e["likely"], e["midpoint"], e["status"]) == ([1000, 1000], [1000, 1000], 1000, "incomplete")


def test_same_reading_same_range():
    r = reading([MIRROR], [HOOD, BUMPER])
    assert estimate.rough_range(r) == estimate.rough_range(r)


# --- the table itself ---


def test_table_covers_every_damage_type_and_severity():
    assert set(estimate.TABLE) == vocab.DAMAGE_TYPES
    for damage_type, row in estimate.TABLE.items():
        for severity in damage.SEVERITIES:
            cell = row[severity]
            assert cell is None or (len(cell) == 2 and 0 < cell[0] < cell[1]), (damage_type, severity)
        if any(row[s] is not None for s in damage.SEVERITIES):
            assert row["sources"] and all(u.startswith("https://") for u in row["sources"]), damage_type


@pytest.mark.parametrize("damage_type", ["FIRE_DAMAGE", "INTERIOR_DAMAGE"])
def test_types_without_a_trustworthy_figure_stay_unpriced(damage_type):
    assert all(estimate.TABLE[damage_type][s] is None for s in damage.SEVERITIES)


# --- one physical damage seen in several photos is priced once (judge review 1 Nov, Claude #2) ---

def test_the_same_dent_in_the_close_up_and_the_wide_photo_is_priced_once():
    """The repo fixture: GOOD_DAMAGE in both damage photos. Before the merge: full [600, 2800]."""
    e = estimate.rough_range(reading([dmg()], [dmg()]))
    assert (e["full"], e["likely"], e["midpoint"]) == ([300, 1400], [600, 1100], 850)
    [one] = e["priced"]
    assert one["evidence"] == [{"item_id": "i0", "slot_id": "damage_closeup", "number": 1},
                               {"item_id": "i1", "slot_id": "damage_wide", "number": 1}]


def test_the_merged_damage_keeps_the_highest_severity():
    e = estimate.rough_range(reading([dmg(severity="minor")], [dmg(severity="severe")]))
    [one] = e["priced"]
    assert (one["severity"], one["slot_id"]) == ("severe", "damage_wide")


def test_different_damages_in_two_photos_are_both_priced():
    e = estimate.rough_range(reading([dmg()], [HOOD]))
    assert [p["damage_type"] for p in e["priced"]] == ["BUMPER_DAMAGE", "BODY_PANEL_DENT"]
