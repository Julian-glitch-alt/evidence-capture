"""T34 photo check through the API (contract: tasks/t34-design.md), with a fake model: no Meta calls."""

import json
from pathlib import Path

from conftest import JPEG, GOOD_READS, Phone
from evidence_server import coverage, loss_notice, photo_check, photo_prompts
from test_loss_notice import DAMAGE_ANSWERS, MEDIA, detail, open_claim, save

TESLA_VIN = "5YJ3E1EA8MA159083"  # DEMO-AUTO-1002's car, not on DEMO-AUTO-1001
WRONG_VIN = [
    {"is_vin": True, "vin": TESLA_VIN, "unsure": False, "problem": "none"},
    {"is_vin": True, "characters": list(TESLA_VIN), "unsure": False, "problem": "none"},
]
WRONG_PLATE = {**GOOD_READS["plate"][0], "text": "8ABC123"}
VERSION = f"{photo_check.VERSION}+{photo_prompts.VERSION}"
AFTER_RESPONSE = {"damage.read", "consistency.checked", "report.ready"}  # T20 and T21 log after the submit response


def shot(n: int) -> bytes:
    """A different photo each time (the same bytes would come back from the cache)."""
    return JPEG + f"-shot-{n}".encode()


def start(client, answers=DAMAGE_ANSWERS):
    claim = open_claim(client)
    layout = save(client, claim["token"], answers)
    return claim, layout, Phone(client, claim["token"])


def checks_seen(client, claim):
    return client.get(f"/api/c/{claim['token']}").json()["photo_checks"]


def by_slot(entries):
    return {e["slot_id"]: e for e in entries}


def capture_rest(phone, layout, skip=()):
    for section in layout["sections"]:
        for slot in section["slots"]:
            if slot["required"] and slot["id"] not in skip:
                data, mime = MEDIA.get(slot["kind"], (JPEG, "image/jpeg"))
                assert phone.upload(slot["id"], data, mime).status_code == 200


def submit(client, claim):
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 200, r.text
    return detail(client, claim)


def photo_rows(d):
    return {c["check_id"]: c for c in d["checks"] if c["check_id"].startswith("photo.")}


def db_rows(client, sql, *args):
    with client.app.state.store.db.tx() as conn:
        return conn.execute(sql, args).fetchall()


# --- checking each shot as it uploads ---


def test_upload_is_checked_in_the_background(client, model):
    claim, _, phone = start(client)
    item = phone.upload("plate").json()
    assert checks_seen(client, claim) == [
        {"item_id": item["id"], "slot_id": "plate", "result": "pass", "hint": None, "shots_left": 2}
    ]
    [call] = model.calls
    assert (call["slot"], call["model"], call["mime"], call["timeout"]) == ("plate", photo_prompts.MODEL, "image/jpeg", 30.0)
    assert call["image"] == JPEG


def test_vin_is_read_twice_with_two_prompts(client, model):
    claim, _, phone = start(client)
    phone.upload("vin_photo")
    assert sorted((c["slot"], c["read"]) for c in model.calls) == [("vin_photo", 0), ("vin_photo", 1)]
    assert checks_seen(client, claim)[0]["result"] == "pass"


def test_wrong_vin_is_caught_and_retaken_on_the_spot(client, model, clock):
    # The demo storyboard's 0:35 scene.
    claim, _, phone = start(client)
    model.set("vin_photo", *WRONG_VIN)
    phone.upload("vin_photo", shot(1))
    first = checks_seen(client, claim)[0]
    assert (first["result"], first["shots_left"]) == ("retake", 2)
    assert first["hint"] == "Retake this photo."  # identity mismatches give a plain hint (judge review 1 Nov, #4)

    model.set("vin_photo", *GOOD_READS["vin_photo"])
    clock.advance(seconds=1)
    item = phone.upload("vin_photo", shot(2)).json()
    [entry] = checks_seen(client, claim)
    assert (entry["item_id"], entry["result"], entry["shots_left"]) == (item["id"], "pass", 1)


