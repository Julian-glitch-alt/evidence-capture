"""T20 damage reading after submit, through the API (contract: tasks/t20-design.md), with a fake model: no Meta calls."""

from conftest import GOOD_DAMAGE, JPEG, Phone
from evidence_server import damage, damage_prompts
from test_loss_notice import DAMAGE_ANSWERS, MEDIA, detail, open_claim, save

CLOSEUP, WIDE = JPEG + b"-closeup", JPEG + b"-wide"
VERSION = f"{damage.VERSION}+{damage_prompts.VERSION}"
OLD_HOOD = {**GOOD_DAMAGE["damages"][0], "area": "Hood", "damage_type": "SCRATCH_SCUFF", "severity": "minor", "age": "old"}


def captured_claim(client, clock, photos=None, policy="DEMO-AUTO-1001"):
    """A claim with every required shot taken; damage photos {slot: [bytes, ...]} in that order. Not sent yet."""
    photos = photos or {"damage_closeup": [CLOSEUP], "damage_wide": [WIDE]}
    claim = open_claim(client, policy)
    layout = save(client, claim["token"], DAMAGE_ANSWERS)
    phone = Phone(client, claim["token"])
    items = {}
    for section in layout["sections"]:
        for slot in section["slots"]:
            if slot["id"] in photos:
                for data in photos[slot["id"]]:
                    clock.advance(seconds=1)  # photos are ordered by when they arrived
                    items.setdefault(slot["id"], []).append(phone.upload(slot["id"], data).json())
            elif slot["required"]:
                data, mime = MEDIA.get(slot["kind"], (JPEG, "image/jpeg"))
                assert phone.upload(slot["id"], data, mime).status_code == 200
    return claim, items


def send(client, claim):
    r = client.post(f"/api/c/{claim['token']}/submit", json={})
    assert r.status_code == 200, r.text
    return detail(client, claim)


def damage_calls(model):
    return [c for c in model.calls if c["slot"] == "damage"]


def db_rows(client, sql, *args):
    with client.app.state.store.db.tx() as conn:
        return conn.execute(sql, args).fetchall()


# --- the reading ---


def test_sent_claim_gets_a_damage_reading(client, model, clock):
    claim, items = captured_claim(client, clock)
    v = send(client, claim)["damage"]
    assert v["status"] == "done"
    ids = [items["damage_closeup"][0]["id"], items["damage_wide"][0]["id"]]
    assert [(p["item_id"], p["slot_id"], p["status"]) for p in v["photos"]] == [
        (ids[0], "damage_closeup", "done"), (ids[1], "damage_wide", "done"),
    ]
    assert [(d["item_id"], d["area"], d["damage_type"], d["severity"], d["age"]) for d in v["damages"]] == [
        (ids[0], "Rear bumper", "BUMPER_DAMAGE", "moderate", "new"), (ids[1], "Rear bumper", "BUMPER_DAMAGE", "moderate", "new"),
    ]
    assert (v["areas"], v["old_damage"]) == (["Rear bumper"], False)


def test_one_call_per_photo_through_the_meta_client(client, model, clock):
    claim, items = captured_claim(client, clock)
    send(client, claim)
    assert sorted(c["image"] for c in damage_calls(model)) == sorted([CLOSEUP, WIDE])
    assert all(c["model"] == damage_prompts.MODEL for c in damage_calls(model))
    calls = db_rows(client, "SELECT purpose, claim_id, item_id, status FROM model_calls WHERE purpose LIKE 'damage.%%' ORDER BY purpose")
    assert [(c["purpose"], c["item_id"]) for c in calls] == [
        ("damage.damage_closeup", items["damage_closeup"][0]["id"]), ("damage.damage_wide", items["damage_wide"][0]["id"]),
    ]
    assert all(c["claim_id"] == claim["id"] and c["status"] == "ok" for c in calls)


def test_nothing_is_read_before_the_claim_is_sent(client, model, clock):
    claim, _ = captured_claim(client, clock)
    assert damage_calls(model) == []
    assert detail(client, claim)["damage"]["status"] == "none"


def test_several_close_ups_are_each_read(client, model, clock):
    second = JPEG + b"-closeup-2"
    claim, items = captured_claim(client, clock, {"damage_closeup": [CLOSEUP, second], "damage_wide": [WIDE]})
    v = send(client, claim)["damage"]
    assert [p["item_id"] for p in v["photos"]] == [items["damage_closeup"][0]["id"], items["damage_closeup"][1]["id"], items["damage_wide"][0]["id"]]
    assert len(damage_calls(model)) == 3


