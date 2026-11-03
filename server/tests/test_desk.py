"""T26 queue key, T32 cycle-time clock and T27 decision rules, pure (contract: tasks/t27-t26-t32-design.md)."""

import pytest

from evidence_server import decision, desk

SLOTS = ["plate", "vin_photo", "odometer", "damage_closeup", "damage_wide"]
FLAG = {"source": "consistency.story", "level": "conflict", "reason_code": "X", "reason": "Story does not fit.", "evidence": []}


def prop(lane="fast_track", flags=None, reserve=1200):
    return {"status": "ready", "lane": lane, "flags": flags or [], "suggested_reserve": reserve,
            "next_step": "Send to estimating for the final estimate; payment needs a person's approval."}


def ev(type_, at):
    return {"type": type_, "at": at}


# --- queue_key ---


@pytest.mark.parametrize("submitted, proposal, dec, key", [
    (None, None, None, "capturing"),
    ("2026-10-28T15:00:00+00:00", None, None, "analysing"),
    ("2026-10-28T15:00:00+00:00", {"status": "pending"}, None, "analysing"),
    ("2026-10-28T15:00:00+00:00", prop("siu_referral"), None, "siu_referral"),
    ("2026-10-28T15:00:00+00:00", prop("standard"), None, "standard"),
    ("2026-10-28T15:00:00+00:00", prop("standard"), {"action": "approve"}, "decided"),
])
def test_queue_key(submitted, proposal, dec, key):
    assert desk.queue_key(submitted, proposal, dec) == key


def test_queues_are_in_a_fixed_order():
    assert [k for k, _ in desk.QUEUES] == ["siu_referral", "adjuster_review", "fast_track", "standard",
                                          "analysing", "capturing", "decided"]


# --- cycle ---


def test_cycle_from_the_first_event_of_each_type():
    events = [ev("claim.opened", "2026-10-28T15:00:00+00:00"), ev("claim.submitted", "2026-10-28T15:04:30+00:00"),
              ev("report.ready", "2026-10-28T15:05:00+00:00"), ev("report.ready", "2026-10-28T16:00:00+00:00"),
              ev("decision.recorded", "2026-10-28T15:12:00+00:00")]
    assert desk.cycle(events) == {"link_to_submit": 270, "submit_to_report": 30, "report_to_decision": 420,
                                  "link_to_decision": 720}


def test_cycle_with_missing_stages_is_none():
    c = desk.cycle([ev("claim.opened", "2026-10-28T15:00:00+00:00")])
    assert c == {"link_to_submit": None, "submit_to_report": None, "report_to_decision": None, "link_to_decision": None}


def test_averages_skip_missing_and_round_half_up():
    out = desk.averages([{"link_to_submit": 10, "submit_to_report": None, "report_to_decision": None, "link_to_decision": None},
                         {"link_to_submit": 11, "submit_to_report": 4, "report_to_decision": None, "link_to_decision": None}])
    assert out["link_to_submit"] == {"mean_s": 11, "n": 2}
    assert out["submit_to_report"] == {"mean_s": 4, "n": 1}
    assert out["report_to_decision"] == {"mean_s": None, "n": 0}


# --- decision.validate ---


def test_approve_without_flags_needs_no_reason():
    c = decision.validate("approve", {}, prop(), SLOTS)
    assert (c["action"], c["label"], c["reason"], c["by"]) == ("approve", "Approved", None, "Adjuster")
    assert (c["lane_before"], c["lane_after"], c["reserve_before"], c["reserve_after"], c["slots"]) == (
        "fast_track", "fast_track", 1200, 1200, [])


def test_approve_with_flags_needs_a_reason():
    with pytest.raises(ValueError, match="reason"):
        decision.validate("approve", {"reason": "ok"}, prop("adjuster_review", [FLAG]), SLOTS)
    c = decision.validate("approve", {"reason": "  Looked at Wide #1, story fits.  "}, prop("adjuster_review", [FLAG]), SLOTS)
    assert c["reason"] == "Looked at Wide #1, story fits."


@pytest.mark.parametrize("action, extra", [
    ("edit_approve", {"lane": "standard"}), ("send_back", {"slots": ["odometer"]}),
    ("refer_siu", {}), ("field_inspection", {}),
])
def test_other_actions_need_a_reason(action, extra):
    with pytest.raises(ValueError, match="reason"):
        decision.validate(action, extra, prop(), SLOTS)
    assert decision.validate(action, {**extra, "reason": "Checked the photos."}, prop(), SLOTS)["action"] == action


