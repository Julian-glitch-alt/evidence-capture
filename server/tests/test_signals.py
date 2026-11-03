"""Cheap signals, pure (docs/explorations/algorithms.md): coaching trail, screen re-capture, FNOL vs statement."""

from evidence_server import photo_prompts, proposal, signals, vocab
from test_loss_notice import DAMAGE_ANSWERS
from test_photo_check import ctx, reads, vin_reads


def shot(item, slot, readings, superseded=False, at="2026-10-28T15:00:00+00:00", status="done"):
    return {"item_id": item, "slot_id": slot, "received_at": at, "superseded": superseded, "status": status,
            "readings": readings, "started_at": at}


ANSWERS = {**DAMAGE_ANSWERS}


# --- 4. coaching trail ---


def test_a_superseded_shot_of_another_plate_is_shown_to_the_adjuster():
    rows = [shot("p1", "plate", reads("plate", text="8ABC123"), superseded=True, at="2026-10-28T15:00:01+00:00"),
            shot("p2", "plate", reads("plate"), at="2026-10-28T15:00:02+00:00")]
    r = signals.retake_trail(rows, ctx())
    assert (r["check_id"], r["result"], r["reason_code"]) == ("capture.retake_trail", "unclear", "RETAKE_AFTER_MISMATCH")
    assert r["value"]["evidence"] == [{"item_id": "p1", "slot_id": "plate", "earlier": True}]
    assert r["value"]["shots"][0]["reason"] == "Plate 8ABC123 does not match any vehicle on the policy."
    assert r["reason"] == "1 earlier shot did not match the claim and was retaken (license plate). Look at the earlier shot."


def test_a_blurry_retake_or_a_voluntary_retake_is_not_coaching():
    rows = [shot("p1", "plate", reads("plate", problem="blurry"), superseded=True),
            shot("v1", "vin_photo", vin_reads("1HGCV1F39LA526018"), superseded=True),
            shot("p2", "plate", reads("plate"))]
    assert signals.retake_trail(rows, ctx()) is None


def test_the_current_mismatching_shot_is_already_a_photo_check_not_the_trail():
    assert signals.retake_trail([shot("p1", "plate", reads("plate", text="8ABC123"))], ctx()) is None


def test_form_edits_after_a_photo_hint_become_a_finding_with_the_photo():
    events = [{"type": "answers.changed_after_photo_hint",
               "data": {"item_id": "d1", "slot": "damage_closeup", "reason_code": "DAMAGE_AREA_MISMATCH",
                        "field": "damaged_areas", "before": ["Front bumper"], "after": ["Rear bumper"]}}]
    r = signals.hint_edits(events)
    assert (r["result"], r["reason_code"]) == ("unclear", "ANSWER_CHANGED_AFTER_HINT")
    assert r["value"]["evidence"] == [{"item_id": "d1", "slot_id": "damage_closeup", "earlier": True}]
    assert r["value"]["edits"][0]["before"] == ["Front bumper"] and r["value"]["edits"][0]["after"] == ["Rear bumper"]
    assert signals.hint_edits([{"type": "claim.opened", "data": {}}]) is None


def test_trail_and_edits_share_one_root_cause():
    assert proposal.ROOT_CAUSES["RETAKE_AFTER_MISMATCH"] == proposal.ROOT_CAUSES["ANSWER_CHANGED_AFTER_HINT"] == "coaching"


# --- 5. screen or print ---


def test_the_screen_question_is_in_every_photo_read():
    for prompts in photo_prompts.READS.values():
        for prompt in prompts:
            assert '"screen_or_print"' in prompt


def test_a_screen_answer_becomes_a_review_finding_never_a_retake():
    readings = reads("plate", screen_or_print="yes")
    assert signals.photo_check.decide("plate", readings, ctx())["result"] == "pass"  # the claimant is not blocked
    r = signals.screen([shot("p1", "plate", readings), shot("d1", "damage_closeup", reads("damage_closeup", screen_or_print="no"))])
    assert (r["check_id"], r["result"], r["reason_code"]) == ("capture.screen", "unclear", "SCREEN_RECAPTURE_SUSPECTED")
    assert r["value"]["evidence"] == [{"item_id": "p1", "slot_id": "plate"}]
    assert r["inputs"] == {"photo.screen_yes": 1, "photo.screen_answered": 2, "photo.screen_missing": 0}
    assert "not a finding of fraud" in r["reason"]


