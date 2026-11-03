"""Track 1: the AI summary sentence checks (ai_summary.verify), one rule per test group."""

import pytest

from evidence_server import ai_summary, report_pdf

ITEM = "4f9c2a7e1b3d4c5a8e6f0a1b2c3d4e5f"  # a uuid4 hex item id: its digits must not count as facts


def facts(**extra):
    out = {
        "F1": {"level": "conflict", "code": "STORY_INCONSISTENT",
               "reason": "The statement does not fit the damage in the photos.", "evidence": ["Wide #1"]},
        "D1": {"area": "Rear bumper", "type": "BUMPER_DAMAGE", "severity": "moderate", "age": "new",
               "label": "Close-up #1", "item_id": ITEM},
        "C:photo.vin_photo": {"result": "pass", "reason": "VIN 1HGCV1F39LA526018 matches the vehicle on the policy."},
        "C:photo.plate": {"result": "fail", "reason": "Plate ABC123 does not match any vehicle on the policy."},
        "C:photo.odometer": {"result": "unclear", "reason": "The photo could not be checked automatically."},
        "C:consistency.story": {"result": "fail", "reason": "The statement does not fit the damage in the photos."},
        "S": "Backed into a bollard at 5 mph.",
    }
    out.update(extra)
    return out


def one(text, refs, f=None):
    return ai_summary.verify({"sentences": [{"text": text, "refs": refs}]}, f or facts())


def ok(text, refs, f=None):
    out = one(text, refs, f)
    assert out["status"] == "verified", out["reasons"]


def rejected(text, refs, prefix, f=None):
    out = one(text, refs, f)
    assert out["status"] == "rejected" and out["sentences"] == []
    assert out["reasons"][0].startswith(prefix), out["reasons"]


# --- numbers: ids and hashes are not facts ---


def test_digits_of_an_item_id_do_not_let_a_number_through():
    assert "4" in ITEM and "9" in ITEM
    rejected("The rear bumper shows 4 dents.", ["D1"], "number: 4")
    rejected("Repairs need 9 hours.", ["D1"], "number: 9")


def test_hashes_in_any_value_are_ignored():
    f = facts(**{"C:seal": {"result": "pass", "reason": "Sealed with sha256 " + "0123456789abcdef" * 4}})
    rejected("The claim was sealed 7 times.", ["C:seal"], "number: 7", f)


def test_numbers_are_matched_as_whole_numbers_not_substrings():
    # 5 is in the statement ("5 mph"); 52 is not, although "526018" contains "52".
    ok("The claimant says the speed was 5 mph.", ["S"])
    rejected("The claimant says the speed was 52 mph.", ["S"], "number: 52")


def test_numbers_from_check_reasons_and_vin_are_allowed():
    ok("VIN 1HGCV1F39LA526018 is on the policy.", ["C:photo.vin_photo"])


def test_fact_keys_written_in_the_text_are_not_numbers():
    ok("The bumper damage [D1] is new.", ["D1"])


def test_counts_of_findings_and_damages_are_allowed():
    ok("There is 1 finding to review: the statement does not fit the damage.", ["F1"])


def test_fact_numbers_skips_keys_and_ids():
    nums = ai_summary.fact_numbers({"D1": {"item_id": "123456", "key": "abc:7", "label": "Wide #2"}})
    assert 123456 not in nums and 7 not in nums and 2 in nums


# --- polarity: a sentence must not flip the result it cites ---


@pytest.mark.parametrize("text, refs", [
    ("The plate matches the policy.", ["C:photo.plate"]),
    ("The plate check passed.", ["C:photo.plate"]),
    ("The odometer reading is confirmed.", ["C:photo.odometer"]),
    ("The statement fits the damage in the photos.", ["C:consistency.story"]),
    ("The statement is consistent with the damage.", ["F1"]),
    ("The statement agrees with the photos.", ["F1", "D1"]),
])
def test_citing_a_failed_or_unclear_result_cannot_sound_like_a_pass(text, refs):
    rejected(text, refs, "polarity:")


@pytest.mark.parametrize("text, refs", [
    ("The VIN does not match the policy.", ["C:photo.vin_photo"]),
    ("The VIN check failed.", ["C:photo.vin_photo"]),
    ("There is a VIN mismatch.", ["C:photo.vin_photo"]),
    ("The VIN is inconsistent with the policy.", ["C:photo.vin_photo"]),
])
def test_citing_only_passed_results_cannot_sound_like_a_failure(text, refs):
    rejected(text, refs, "polarity:")


@pytest.mark.parametrize("text, refs", [
    ("The plate does not match the policy.", ["C:photo.plate"]),
    ("The plate doesn't match any vehicle on the policy.", ["C:photo.plate"]),
    ("The statement does not fit the damage in the photos.", ["F1", "D1"]),
    ("The VIN matches the vehicle on the policy.", ["C:photo.vin_photo"]),
    ("The odometer could not be read automatically.", ["C:photo.odometer"]),
    ("The new rear bumper damage is moderate.", ["D1"]),  # damages carry no result: no polarity rule
])
def test_sentences_that_keep_the_polarity_pass(text, refs):
    ok(text, refs)


