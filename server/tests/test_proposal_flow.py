"""T22–T25 proposal through the API (contract: tasks/t22-t25-design.md), with a fake model."""

from conftest import GOOD_DAMAGE, GOOD_READS, GOOD_STORY
from evidence_server import estimate, proposal
from test_damage_flow import captured_claim, send
from test_loss_notice import detail


def test_clean_claim_is_proposed_for_fast_track(client, model, clock):
    claim, _ = captured_claim(client, clock)
    p = send(client, claim)["proposal"]
    assert (p["status"], p["lane"], p["rule"], p["version"]) == ("ready", "fast_track", "R3_FAST", proposal.VERSION)
    e = p["estimate"]
    # the fixture shows one dent in the close-up and the wide photo: priced once (was [600, 2800], reserve 1200)
    assert (e["status"], e["full"], e["likely"], e["midpoint"]) == ("complete", [300, 1400], [600, 1100], 850)
    assert (p["deductible"], p["suggested_reserve"]) == (500, 350)
    [priced] = e["priced"]
    assert [x["slot_id"] for x in priced["evidence"]] == ["damage_closeup", "damage_wide"]
    assert p["flags"] == []
    assert [(n["source"], n["reason_code"]) for n in p["notes"]] == [("photo.driver_license", "PHOTO_UNCHECKED")]


def test_no_proposal_before_submit(client, model, clock):
    claim, _ = captured_claim(client, clock)
    assert detail(client, claim)["proposal"] is None


def test_pending_while_the_analyses_run(client, model, clock):
    claim, _ = captured_claim(client, clock)
    client.app.state.store.submit(claim["token"])  # without the background run
    assert detail(client, claim)["proposal"] == {"status": "pending"}


def test_a_story_that_does_not_fit_goes_to_adjuster_review(client, model, clock):
    model.set("damage", {**GOOD_DAMAGE, "damages": [{**GOOD_DAMAGE["damages"][0], "area": "Front bumper"}]})  # both photos
    model.set("story", {**GOOD_STORY, "consistent": "no", "reasons": [{"text": "Front damage, rear story.", "damages": [1]}]})
    claim, items = captured_claim(client, clock)
    p = send(client, claim)["proposal"]
    assert (p["lane"], p["rule"]) == ("adjuster_review", "R2_REVIEW")
    assert [(f["source"], f["level"]) for f in p["flags"]] == [("consistency.areas", "review"), ("consistency.story", "conflict")]
    assert p["reasons"][0] == "The statement does not fit the damage in the photos."
    assert p["flags"][1]["evidence"] == [{"item_id": items["damage_closeup"][0]["id"], "slot_id": "damage_closeup", "number": 1},
                                         {"item_id": items["damage_wide"][0]["id"], "slot_id": "damage_wide", "number": 1}]


def test_a_code_conflict_plus_a_model_verdict_is_not_siu(client, model, clock):
    """Plate mismatch (code) + story inconsistent (model only): was "Refer to SIU" before root causes."""
    model.set("plate", {**GOOD_READS["plate"][0], "text": "8ABC123"})
    model.set("story", {**GOOD_STORY, "consistent": "no"})
    claim, _ = captured_claim(client, clock)
    p = send(client, claim)["proposal"]
    assert (p["lane"], p["rule"]) == ("adjuster_review", "R2_REVIEW")
    assert [(c["cause"], c["level"], c["model_only"]) for c in p["causes"]] == [
        ("vehicle_identity", "conflict", False), ("story", "conflict", True)]


def test_two_independent_conflicts_on_a_small_claim_stay_in_review_under_the_cost_rule(client, model, clock):
    """Track 5: plate + odometer conflicts on a claim near $850; screening pays off only above $6,750."""
    model.set("plate", {**GOOD_READS["plate"][0], "text": "8ABC123"})
    model.set("odometer", {**GOOD_READS["odometer"][0], "reading": 900000})
    claim, _ = captured_claim(client, clock)
    p = send(client, claim)["proposal"]
    assert (p["lane_rule"], p["lane"], p["rule"]) == ("cost", "adjuster_review", "R2_REVIEW")
    assert p["lane_v1"]["lane"] == "siu_referral"
    assert p["lane_cost"]["cost"]["break_even_value"] == 6750
    assert "pay off only above about $6,750" in p["headline"]
    assert (p["evidence"]["band"], [c["cause"] for c in p["evidence"]["contributions"]]) == (
        "strong", ["vehicle_identity", "odometer"])


def test_two_independent_conflicts_suggest_siu_screening_under_the_count_rule(client, model, clock):
    client.app.state.store.lane_rule = "count"
    model.set("plate", {**GOOD_READS["plate"][0], "text": "8ABC123"})
    model.set("odometer", {**GOOD_READS["odometer"][0], "reading": 900000})
    claim, _ = captured_claim(client, clock)
    p = send(client, claim)["proposal"]
    assert (p["lane_rule"], p["lane"], p["label"]) == ("count", "siu_referral", "SIU screening suggested")
    assert p["headline"] == "2 independent conflicts: vehicle identity, odometer."
    assert p["next_step"] == "Screen for an SIU referral before any payment; referring is the adjuster's decision."


def test_model_down_means_review_without_a_range(client, model, clock):
    model.set("damage", RuntimeError("Meta is down"))
    claim, _ = captured_claim(client, clock)
    p = send(client, claim)["proposal"]
    assert (p["lane"], p["estimate"]["status"], p["suggested_reserve"]) == ("adjuster_review", "none", None)


def test_the_claimant_never_sees_the_proposal(client, model, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    r = client.get(f"/api/c/{claim['token']}")
    assert "proposal" not in r.json()
    for word in ("fast_track", "suggested_reserve", estimate.BASIS[:20]):
        assert word not in r.text