def test_one_of_two_vin_reads_saying_screen_is_enough_and_earlier_shots_count():
    first, second = vin_reads("1HGCV1F39LA526018")
    r = signals.screen([shot("v1", "vin_photo", [first, {**second, "screen_or_print": "yes"}], superseded=True)])
    assert r["value"]["evidence"] == [{"item_id": "v1", "slot_id": "vin_photo", "earlier": True}]


def test_no_answer_is_missing_not_a_pass():
    assert signals.screen([shot("p1", "plate", reads("plate"))]) is None  # reads before the question: no row
    assert signals.screen([shot("p1", "plate", reads("plate", screen_or_print="no"))]) is None


def test_screen_is_model_only():
    assert proposal.ROOT_CAUSES["SCREEN_RECAPTURE_SUSPECTED"] in proposal.MODEL_ONLY


# --- 6. first notice vs statement ---


def test_same_date_and_cause_pass():
    r = signals.fnol({"date": "2026-10-27", "cause": "collision", "description": "Backed into a pole."}, ANSWERS)
    assert (r["check_id"], r["result"], r["reason_code"]) == ("fnol.statement", "pass", None)
    assert r["reason"] == "The statement matches the first notice of loss on loss date, cause."


def test_a_different_date_is_a_review_finding_with_both_texts():
    r = signals.fnol({"date": "2026-10-20", "description": "Hit a pole."}, ANSWERS)
    assert (r["result"], r["reason_code"]) == ("unclear", "FNOL_MISMATCH")
    assert r["value"]["differences"] == [{"field": "loss date", "fnol": "2026-10-20", "statement": "2026-10-27"}]
    assert r["value"]["fnol"]["description"] == "Hit a pole."
    assert r["value"]["statement"]["description"] == "Backed into a bollard in a parking lot."


def test_comprehensive_first_notice_against_a_collision_statement():
    r = signals.fnol({"cause": "comprehensive"}, ANSWERS)
    assert r["value"]["differences"] == [{"field": "cause", "fnol": "comprehensive", "statement": "Hit an object (collision)"}]


def test_front_versus_rear_contradiction_in_the_texts():
    a = {**ANSWERS, "description": "Someone hit my front bumper at a light."}
    r = signals.fnol({"description": "Insured was rear-ended at a light."}, a)
    assert r["value"]["differences"] == [{"field": "impact end", "fnol": "rear", "statement": "front"}]


def test_texts_that_name_both_ends_or_in_front_of_are_not_compared():
    a = {**ANSWERS, "description": "Parked in front of the store; my rear bumper was hit."}
    r = signals.fnol({"description": "Rear bumper damage while parked."}, a)
    assert r["result"] == "pass" and "impact end" in r["value"]["compared"]
    both = {**ANSWERS, "description": "Front and rear damage."}
    assert "impact end" not in signals.fnol({"description": "Rear damage."}, both)["value"]["compared"]


def test_nothing_comparable_is_attached_as_a_value():
    r = signals.fnol({"description": "Damage at the mall."}, {**ANSWERS, "loss_date": None})
    assert (r["result"], r["reason_code"]) == ("value", None)


def test_no_first_notice_is_missing():
    assert signals.fnol(None, ANSWERS) is None
    assert signals.fnol({}, ANSWERS) is None


def test_rows_reads_the_first_notice_from_claim_opened():
    events = [{"type": "claim.opened", "data": {"loss": {"date": "2026-10-20"}}}]
    [r] = signals.rows([], ctx(), events, ANSWERS)
    assert r["reason_code"] == "FNOL_MISMATCH"


def test_new_codes_are_in_the_vocabulary_and_have_a_cause():
    codes = {"SCREEN_RECAPTURE_SUSPECTED", "RETAKE_AFTER_MISMATCH", "ANSWER_CHANGED_AFTER_HINT", "FNOL_MISMATCH"}
    assert codes <= vocab.REASON_CODES
    assert codes <= set(proposal.ROOT_CAUSES)
