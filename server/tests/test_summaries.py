"""T40 code summary, AI summary verifier and review state, pure (contract: tasks/t40-design.md)."""

import pytest

from evidence_server import ai_summary, report, review
from test_report import REVIEW_FLAG, SLOT_FLAG, build, proposal


def checks(vin="pass", plate="pass", story="pass"):
    return [{"check_id": "photo.vin_photo", "result": vin, "reason_code": "X", "reason": "VIN."},
            {"check_id": "photo.plate", "result": plate, "reason_code": "X", "reason": "Plate."},
            {"check_id": "consistency.story", "result": story, "reason_code": "X", "reason": "Story."}]


def with_checks(r, **kw):
    r["checks"] = [{**c, "title": c["check_id"]} for c in checks(**kw)]
    return r


# --- code summary (A) ---


def test_clean_claim_summary():
    r = with_checks(build())
    assert report.summary(r) == ("collision (Backed into a bollard). The VIN and plate photos match the vehicle on the policy"
                                 " and the damage fits the statement. Nothing to check: all checks passed.")


def test_summary_with_findings_and_a_bad_story():
    r = with_checks(build(proposal=proposal("adjuster_review", flags=[REVIEW_FLAG, SLOT_FLAG])), story="fail")
    s = report.summary(r)
    assert ", and the damage does not fit the statement." in s
    assert s.endswith("2 items to check: the statement does not fit the damage in the photos (Wide #1); "
                      "odometer unreadable (Odometer photo).")


def test_summary_vehicle_wording():
    assert "may not match the vehicle on the policy" in report.summary(with_checks(build(), plate="fail"))
    assert "could not be fully confirmed" in report.summary(with_checks(build(), vin="unclear"))
    assert report.summary(with_checks(build(), story="unclear")).count("statement") == 0


def test_no_policy_never_says_the_vehicle_matches():
    """Judge review 1 Nov (Claude #7): with no verified policy, even two passes must not read as a match."""
    r = with_checks(build())
    r["policy_verified"] = False
    s = report.summary(r)
    assert "No verified policy on file, so the vehicle could not be matched to a policy" in s
    assert "match the vehicle on the policy" not in s
    assert report.vehicle_matches_policy(r) is False


def test_pdf_seal_line_names_the_match_only_with_a_verified_policy():
    from evidence_server import report_pdf
    r = with_checks(build())
    assert "VIN and plate photos match the policy" in report_pdf.seal_text(r)
    r["policy_verified"] = False
    text = report_pdf.seal_text(r)
    assert "match the policy" not in text and "no verified policy on file" in text
    r = with_checks(build(), vin="unclear")
    assert "match the policy" not in report_pdf.seal_text(r)


def test_summary_cuts_long_descriptions_and_skips_dismissed_flags():
    r = with_checks(build(proposal=proposal("adjuster_review", flags=[REVIEW_FLAG])))
    r["loss"]["description"] = "word " * 40
    r["review"] = {"flags": {"consistency.story:STORY_DOES_NOT_FIT": {"verdict": "false_alarm"}}, "damages": {}, "notes": []}
    s = report.summary(r)
    assert s.startswith("collision (" + " ".join(["word"] * 16) + "…).")
    assert s.endswith("Nothing to check: all checks passed.")


def test_summary_is_pure():
    r = with_checks(build())
    assert report.summary(r) == report.summary(r)


# --- AI summary (B) verifier ---


def facts():
    return ai_summary.facts(with_checks(build(proposal=proposal("adjuster_review", flags=[REVIEW_FLAG]))))


def test_facts_are_keyed():
    f = facts()
    assert {"F1", "D1", "D2", "S", "C:photo.vin_photo"} <= set(f)
    assert "D3" not in f  # the rear bumper dent is in the close-up and the wide photo: one fact
    assert f["D1"]["label"] == "Close-up #1, Wide #1"
    assert f["S"] == "Backed into a bollard."


