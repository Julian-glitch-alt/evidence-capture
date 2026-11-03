"""Track 5: weight of evidence per root cause and the expected-cost lane rule, pure.

Hand-worked numbers use the shipped weights (reference/evidence_weights.json): prior 1 in 20
(log10 odds -1.27875), vehicle identity conflict 0.5, odometer conflict 0.5, SIU review $1,000,
honest friction $250, recovery rate 0.5.
"""

import json
import logging

import pytest

from evidence_server import estimate, evidence, proposal
from test_consistency import dmg, reading
from test_proposal import CLEAN, READ, row

EST = estimate.rough_range(READ)  # midpoint $1,700


def flagged(extra=(), codes=(), notes=()):
    f = proposal.flags(CLEAN + list(extra), list(codes), READ)
    f["notes"] += list(notes)
    return f


def priced(midpoint):
    return {**EST, "midpoint": midpoint}


PLATE = row("photo.plate", "fail", "PLATE_MISMATCH", "Plate not listed.", inputs={"item_id": "p"})
VIN_FORM = row("coverage.vehicle_listed", "fail", "VIN_MISMATCH", "VIN not listed.")
VIN_PHOTO = row("photo.vin_photo", "fail", "VIN_MISMATCH", "VIN not listed.", inputs={"item_id": "v"})
ODOMETER = row("photo.odometer", "fail", "ODOMETER_IMPLAUSIBLE", "Mileage implausible.", inputs={"item_id": "o"})
STORY = row("consistency.story", "fail", "STORY_INCONSISTENT", "Does not fit.")
PRIOR = row("consistency.prior_damage", "fail", "PRIOR_DAMAGE_SUSPECTED", "Old damage.")
SCREEN = row("capture.screen", "unclear", "SCREEN_RECAPTURE_SUSPECTED", "Maybe a screen.")
UNCHECKED = row("photo.vin_photo", "unclear", "PHOTO_UNCHECKED", "Not checked.", inputs={"item_id": "v"})


# --- the score ---


def test_a_clean_claim_has_no_evidence_and_scores_the_prior():
    ev = proposal.evidence(flagged())
    assert (ev["weight_log10"], ev["band"], ev["contributions"], ev["missing"]) == (0.0, "low", [], [])
    assert ev["score_log10"] == ev["prior_log10"] == -1.279
    assert ev["prior_note"] == "Prior: about 1 in 20 claims needs investigation (assumed)."
    assert ev["note"] == "Weights are assumptions, set in configuration."


def test_one_cause_counts_once_however_many_rows_report_it():
    """Form VIN + photo VIN + plate: one fact (the car is not on the policy), one likelihood ratio."""
    ev = proposal.evidence(flagged([VIN_FORM, VIN_PHOTO, PLATE], ["VIN_MISMATCH"]))
    assert [(c["cause"], c["level"], c["log10_lr"], c["source"]) for c in ev["contributions"]] == [
        ("vehicle_identity", "conflict", 0.5, "assumed")]
    assert (ev["weight_log10"], ev["band"]) == (0.5, "moderate")


def test_independent_causes_add_up():
    ev = proposal.evidence(flagged([PLATE, ODOMETER]))
    assert (ev["weight_log10"], ev["score_log10"], ev["band"]) == (1.0, -0.279, "strong")


def test_review_level_weighs_less_than_conflict():
    review = proposal.evidence(flagged([row("photo.plate", "unclear", "PLATE_MISMATCH", "x")]))
    assert review["contributions"][0]["log10_lr"] == 0.2


def test_a_check_that_did_not_run_is_missing_never_clean_and_never_negative():
    ev = proposal.evidence(flagged([UNCHECKED, row("coverage.in_force", "unclear", "POLICY_NOT_VERIFIED", "n")]))
    assert ev["weight_log10"] == 0.0 and ev["contributions"] == []
    assert [(m["cause"], m["codes"]) for m in ev["missing"]] == [
        ("analysis_missing", ["PHOTO_UNCHECKED"]), ("policy_on_file", ["POLICY_NOT_VERIFIED"])]