def test_reason_length_limits():
    with pytest.raises(ValueError, match="too long"):
        decision.validate("refer_siu", {"reason": "x" * 501}, prop(), SLOTS)
    with pytest.raises(ValueError, match="too long"):
        decision.validate("approve", {"reason": "x" * 501}, prop(), SLOTS)


def test_unknown_action():
    with pytest.raises(ValueError, match="Unknown action"):
        decision.validate("pay", {"reason": "please pay"}, prop(), SLOTS)


def test_edit_must_change_something_valid():
    r = "Story and photos agree."
    with pytest.raises(ValueError, match="Pick a lane"):
        decision.validate("edit_approve", {"reason": r, "lane": "approve_payment"}, prop(), SLOTS)
    with pytest.raises(ValueError, match="Nothing was changed"):
        decision.validate("edit_approve", {"reason": r, "lane": "fast_track", "suggested_reserve": 1200}, prop(), SLOTS)
    for bad in (-1, 12.5, True, "100"):
        with pytest.raises(ValueError, match="whole number"):
            decision.validate("edit_approve", {"reason": r, "lane": "standard", "suggested_reserve": bad}, prop(), SLOTS)
    c = decision.validate("edit_approve", {"reason": r, "lane": "standard", "suggested_reserve": 2000}, prop(), SLOTS)
    assert (c["lane_after"], c["reserve_after"], c["label"]) == ("standard", 2000, "Edited and approved")
    c = decision.validate("edit_approve", {"reason": r, "lane": "fast_track", "suggested_reserve": 900}, prop(), SLOTS)
    assert (c["lane_after"], c["reserve_after"]) == ("fast_track", 900)


def test_send_back_needs_known_distinct_slots():
    r = "Odometer is unreadable."
    for slots in ([], ["windshield"], ["odometer", "odometer"], None):
        with pytest.raises(ValueError, match="photos to retake"):
            decision.validate("send_back", {"reason": r, "slots": slots}, prop(), SLOTS)
    assert decision.validate("send_back", {"reason": r, "slots": ["odometer", "vin_photo"]}, prop(), SLOTS)["slots"] == [
        "odometer", "vin_photo"]


def test_by_is_trimmed_and_capped():
    assert decision.validate("approve", {"by": "  J. Smith  "}, prop(), SLOTS)["by"] == "J. Smith"
    assert len(decision.validate("approve", {"by": "x" * 99}, prop(), SLOTS)["by"]) == 60


# --- outcome and note ---


@pytest.mark.parametrize("action, lane, status, subject, priority, days", [
    ("approve", "fast_track", "approved", "Send to estimating (fast track)", "normal", 1),
    ("approve", "standard", "approved", "Send to estimating", "normal", 2),
    ("approve", "adjuster_review", "approved", "Send to estimating", "normal", 2),
    ("approve", "siu_referral", "siu", "SIU referral review", "high", 1),
    ("send_back", "fast_track", "more_evidence", "Follow up on requested photos", "normal", 2),
    ("refer_siu", "fast_track", "siu", "SIU referral review", "high", 1),
    ("field_inspection", "fast_track", "handed_to_human", "Schedule field inspection", "high", 1),
])
def test_outcome(action, lane, status, subject, priority, days):
    c = {"action": action, "lane_after": lane}
    assert decision.outcome(c) == {"status": status, "activity": {"subject": subject, "priority": priority, "business_days": days}}


def test_note_explains_changes_and_never_judges():
    c = decision.validate("edit_approve", {"reason": "Rear damage fits the story.", "lane": "standard",
                                           "suggested_reserve": 2000, "by": "J. Smith"}, prop(), SLOTS)
    body = decision.note(c, prop())["body"]
    assert body == ("Decision by J. Smith: Edited and approved. Lane: Standard (changed from Fast track). "
                    "Suggested initial reserve $2,000 (changed from $1,200). Reason: Rear damage fits the story.")
    sb = decision.note(decision.validate("send_back", {"reason": "Glare on both.", "slots": ["odometer", "vin_photo"]},
                                         prop(), SLOTS), prop())["body"]
    assert "Photos requested: Odometer, Vin Photo." in sb
    for action, extra in [("refer_siu", {}), ("approve", {}), ("field_inspection", {})]:
        text = decision.note(decision.validate(action, {**extra, "reason": "Checked it all."}, prop(), SLOTS), prop())["body"].lower()
        assert not any(w in text for w in ("denied", "rejected", "fraud"))