def test_prompt_starts_with_the_marker_and_carries_the_facts():
    p = ai_summary.prompt(facts())
    assert p.startswith(ai_summary.SUMMARY) and '"F1"' in p


def test_good_answer_is_verified():
    ans = {"sentences": [{"text": "The rear bumper dent fits backing into a bollard.", "refs": ["D1", "S"]},
                         {"text": "The statement does not fit one photo.", "refs": ["F1"]}]}
    out = ai_summary.verify(ans, facts())
    assert out["status"] == "verified" and len(out["sentences"]) == 2 and out["reasons"] == []


@pytest.mark.parametrize("ans, why", [
    (None, "shape"), ({"sentences": []}, "shape"), ({"sentences": [{"text": "x", "refs": ["D1"]}] * 5}, "shape"),
    ({"sentences": [{"text": "No refs here.", "refs": []}]}, "ref"),
    ({"sentences": [{"text": "Cites a ghost.", "refs": ["F9"]}]}, "ref"),
    ({"sentences": [{"text": "This looks like fraud to me.", "refs": ["F1"]}]}, "forbidden"),
    ({"sentences": [{"text": "You should approve it.", "refs": ["F1"]}]}, "forbidden"),
    ({"sentences": [{"text": "Repairs will cost 9,999 dollars.", "refs": ["D1"]}]}, "number"),
    ({"sentences": [{"text": "x" * 301, "refs": ["D1"]}]}, "long"),
])
def test_bad_answers_are_rejected(ans, why):
    out = ai_summary.verify(ans, facts())
    assert out["status"] == "rejected" and out["reasons"]


def test_numbers_present_in_the_facts_are_allowed():
    f = facts()
    f["C:photo.odometer"] = {"result": "unclear", "reason": "Read 56,947 mi once."}
    ans = {"sentences": [{"text": "The odometer shows 56947 miles.", "refs": ["C:photo.odometer"]}]}
    assert ai_summary.verify(ans, f)["status"] == "verified"


# --- review state ---


def row(kind, target, verdict=None, reason=None, text=None, at="2026-10-28T15:00:00+00:00"):
    return {"kind": kind, "target": target, "verdict": verdict, "reason": reason, "text": text,
            "decided_by": "J. Smith", "at": at}


def test_last_review_per_target_wins():
    s = review.state([row("flag", "a:X", "false_alarm", "Glare."), row("flag", "a:X", "confirmed", at="2026-10-28T15:01:00+00:00"),
                      row("damage", "it:1", "excluded", "Old wear."), row("note", None, text="Called the claimant.")])
    assert s["flags"]["a:X"]["verdict"] == "confirmed"
    assert s["damages"]["it:1"]["verdict"] == "excluded"
    assert [n["text"] for n in s["notes"]] == ["Called the claimant."]
    assert review.excluded(s) == {"it:1"}
    assert review.excluded(review.state([row("damage", "it:1", "excluded", "Old."), row("damage", "it:1", "included")])) == set()


def test_review_validation():
    fk, dk = {"a:X"}, {"it:1"}
    assert review.validate({"kind": "flag", "key": "a:X", "verdict": "confirmed"}, fk, dk)["verdict"] == "confirmed"
    for bad, msg in [({"kind": "flag", "key": "a:X", "verdict": "false_alarm"}, "reason"),
                     ({"kind": "flag", "key": "zzz", "verdict": "confirmed"}, "Unknown finding"),
                     ({"kind": "damage", "key": "it:9", "verdict": "excluded", "reason": "Old wear."}, "Unknown damage"),
                     ({"kind": "damage", "key": "it:1", "verdict": "excluded"}, "reason"),
                     ({"kind": "note", "text": "hi"}, "5"),
                     ({"kind": "rate", "key": "a:X"}, ".")]:
        with pytest.raises(ValueError, match=msg):
            review.validate(bad, fk, dk)