def test_third_failed_shot_is_flagged_and_the_claim_goes_on(client, model, clock):
    claim, layout, phone = start(client)
    model.set("plate", WRONG_PLATE)
    seen = []
    for n in (1, 2, 3):
        clock.advance(seconds=1)  # shots are ordered by when they arrived
        phone.upload("plate", shot(n))
        entry = checks_seen(client, claim)[0]
        seen.append((entry["result"], entry["shots_left"], entry["hint"] is None))
    assert seen == [("retake", 2, False), ("retake", 1, False), ("flagged", 0, True)]

    capture_rest(phone, layout, skip={"plate"})
    row = photo_rows(submit(client, claim))["photo.plate"]
    assert (row["result"], row["reason_code"]) == ("fail", "PLATE_MISMATCH")


def test_retakes_in_a_multi_photo_slot_count_removed_shots(client, model, clock):
    claim, _, phone = start(client)
    phone.upload("damage_closeup", shot(1))
    clock.advance(seconds=1)
    model.set("damage_closeup", {**GOOD_READS["damage_closeup"][0], "damage_visible": False, "parts": []})
    bad = phone.upload("damage_closeup", shot(2)).json()
    entries = checks_seen(client, claim)
    assert [e["result"] for e in entries] == ["pass", "retake"]

    r = client.delete(f"/api/c/{claim['token']}/items/{bad['id']}", params={"session_id": phone.session["session_id"]})
    assert r.status_code == 200
    model.set("damage_closeup", *GOOD_READS["damage_closeup"])
    clock.advance(seconds=1)
    phone.upload("damage_closeup", shot(3))
    entries = checks_seen(client, claim)
    assert [(e["result"], e["shots_left"]) for e in entries] == [("pass", 2), ("pass", 1)]


def test_same_shot_uploaded_twice_is_checked_once(client, model):
    claim, _, phone = start(client)
    nonce = phone.nonce("plate")
    first = phone.upload("plate", nonce=nonce).json()
    again = phone.upload("plate", nonce=nonce).json()
    assert first["id"] == again["id"]
    assert len(model.calls) == 1
    assert len(checks_seen(client, claim)) == 1


def test_same_photo_bytes_hit_the_cache_across_claims(client, model):
    start(client)[2].upload("plate")
    start(client)[2].upload("plate")
    assert len(model.calls) == 1


def test_video_signature_and_documents_are_not_checked(client, model):
    claim, _, phone = start(client)
    assert phone.upload("walkaround", *MEDIA["video"]).status_code == 200
    assert phone.upload("documents").status_code == 200
    assert checks_seen(client, claim) == []
    assert model.calls == []
    assert db_rows(client, "SELECT * FROM photo_checks") == []


def test_walkaround_asks_for_a_slow_full_circle():
    slot = loss_notice.all_slots()["walkaround"]
    assert slot["max_seconds"] == 60
    assert slot["instructions"].startswith("Start at the front. Walk slowly all the way around the vehicle")


# --- never blocking the customer ---


def test_licence_is_not_sent_to_the_model_unless_switched_on(client, model):
    claim, _, phone = start(client)
    item = phone.upload("driver_license", shot(1)).json()
    assert model.calls == []
    assert checks_seen(client, claim)[0]["result"] == "unchecked"
    [row] = db_rows(client, "SELECT status, error FROM photo_checks WHERE item_id=%s", item["id"])
    assert (row["status"], row["error"]) == ("failed", "license_check_off")

    client.app.state.store.check_license = True
    phone.upload("driver_license", shot(2))
    assert [c["slot"] for c in model.calls] == ["driver_license"]
    assert checks_seen(client, claim)[0]["result"] == "pass"


def test_model_down_is_unchecked_and_submit_still_works(client, model):
    claim, layout, phone = start(client)
    model.set("plate", TimeoutError("slow"))
    phone.upload("plate")
    entry = checks_seen(client, claim)[0]
    assert (entry["result"], entry["hint"]) == ("unchecked", None)
    assert [r["status"] for r in db_rows(client, "SELECT status FROM model_calls")] == ["timeout"]

    capture_rest(phone, layout, skip={"plate"})
    row = photo_rows(submit(client, claim))["photo.plate"]
    assert (row["result"], row["reason_code"]) == ("unclear", "PHOTO_UNCHECKED")