def test_an_unchecked_licence_note_is_listed_as_missing():
    note = {"source": "photo.driver_license", "level": "note", "reason_code": "PHOTO_UNCHECKED",
            "cause": "analysis_missing", "model_only": False}
    ev = proposal.evidence(flagged(notes=[note]))
    assert [m["cause"] for m in ev["missing"]] == ["analysis_missing"] and ev["weight_log10"] == 0.0


def test_model_only_causes_are_capped_together():
    ev = proposal.evidence(flagged([STORY, PRIOR, SCREEN]))
    assert [(c["cause"], c["log10_lr"], c["model_only"]) for c in ev["contributions"]] == [
        ("story", 0.3, True)]  # the first model-only cause (row order) takes the whole 0.3 cap
    assert ev["neutral"] == ["earlier damage", "photo of a screen or print"]
    assert ev["weight_log10"] == 0.3


def test_routing_reasons_are_neutral_not_evidence():
    ev = proposal.evidence(flagged(codes=["MULTI_PARTY", "INJURY_REPORTED"]))
    assert ev["contributions"] == [] and ev["neutral"] == ["another vehicle", "injury"]


def test_an_unknown_cause_gets_the_default_weight_and_one_warning(caplog):
    evidence._warned.discard("BRAND_NEW_CODE")
    with caplog.at_level(logging.WARNING, logger="evidence_server.evidence"):
        ev = proposal.evidence(flagged([row("signals.new", "fail", "BRAND_NEW_CODE", "x")]))
        proposal.evidence(flagged([row("signals.new", "fail", "BRAND_NEW_CODE", "x")]))
    assert [(c["cause"], c["log10_lr"]) for c in ev["contributions"]] == [("BRAND_NEW_CODE", 0.3)]
    assert [r.getMessage() for r in caplog.records].count(
        "evidence: no weight for cause 'BRAND_NEW_CODE' (codes BRAND_NEW_CODE); using the default") == 1


@pytest.mark.parametrize("weight, band", [(0.0, "low"), (0.49, "low"), (0.5, "moderate"), (0.99, "moderate"), (1.0, "strong")])
def test_bands_follow_kass_and_raftery(weight, band):
    assert evidence.band(weight) == band


# --- the config ---


def test_every_known_root_cause_has_a_labelled_weight():
    table = evidence.WEIGHTS["causes"]
    assert set(proposal.ROOT_CAUSES.values()) <= set(table)
    for cause, cfg in table.items():
        assert cfg["source"] and cfg["rationale"], cause
        if cfg.get("missing"):
            assert not set(cfg) & set(evidence.LEVELS), cause
            continue
        assert all(cfg[level] >= 0 for level in evidence.LEVELS), cause  # no negative weight for anything
    for key in ("prior", "model_only_cap", "default"):
        assert evidence.WEIGHTS[key]["source"] == "assumed"
    assert all(v["source"] == "assumed" and v["rationale"] for v in evidence.WEIGHTS["cost"].values())


def test_model_only_causes_match_track_2():
    assert {c for c, cfg in evidence.WEIGHTS["causes"].items() if "Model-only" in cfg["rationale"]} == proposal.MODEL_ONLY


# --- the expected-cost rule ---


def test_break_even_value_hand_worked():
    """Weight 1.0: odds 10/19, P = 10/29; break-even = (1000 + 19/29 * 250) / (10/29 * 0.5) = 33750 / 5 = $6,750."""
    test = evidence.siu_test(proposal.evidence(flagged([PLATE, ODOMETER])), 15000)
    assert (test["eligible"], test["break_even_value"], test["suggest"]) == (True, 6750, True)
    assert test["parameters"] == {"siu_review_cost": 1000, "honest_friction_cost": 250, "recovery_rate": 0.5}


