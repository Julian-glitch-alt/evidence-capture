"""T23 flags, T24 lane, T25 proposal, pure (contract: tasks/t22-t25-design.md)."""

import pytest

from evidence_server import estimate, proposal
from test_consistency import dmg, reading

READ = reading([dmg()], [dmg(area="Front bumper")])  # two separate moderate bumper damages: likely [1350, 2050]
EST = estimate.rough_range(READ)
EV = {"item_id": "i1", "slot_id": "damage_wide", "number": 2}


def row(check_id, result="pass", reason_code=None, reason="ok", value=None, inputs=None):
    return {"check_id": check_id, "result": result, "reason_code": reason_code, "reason": reason,
            "value": value, "inputs": inputs if inputs is not None else {}}


CLEAN = [row("coverage.in_force"), row("coverage.deductible", "value", value=500), row("photo.plate", inputs={"item_id": "p"}),
         row("consistency.areas"), row("consistency.prior_damage"), row("consistency.story")]


def flagged(checks=CLEAN, codes=(), r=READ):
    return proposal.flags(list(checks), list(codes), r)


# --- flags (T23) ---


def test_clean_claim_has_no_flags():
    assert flagged() == {"flags": [], "notes": []}


def test_fail_is_a_conflict_and_unclear_a_review():
    f = flagged(CLEAN + [row("coverage.driver_listed", "unclear", "DRIVER_NOT_LISTED", "Driver not listed."),
                         row("consistency.story", "fail", "STORY_INCONSISTENT", "Does not fit.", value=None)])["flags"]
    assert [(x["source"], x["level"], x["reason_code"], x["reason"], x["evidence"]) for x in f] == [
        ("coverage.driver_listed", "review", "DRIVER_NOT_LISTED", "Driver not listed.", []),
        ("consistency.story", "conflict", "STORY_INCONSISTENT", "Does not fit.", []),
    ]


def test_evidence_comes_from_each_check():
    checks = [
        row("consistency.areas", "unclear", "DAMAGE_OUTSIDE_STATEMENT", "a", value={"undeclared": ["Hood"], "not_seen": [], "evidence": [EV]}),
        row("consistency.prior_damage", "unclear", "PRIOR_DAMAGE_SUSPECTED", "b", value={"old": [EV]}),
        row("consistency.story", "fail", "STORY_INCONSISTENT", "c", value={"reasons": [{"text": "x", "evidence": [EV]}, {"text": "y", "evidence": [EV, {**EV, "number": 1}]}]}),
        row("photo.vin_photo", "fail", "VIN_MISMATCH", "d", inputs={"item_id": "v1", "photo.vin": []}),
        row("photo.plate", "unclear", "PHOTO_UNCHECKED", "e", inputs={}),
    ]
    f = flagged(checks)["flags"]
    assert [x["evidence"] for x in f] == [[EV], [EV], [EV, {**EV, "number": 1}], [{"item_id": "v1", "slot_id": "vin_photo"}], []]


def test_unchecked_licence_is_only_a_note():
    f = flagged(CLEAN + [row("photo.driver_license", "unclear", "PHOTO_UNCHECKED", "Not checked.", inputs={"item_id": "l"})])
    assert f["flags"] == []
    assert [(n["level"], n["source"]) for n in f["notes"]] == [("note", "photo.driver_license")]


def test_another_unchecked_photo_is_a_review():
    f = flagged(CLEAN + [row("photo.vin_photo", "unclear", "PHOTO_UNCHECKED", "Not checked.", inputs={"item_id": "v"})])
    assert [x["level"] for x in f["flags"]] == ["review"]


def test_submit_codes_are_reviews_without_double_counting():
    checks = CLEAN + [row("coverage.vehicle_listed", "fail", "VIN_MISMATCH", "VIN not on the policy.")]
    f = flagged(checks, ["VIN_MISMATCH", "INJURY_REPORTED", "SOMETHING_NEW"])["flags"]
    assert [(x["source"], x["level"], x["reason_code"], x["reason"]) for x in f] == [
        ("coverage.vehicle_listed", "conflict", "VIN_MISMATCH", "VIN not on the policy."),
        ("submit", "review", "INJURY_REPORTED", "Someone was hurt."),
        ("submit", "review", "SOMETHING_NEW", "Needs a person: SOMETHING_NEW."),
    ]