def test_check_pending_too_long_shows_unchecked(client, clock):
    claim, _, phone = start(client)
    item = phone.upload("plate").json()
    with client.app.state.store.db.tx() as conn:
        conn.execute(
            "UPDATE photo_checks SET status='pending', readings_json=NULL, finished_at=NULL, started_at=%s WHERE item_id=%s",
            (clock.now().isoformat(), item["id"]),
        )
    assert checks_seen(client, claim)[0]["result"] == "pending"
    clock.advance(seconds=46)
    assert checks_seen(client, claim)[0]["result"] == "unchecked"


def test_fixing_the_form_turns_a_retake_into_a_pass_without_a_new_call(client, model):
    claim, _, phone = start(client)
    model.set("damage_wide", {**GOOD_READS["damage_wide"][0], "parts": ["Front left"]})
    phone.upload("damage_wide")
    assert checks_seen(client, claim)[0]["result"] == "retake"
    calls = len(model.calls)  # the read, and the second read the mismatch asked for (T49)
    save(client, claim["token"], {**DAMAGE_ANSWERS, "damaged_areas": ["Front left"]})
    assert checks_seen(client, claim)[0]["result"] == "pass"
    assert len(model.calls) == calls == 2


def test_customer_sees_no_readings_reasons_or_policy_values(client, model):
    claim, _, phone = start(client)
    model.set("plate", WRONG_PLATE)
    phone.upload("plate")
    phone.upload("vin_photo")
    for entry in checks_seen(client, claim):
        assert set(entry) == {"item_id", "slot_id", "result", "hint", "shots_left"}
    assert "8ABC123" not in client.get(f"/api/c/{claim['token']}").text


# --- what the adjuster and later steps get ---


def test_each_check_is_logged_as_an_event_and_a_model_call(client, model):
    claim, _, phone = start(client)
    item = phone.upload("vin_photo").json()
    d = detail(client, claim)
    [event] = [e for e in d["events"] if e["type"] == "photo.checked"]
    assert event["data"] == {"item_id": item["id"], "slot": "vin_photo", "status": "done", "version": VERSION}
    calls = db_rows(client, "SELECT purpose, claim_id, item_id, status, cost_usd FROM model_calls ORDER BY purpose")
    assert [c["purpose"] for c in calls] == ["photo.vin_photo#1", "photo.vin_photo#2"]
    assert all(c["claim_id"] == claim["id"] and c["item_id"] == item["id"] and c["status"] == "ok" for c in calls)
    assert all(c["cost_usd"] > 0 for c in calls)
    [entry] = d["photo_checks"]
    assert entry["reason_code"] is None and entry["value"] == "1HGCV1F39LA526018" and entry["attempt"] == 1


def test_no_photo_rows_before_submit_and_one_per_slot_after(client, model):
    claim, layout, phone = start(client)
    capture_rest(phone, layout)
    assert detail(client, claim)["checks"] == []

    d = submit(client, claim)
    assert [c["check_id"] for c in d["checks"] if not c["check_id"].startswith("consistency.")] == (
        list(coverage.CHECK_ORDER) + [f"photo.{s}" for s in photo_check.CHECKED_SLOTS]
    )
    rows = photo_rows(d)
    assert {k: r["result"] for k, r in rows.items()} == {
        "photo.plate": "pass",
        "photo.vin_photo": "pass",
        "photo.odometer": "pass",
        "photo.driver_license": "unclear",  # licence check is off by default
        "photo.damage_closeup": "pass",
        "photo.damage_wide": "pass",
    }
    assert all(r["source"] == photo_check.VERSION and r["created_at"] == d["submitted_at"] for r in rows.values())
    assert rows["photo.odometer"]["value"] == 56947
    assert rows["photo.plate"]["inputs"]["item_id"] == by_slot(d["photo_checks"])["plate"]["item_id"]
    assert d["reason_codes"] == []  # T34 adds no submit-time codes (P12)
    types = [e["type"] for e in d["events"] if e["type"] not in AFTER_RESPONSE]
    assert types[-2:] == ["claim.sealed", "coverage.checked"]