@pytest.mark.parametrize("value, lane, rule", [(300, "adjuster_review", "R2_REVIEW"), (15000, "siu_referral", "C1_SIU_COST")])
def test_the_same_signals_send_a_small_claim_to_review_and_a_large_one_to_siu(value, lane, rule):
    f = flagged([PLATE, ODOMETER])
    out = proposal.lane_cost(f, READ, priced(value), proposal.evidence(f))
    assert (out["lane"], out["rule"]) == (lane, rule)
    assert proposal.lane_rule_v1(f, READ, priced(value))["lane"] == "siu_referral"  # counting ignores the value


def test_cost_reasons_name_the_causes_and_the_break_even_value():
    f = flagged([PLATE, ODOMETER])
    big = proposal.lane_cost(f, READ, priced(15000), proposal.evidence(f))
    assert big["reason"] == ("Strong evidence (vehicle identity, odometer) on a claim near $15,000: "
                             "screening pays off above about $6,750 (assumed weights).")
    small = proposal.lane_cost(f, READ, priced(300), proposal.evidence(f))
    assert small["reason"] == ("2 findings to review. SIU screening would pay off only above about $6,750; "
                               "likely cost near $300.")


def test_one_code_conflict_on_a_large_claim_stays_in_review():
    """A newly bought car (vehicle identity alone): odds 10**0.5 / 19, P = 0.1427, break-even about $17,050."""
    f = flagged([VIN_FORM, VIN_PHOTO, PLATE])
    out = proposal.lane_cost(f, READ, priced(15000), proposal.evidence(f))
    assert (out["lane"], out["cost"]["break_even_value"]) == ("adjuster_review", 17050)


def test_model_only_evidence_never_reaches_siu_whatever_the_value():
    f = flagged([STORY, PRIOR, SCREEN])
    out = proposal.lane_cost(f, READ, priced(10_000_000), proposal.evidence(f))
    assert (out["lane"], out["cost"]["eligible"]) == ("adjuster_review", False)


def test_a_clean_expensive_claim_is_never_sent_to_siu():
    f = flagged()
    out = proposal.lane_cost(f, READ, priced(10_000_000), proposal.evidence(f))
    assert out["lane"] in ("fast_track", "standard") and out["cost"]["suggest"] is False


def test_an_unpriced_claim_falls_back_to_the_counting_rule_and_says_so():
    r = reading([dmg()], statuses=["failed"])
    f = proposal.flags(CLEAN + [PLATE, ODOMETER], [], r)
    out = proposal.lane_cost(f, r, estimate.rough_range(r), proposal.evidence(f))
    assert (out["lane"], out["rule"]) == ("siu_referral", "R1_SIU")
    assert out["reason"].endswith("Not priced, so the counting rule decided.")
    assert (out["cost"]["fallback"], out["cost"]["value_basis"], out["cost"]["claim_value"]) == (True, "not priced", None)


# --- the proposal ---


def test_build_returns_both_rules_and_the_option_picks_the_lane():
    checks = CLEAN + [PLATE, ODOMETER]
    count = proposal.build(checks, [], READ, priced(300), 500, "count")
    cost = proposal.build(checks, [], READ, priced(300), 500, "cost")
    assert (count["lane"], count["lane_v1"]["lane"], count["lane_cost"]["lane"]) == ("siu_referral", "siu_referral", "adjuster_review")
    assert (cost["lane"], cost["lane_rule"], cost["next_step"]) == (
        "adjuster_review", "cost", "Review the flags, then approve, edit or send back.")
    assert count["evidence"] == cost["evidence"]


def test_an_unknown_lane_rule_is_refused():
    with pytest.raises(ValueError):
        proposal.build(CLEAN, [], READ, EST, 500, "vibes")


def test_no_probability_or_percentage_of_fraud_anywhere_in_the_proposal():
    p = proposal.build(CLEAN + [PLATE, ODOMETER, STORY], [], READ, priced(15000), 500, "cost")
    text = json.dumps({k: p[k] for k in ("evidence", "lane_cost", "lane_v1", "headline")}).lower()
    assert "%" not in text and "fraud" not in text and "prob" not in text