def test_a_note_does_not_hide_a_submit_code():
    checks = CLEAN + [row("photo.driver_license", "unclear", "PHOTO_UNCHECKED", "n")]
    assert [x["reason_code"] for x in flagged(checks, ["PHOTO_UNCHECKED"])["flags"]] == ["PHOTO_UNCHECKED"]


def test_severe_water_or_fire_may_be_a_total_loss():
    r = reading([dmg(damage_type="WATER_DAMAGE", severity="severe")], [dmg(damage_type="FIRE_DAMAGE", severity="moderate")])
    [f] = flagged(r=r)["flags"]
    assert (f["source"], f["level"], f["reason_code"]) == ("estimate", "review", "POSSIBLE_TOTAL_LOSS")
    assert f["reason"] == "Severe water or fire damage: the vehicle may be a total loss."
    assert f["evidence"] == [{"item_id": "i0", "slot_id": "damage_closeup", "number": 1}]


# --- lane (T24) ---


def conflicts(n):
    return CLEAN + [row(f"coverage.x{i}", "fail", "STORY_INCONSISTENT", f"c{i}") for i in range(n)]


def test_two_independent_conflicts_suggest_siu_screening():
    checks = CLEAN + [row("coverage.in_force", "fail", "POLICY_NOT_IN_FORCE", "Outside the period."),
                      row("photo.plate", "fail", "PLATE_MISMATCH", "Plate not on the policy.")]
    l = proposal.lane_rule_v1(flagged(checks), READ, EST)
    assert (l["lane"], l["label"], l["rule"], l["reason"]) == (
        "siu_referral", "SIU screening suggested", "R1_SIU", "2 independent conflicts: policy period, vehicle identity.")


# --- root causes before lanes (judge review 1 Nov, Claude #6) ---


def test_a_newly_bought_car_is_one_cause_not_siu():
    """Coverage VIN mismatch + photo VIN mismatch + plate mismatch: one fact (the car is not on the policy)."""
    checks = CLEAN + [row("coverage.vehicle_listed", "fail", "VIN_MISMATCH", "VIN not listed."),
                      row("photo.vin_photo", "fail", "VIN_MISMATCH", "VIN not listed.", inputs={"item_id": "v"}),
                      row("photo.plate", "fail", "PLATE_MISMATCH", "Plate not listed.", inputs={"item_id": "p"})]
    f = flagged(checks, ["VIN_MISMATCH"])
    assert {x["cause"] for x in f["flags"]} == {"vehicle_identity"}
    l = proposal.lane_rule_v1(f, READ, EST)
    assert (l["lane"], l["rule"]) == ("adjuster_review", "R2_REVIEW")
    [cause] = proposal.causes(f["flags"])
    assert (cause["cause"], cause["level"], cause["rows"], cause["model_only"]) == ("vehicle_identity", "conflict", 3, False)


def test_model_only_verdicts_never_count_toward_siu():
    # two model-only conflicts, even on top of one code conflict, stay in adjuster review
    checks = conflicts(2) + [row("photo.plate", "fail", "PLATE_MISMATCH", "Plate not listed.")]
    l = proposal.lane_rule_v1(flagged(checks), READ, EST)
    assert (l["lane"], l["rule"]) == ("adjuster_review", "R2_REVIEW")
    assert all(x["model_only"] for x in flagged(conflicts(2))["flags"])


def test_photo_quality_problems_on_two_photos_are_one_cause():
    checks = CLEAN + [row("photo.plate", "fail", "PHOTO_UNREADABLE", "x"), row("photo.odometer", "fail", "PHOTO_POOR_QUALITY", "y")]
    assert proposal.lane_rule_v1(flagged(checks), READ, EST)["lane"] == "adjuster_review"


def test_an_unknown_code_is_its_own_cause():
    checks = CLEAN + [row("coverage.a", "fail", "NEW_A", "a"), row("coverage.b", "fail", "NEW_B", "b")]
    assert proposal.lane_rule_v1(flagged(checks), READ, EST)["lane"] == "siu_referral"