def test_mixing_a_pass_word_with_a_failed_check_is_rejected():
    rejected("The VIN matches, but the plate does not match.", ["C:photo.vin_photo", "C:photo.plate"], "polarity:")


# --- severity: no invented severity ---


def test_a_severity_word_must_be_a_cited_damage_severity():
    ok("The rear bumper has moderate new damage.", ["D1"])
    rejected("The rear bumper has severe new damage.", ["D1"], "severity: 'severe'")
    rejected("The statement describes minor damage.", ["S"], "severity: 'minor'")


# --- wording: accusation, decision and money words ---


@pytest.mark.parametrize("text", [
    "This looks like fraud.",
    "The loss seems fraudulent.",
    "The accident looks staged.",
    "Possible staging of the scene.",
    "The statement is not credible.",
    "The claimant may be lying.",
    "The photos could be fake.",
    "The damage looks faked.",
    "The story seems fabricated.",
    "The adjuster should approve the claim.",
    "The claim should be denied.",
    "Reject the claim.",
    "The insurer should pay the claimant.",
    "Payment can go out today.",
    "A payout is due.",
    "Settle the claim quickly.",
    "Set the reserve at the midpoint.",
    "The claimant should be reimbursed.",
])
def test_forbidden_words_are_rejected(text):
    rejected(text, ["F1"], "forbidden:")


@pytest.mark.parametrize("text", [
    "The damage lies on the rear bumper.",
    "The passenger side shows no damage.",
    "The claimant repainted the bumper before.",
])
def test_words_that_only_contain_a_forbidden_stem_pass(text):
    ok(text, ["D1"])


def test_total_loss_needs_the_total_loss_flag_cited():
    f = facts(F2={"level": "review", "code": "POSSIBLE_TOTAL_LOSS",
                  "reason": "Severe water or fire damage: the vehicle may be a total loss.", "evidence": []})
    rejected("The vehicle may be a total loss.", ["D1"], "forbidden: total loss", f)
    rejected("The car looks totaled.", ["F1"], "forbidden: total loss", f)
    ok("The vehicle may be a total loss.", ["F2"], f)


def test_flag_facts_carry_the_reason_code():
    f = ai_summary.facts({"flags": [{"level": "review", "reason_code": "POSSIBLE_TOTAL_LOSS", "reason": "r",
                                     "evidence": []}]})
    assert f["F1"]["code"] == "POSSIBLE_TOTAL_LOSS"


# --- caption ---


def test_caption_says_what_was_checked_and_not_verified():
    assert ai_summary.CAPTION == "Muse Spark · sentences checked for sources, numbers and wording"
    assert "verif" not in ai_summary.CAPTION.lower()


def _pdf_text(data: bytes) -> str:
    """The decompressed content streams of an fpdf2 PDF, as latin-1 text."""
    import re
    import zlib
    out = []
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        try:
            out.append(zlib.decompress(m.group(1)).decode("latin-1"))
        except zlib.error:
            out.append(m.group(1).decode("latin-1"))
    return "".join(out)


def test_pdf_prints_the_caption():
    from test_report import build
    r = build()
    ai = {"status": "verified", "sentences": [{"text": "The rear bumper dent is new.", "refs": ["D1"]}], "reasons": []}
    text = _pdf_text(report_pdf.render(r, {}, ai))
    assert "sentences checked for sources" in text
    assert "each sentence cites the findings" not in text


def test_claim_detail_carries_the_caption(client, model, clock):
    from test_damage_flow import captured_claim, send
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    assert d["ai_summary"]["status"] == "verified"
    assert d["ai_summary"]["caption"] == ai_summary.CAPTION


# --- T42: passive and modal negations are negative, not "match" (live claim CLM-20261101-87BD3D) ---


def test_the_live_sentence_that_was_wrongly_rejected_passes():
    ok("The driver stated someone hit the parked car and drove off, another vehicle was involved, "
       "no police report was given, and the statement could not be matched with confidence to the damage.",
       ["S", "C:consistency.story"])


@pytest.mark.parametrize("text", [
    "The statement could not be matched to the damage.",
    "The plate cannot be confirmed against the policy.",
    "The plate was not verified against the policy.",
    "The plate could not be matched.",
    "The plate is not a match for any vehicle on the policy.",
    "The plate did not match any vehicle on the policy.",
    "We were unable to match the plate to the policy.",
    "The plate shows no match with the policy.",
])
def test_negated_positive_words_count_as_negative(text):
    ok(text, ["C:photo.plate"])


def test_a_real_positive_next_to_a_failed_check_is_still_rejected():
    rejected("The plate matched the policy.", ["C:photo.plate"], "polarity")
    rejected("The VIN matches, but the plate could not be matched.", ["C:photo.vin_photo", "C:photo.plate"], "polarity")


def test_a_negated_word_on_a_passed_check_is_still_rejected():
    rejected("The VIN could not be matched to the policy.", ["C:photo.vin_photo"], "polarity")


# --- T44: the prompt states the length the verifier enforces ---


def test_the_prompt_states_the_word_limit():
    text = ai_summary.prompt(facts())
    assert "90 words" in text
    assert "90 words" in ai_summary.retry_prompt(facts(), {"sentences": []}, ["long: the summary is over 90 words"])