def test_old_damage_is_marked_and_left_out_of_the_areas(client, model, clock):
    model.set_image(CLOSEUP, {**GOOD_DAMAGE, "damages": [GOOD_DAMAGE["damages"][0], OLD_HOOD]})
    claim, _ = captured_claim(client, clock)
    v = send(client, claim)["damage"]
    assert [(d["area"], d["age"]) for d in v["damages"]] == [("Rear bumper", "new"), ("Hood", "old"), ("Rear bumper", "new")]
    assert (v["areas"], v["old_damage"]) == (["Rear bumper"], True)


def test_each_run_is_logged_as_one_event(client, model, clock):
    claim, _ = captured_claim(client, clock)
    events = [e for e in send(client, claim)["events"] if e["type"] == "damage.read"]
    assert [(e["actor"], e["data"]) for e in events] == [
        ("system", {"version": VERSION, "photos": 2, "done": 2, "failed": 0, "damages": 2}),
    ]


def test_rows_carry_the_version(client, model, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    rows = db_rows(client, "SELECT kind, status, version FROM analyses WHERE claim_id=%s AND kind='damage'", claim["id"])
    assert [(r["kind"], r["status"], r["version"]) for r in rows] == [("damage", "done", VERSION)] * 2


# --- the model never blocks (D3, D4) ---


def test_model_down_shows_not_analysed_and_the_claim_is_sent(client, model, clock):
    model.set("damage", RuntimeError("Meta is down"))
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    assert d["status"] != "capturing" and d["seal"] is not None
    v = d["damage"]
    assert v["status"] == "failed"
    assert all(p["status"] == "failed" and p["error"].startswith("RuntimeError") for p in v["photos"])
    assert v["damages"] == []
    [event] = [e for e in d["events"] if e["type"] == "damage.read"]
    assert (event["data"]["done"], event["data"]["failed"]) == (0, 2)


def test_one_unreadable_photo_leaves_the_others(client, model, clock):
    model.set_image(WIDE, "this is not JSON")
    claim, _ = captured_claim(client, clock)
    v = send(client, claim)["damage"]
    assert v["status"] == "partial"
    assert [(p["slot_id"], p["status"], p["error"]) for p in v["photos"]] == [
        ("damage_closeup", "done", None), ("damage_wide", "failed", "bad_json"),
    ]
    assert [d["slot_id"] for d in v["damages"]] == ["damage_closeup"]


def test_an_answer_of_the_wrong_shape_is_bad_shape(client, model, clock):
    model.set_image(WIDE, {"damages": []})
    claim, _ = captured_claim(client, clock)
    v = send(client, claim)["damage"]
    assert [(p["status"], p["error"]) for p in v["photos"]] == [("done", None), ("failed", "bad_shape")]


def test_reading_not_finished_shows_pending_then_stale(client, model, clock):
    claim, _ = captured_claim(client, clock)
    client.app.state.store.submit(claim["token"])  # submit without the background run
    v = detail(client, claim)["damage"]
    assert v["status"] == "pending" and all(p["status"] == "pending" for p in v["photos"])
    clock.advance(seconds=121)
    v = detail(client, claim)["damage"]
    assert v["status"] == "failed" and all(p["error"] == "stale" for p in v["photos"])


def test_running_again_calls_nothing_new(client, model, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    before = len(model.calls)
    client.app.state.store.run_damage(claim["id"])
    assert len(model.calls) == before
    assert len([e for e in detail(client, claim)["events"] if e["type"] == "damage.read"]) == 1


def test_run_damage_never_raises(client):
    assert client.app.state.store.run_damage("CLM-NOPE") is None


# --- only current photos, only the adjuster (D5, D8) ---


def test_a_retaken_wide_shot_is_not_read(client, model, clock):
    newer = JPEG + b"-wide-2"
    claim, items = captured_claim(client, clock, {"damage_closeup": [CLOSEUP], "damage_wide": [WIDE, newer]})
    v = send(client, claim)["damage"]
    assert WIDE not in [c["image"] for c in damage_calls(model)]
    assert [p["item_id"] for p in v["photos"]] == [items["damage_closeup"][0]["id"], items["damage_wide"][1]["id"]]


def test_the_claimant_never_sees_the_reading(client, model, clock):
    model.set_image(CLOSEUP, {**GOOD_DAMAGE, "damages": [OLD_HOOD]})
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    r = client.get(f"/api/c/{claim['token']}")
    assert r.status_code == 200
    assert "damage" not in r.json()
    for word in ("BUMPER_DAMAGE", "SCRATCH_SCUFF", "old_damage", '"age"'):
        assert word not in r.text
