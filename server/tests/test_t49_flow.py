"""T49 through the API with the fake model (tasks/t49-design.md): the second read, its cap, the feedback report."""

from conftest import GOOD_READS, GOOD_STORY
from evidence_server import evaluation
from test_damage_flow import db_rows
from test_loss_notice import DAMAGE_ANSWERS, detail
from test_photo_check_flow import by_slot, shot, start
from test_review_flow import ready, review

ANSWERS = {**DAMAGE_ANSWERS, "damaged_areas": ["Windshield"]}
REAR_CLOSE = {**GOOD_READS["damage_closeup"][0], "parts": ["Rear bumper"]}
REAR_WIDE = {**GOOD_READS["damage_wide"][0], "parts": ["Rear bumper"]}
WINDSHIELD = {**GOOD_READS["damage_closeup"][0], "parts": ["Windshield"]}


def adjuster_check(client, claim, slot):
    """The latest check of the slot as the adjuster sees it (with the reason code)."""
    return [c for c in detail(client, claim)["photo_checks"] if c["slot_id"] == slot][-1]


def purposes(client, claim):
    rows = db_rows(client, "SELECT purpose FROM model_calls WHERE claim_id=%s", claim["id"])
    return [r["purpose"] for r in rows]


# --- the second read ---


def test_a_disagreeing_second_read_lets_the_claimant_continue(client, model):
    claim, _, phone = start(client, ANSWERS)
    model.set("damage_closeup", REAR_CLOSE, WINDSHIELD)
    phone.upload("damage_closeup", shot(1))
    check = adjuster_check(client, claim, "damage_closeup")
    assert (check["result"], check["reason_code"]) == ("unclear", "DAMAGE_AREA_UNCLEAR")
    calls = purposes(client, claim)  # the fake clock stands still, so compare as counts, not order
    assert (calls.count("photo.damage_closeup#1"), calls.count("photo.damage_closeup#recheck")) == (1, 1)
    seen = by_slot(client.get(f"/api/c/{claim['token']}").json()["photo_checks"])["damage_closeup"]
    assert seen["result"] == "unchecked" and seen["hint"] is None  # nothing asked of the claimant


def test_an_agreeing_second_read_keeps_the_retake(client, model):
    claim, _, phone = start(client, ANSWERS)
    model.set("damage_closeup", REAR_CLOSE, REAR_CLOSE)
    phone.upload("damage_closeup", shot(1))
    check = adjuster_check(client, claim, "damage_closeup")
    assert (check["result"], check["reason_code"]) == ("retake", "DAMAGE_AREA_MISMATCH")
    assert purposes(client, claim).count("photo.damage_closeup#recheck") == 1


def test_a_matching_photo_is_read_once(client, model):
    claim, _, phone = start(client, ANSWERS)
    model.set("damage_closeup", WINDSHIELD)
    phone.upload("damage_closeup", shot(1))
    assert adjuster_check(client, claim, "damage_closeup")["result"] == "pass"
    assert not any(p.endswith("#recheck") for p in purposes(client, claim))


def test_at_most_two_second_reads_per_claim(client, model, clock):
    claim, _, phone = start(client, ANSWERS)
    model.set("damage_closeup", REAR_CLOSE)  # every read, first and second, sees the rear bumper
    model.set("damage_wide", REAR_WIDE)
    phone.upload("damage_closeup", shot(1))
    clock.advance(seconds=1)
    phone.upload("damage_wide", shot(2))
    clock.advance(seconds=1)
    phone.upload("damage_closeup", shot(3))  # the retake: past the cap, decided on its first reading
    assert sum(p.endswith("#recheck") for p in purposes(client, claim)) == 2
    assert adjuster_check(client, claim, "damage_closeup")["reason_code"] == "DAMAGE_AREA_MISMATCH"


def test_a_failed_second_read_falls_back_to_the_first(client, model):
    claim, _, phone = start(client, ANSWERS)
    model.set("damage_closeup", REAR_CLOSE, RuntimeError("model down"))
    phone.upload("damage_closeup", shot(1))
    check = adjuster_check(client, claim, "damage_closeup")
    assert (check["result"], check["reason_code"]) == ("retake", "DAMAGE_AREA_MISMATCH")


# --- adjuster feedback on flags ---


def test_feedback_counts_the_latest_verdict_per_claim_and_flag(client, model, clock):
    model.set("story", {**GOOD_STORY, "consistent": "no"})
    first = ready(client, clock)
    key = detail(client, first)["proposal"]["flags"][0]["key"]
    review(client, first["id"], kind="flag", key=key, verdict="false_alarm", reason="The dent is on the rear, as told.")
    review(client, first["id"], kind="flag", key=key, verdict="confirmed")  # changed their mind: the latest counts
    second = ready(client, clock)
    key2 = detail(client, second)["proposal"]["flags"][0]["key"]
    review(client, second["id"], kind="flag", key=key2, verdict="false_alarm", reason="Seen on the photo, as told.")
    fb = client.get("/api/admin/feedback").json()
    assert fb["reviews"] == 2
    expected: dict[str, list[int]] = {}
    for k, verdict in ((key, "confirmed"), (key2, "false_alarm")):
        counts = expected.setdefault(k.rsplit(":", 1)[1], [0, 0])
        counts[0 if verdict == "confirmed" else 1] += 1
    for code, (confirmed, false_alarm) in expected.items():
        row = next(r for r in fb["rows"] if r["reason_code"] == code)
        total = confirmed + false_alarm
        assert (row["confirmed"], row["false_alarm"], row["total"]) == (confirmed, false_alarm, total)
        assert row["confirm_rate"] == round(confirmed / total, 3)
        assert row["ci"] == evaluation.wilson(confirmed, total)


def test_feedback_is_empty_before_any_review_and_needs_login(anon, client):
    assert client.get("/api/admin/feedback").json() == {"rows": [], "reviews": 0}
    client.post("/api/admin/logout")  # `client` is `anon` after login
    assert anon.get("/api/admin/feedback").status_code == 401