@pytest.mark.parametrize("checks, reason", [
    (conflicts(1), "1 finding to review."),
    (CLEAN + [row("coverage.a", "unclear", "X", "a"), row("coverage.b", "unclear", "Y", "b")], "2 findings to review."),
])
def test_any_flag_means_adjuster_review(checks, reason):
    l = proposal.lane_rule_v1(flagged(checks), READ, EST)
    assert (l["lane"], l["rule"], l["reason"]) == ("adjuster_review", "R2_REVIEW", reason)


def test_no_range_means_adjuster_review():
    r = reading([dmg()], statuses=["failed"])
    l = proposal.lane_rule_v1(flagged(r=r), r, estimate.rough_range(r))
    assert (l["lane"], l["reason"]) == ("adjuster_review", "The damage photos could not be priced.")


def test_clean_and_cheap_is_fast_track():
    l = proposal.lane_rule_v1(flagged(), READ, EST)
    assert (l["lane"], l["label"], l["rule"], l["reason"]) == ("fast_track", "Fast track", "R3_FAST", "All checks passed; likely cost up to $2,050.")


@pytest.mark.parametrize("likely_high, lane", [(2500, "fast_track"), (2550, "standard")])
def test_fast_track_limit_is_the_likely_high_end(likely_high, lane):
    e = {**EST, "likely": [1000, likely_high]}
    assert proposal.lane_rule_v1(flagged(), READ, e)["lane"] == lane


def test_incomplete_or_nothing_priced_is_standard():
    assert proposal.lane_rule_v1(flagged(), READ, {**EST, "status": "incomplete"})["rule"] == "R4_STANDARD"
    r = reading([])
    l = proposal.lane_rule_v1(flagged(r=r), r, estimate.rough_range(r))
    assert (l["lane"], l["reason"]) == ("standard", "All checks passed; the cost needs a full estimate.")


# --- proposal (T25) ---


def test_clean_proposal():
    p = proposal.build(CLEAN, [], READ, EST, 500)
    ev, by_cost = p.pop("evidence"), p.pop("lane_cost")
    assert p == {
        "version": proposal.VERSION, "lane": "fast_track", "label": "Fast track", "rule": "R3_FAST",
        "headline": "All checks passed; likely cost up to $2,050.", "reasons": ["All checks passed."],
        "lane_rule": "count",
        "lane_v1": {"lane": "fast_track", "label": "Fast track", "rule": "R3_FAST",
                    "reason": "All checks passed; likely cost up to $2,050."},
        "flags": [], "notes": [], "causes": [], "estimate": EST, "deductible": 500, "suggested_reserve": 1200,
        "next_step": "Send to estimating for the final estimate; payment needs a person's approval.",
    }
    assert (ev["band"], ev["weight_log10"], ev["contributions"], ev["missing"]) == ("low", 0.0, [], [])
    assert (by_cost["lane"], by_cost["cost"]["eligible"], by_cost["cost"]["suggest"]) == ("fast_track", False, False)


def test_reasons_put_conflicts_first_and_keep_three():
    checks = CLEAN + [row("coverage.a", "unclear", "A", "review a"), row("coverage.b", "fail", "B", "conflict b"),
                      row("coverage.c", "unclear", "C", "review c"), row("coverage.d", "unclear", "D", "review d")]
    p = proposal.build(checks, [], READ, EST, 500)
    assert p["reasons"] == ["conflict b", "review a", "review c"]
    assert p["next_step"] == "Review the flags, then approve, edit or send back."


@pytest.mark.parametrize("deductible, reserve", [(500, 1200), (None, 1700), (2000, 0)])
def test_suggested_reserve_is_midpoint_minus_deductible(deductible, reserve):
    assert proposal.build(CLEAN, [], READ, EST, deductible)["suggested_reserve"] == reserve


def test_no_range_no_reserve():
    r = reading([dmg()], statuses=["failed"])
    p = proposal.build(CLEAN, [], r, estimate.rough_range(r), 500)
    assert (p["suggested_reserve"], p["lane"]) == (None, "adjuster_review")


def test_every_lane_has_a_label_and_a_next_step_and_none_rejects():
    assert set(proposal.LANES) == set(proposal.NEXT_STEPS) == {"siu_referral", "adjuster_review", "fast_track", "standard"}
    for text in list(proposal.LANES.values()) + list(proposal.NEXT_STEPS.values()):
        assert "reject" not in text.lower() and "deny" not in text.lower()