def test_check_finishing_after_submit_updates_its_row(client, model):
    claim, layout, phone = start(client)
    model.set("plate", TimeoutError("slow"))
    item = phone.upload("plate").json()
    capture_rest(phone, layout, skip={"plate"})
    assert photo_rows(submit(client, claim))["photo.plate"]["result"] == "unclear"

    # The background run finishes late: the row goes back to pending and the model now answers.
    with client.app.state.store.db.tx() as conn:
        conn.execute("UPDATE photo_checks SET status='pending', error=NULL, finished_at=NULL WHERE item_id=%s", (item["id"],))
    model.set("plate", *GOOD_READS["plate"])
    client.app.state.store.run_photo_check(item["id"])

    d = detail(client, claim)
    assert photo_rows(d)["photo.plate"]["result"] == "pass"
    types = [e["type"] for e in d["events"]]
    assert types[-1] == "photo.checked" and types.index("claim.sealed") < len(types) - 1
    assert client.app.state.store.verify_chain(claim["id"])["ok"] is True


def test_run_photo_check_never_raises(client, model):
    claim, _, phone = start(client)
    item = phone.upload("plate").json()
    with client.app.state.store.db.tx() as conn:
        conn.execute("UPDATE photo_checks SET status='pending' WHERE item_id=%s", (item["id"],))
        conn.execute("UPDATE items SET path='claims/missing.jpg' WHERE id=%s", (item["id"],))
    client.app.state.store.run_photo_check(item["id"])  # the blob is gone: must not raise
    client.app.state.store.run_photo_check("no-such-item")
    [row] = db_rows(client, "SELECT status FROM photo_checks WHERE item_id=%s", item["id"])
    assert row["status"] == "failed"
    assert checks_seen(client, claim)[0]["result"] == "unchecked"


def test_old_shots_keep_their_check_history(client, model, clock):
    claim, _, phone = start(client)
    model.set("plate", WRONG_PLATE)
    phone.upload("plate", shot(1))
    clock.advance(seconds=1)
    model.set("plate", *GOOD_READS["plate"])
    phone.upload("plate", shot(2))
    rows = db_rows(client, "SELECT status, readings_json FROM photo_checks ORDER BY started_at, item_id")
    assert len(rows) == 2 and all(r["status"] == "done" and r["readings_json"] for r in rows)
    assert len(checks_seen(client, claim)) == 1


# --- form edits made after a hint (T34b) ---


def hint_events(client, claim):
    return [e for e in detail(client, claim)["events"] if e["type"] == "answers.changed_after_photo_hint"]


def test_changing_the_form_after_the_area_hint_leaves_a_trail(client, model):
    claim, _, phone = start(client)
    model.set("damage_wide", {**GOOD_READS["damage_wide"][0], "parts": ["Front left"]})
    item = phone.upload("damage_wide").json()
    save(client, claim["token"], {**DAMAGE_ANSWERS, "damaged_areas": ["Front left"]})
    [event] = hint_events(client, claim)
    assert event["actor"] == "customer"
    assert event["data"] == {
        "item_id": item["id"], "slot": "damage_wide", "reason_code": "DAMAGE_AREA_MISMATCH",
        "field": "damaged_areas", "before": ["Rear bumper"], "after": ["Front left"],
    }
    save(client, claim["token"], {**DAMAGE_ANSWERS, "damaged_areas": ["Front left"], "description": "more"})
    assert len(hint_events(client, claim)) == 1  # the hint is gone, so later edits leave no trail


def test_ordinary_form_edits_leave_no_trail(client, model):
    claim, _, phone = start(client)
    phone.upload("damage_wide")
    save(client, claim["token"], {**DAMAGE_ANSWERS, "damaged_areas": ["Rear bumper", "Trunk or tailgate"]})
    assert hint_events(client, claim) == []


def test_an_edit_sent_with_submit_leaves_a_trail(client, model):
    claim, layout, phone = start(client)
    model.set("damage_wide", {**GOOD_READS["damage_wide"][0], "parts": ["Front left"]})
    capture_rest(phone, layout)
    r = client.post(f"/api/c/{claim['token']}/submit", json={"answers": {**DAMAGE_ANSWERS, "damaged_areas": ["Front left"]}})
    assert r.status_code == 200, r.text
    assert [e["data"]["field"] for e in hint_events(client, claim)] == ["damaged_areas"]


def test_phone_refuses_only_a_nearly_black_frame():
    template = json.loads((Path(__file__).parents[1] / "src/evidence_server/templates/loss_notice/base.json").read_text())
    assert template["quality"] == {"min_sharpness": 60, "min_brightness": 20, "max_brightness": 235}
