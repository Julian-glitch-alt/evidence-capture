"""T21 statement vs photos after submit, through the API (contract: tasks/t21-design.md), with a fake model."""

from conftest import GOOD_DAMAGE, GOOD_STORY
from evidence_server import consistency, consistency_prompts
from test_damage_flow import CLOSEUP, WIDE, captured_claim, db_rows, send
from test_loss_notice import detail

VERSION = f"{consistency.VERSION}+{consistency_prompts.VERSION}"
OLD_HOOD = {**GOOD_DAMAGE["damages"][0], "area": "Hood", "damage_type": "SCRATCH_SCUFF", "severity": "minor", "age": "old"}


def story_calls(model):
    return [c for c in model.calls if c["slot"] == "story"]


def rows_of(d):
    return {c["check_id"]: c for c in d["checks"] if c["check_id"].startswith("consistency.")}


def test_sent_claim_gets_three_consistency_checks(client, model, clock):
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    rows = rows_of(d)
    assert list(rows) == list(consistency.CHECK_ORDER)
    assert {k: r["result"] for k, r in rows.items()} == {
        "consistency.areas": "pass", "consistency.prior_damage": "pass", "consistency.story": "pass",
    }
    assert all(r["source"] == consistency.VERSION for r in rows.values())
    assert d["consistency"] == {"status": "done"}
    [event] = [e for e in d["events"] if e["type"] == "consistency.checked"]
    assert (event["actor"], event["data"]) == ("system", {
        "version": VERSION, "results": {"consistency.areas": "pass", "consistency.prior_damage": "pass", "consistency.story": "pass"},
    })
    types = [e["type"] for e in d["events"]]
    assert types.index("damage.read") < types.index("consistency.checked")


def test_one_text_only_story_call_with_the_statement_fenced(client, model, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    [call] = story_calls(model)
    assert (call["image"], call["mime"], call["model"]) == (None, None, consistency_prompts.MODEL)
    assert call["prompt"].startswith(consistency_prompts.STORY)
    assert "<claim>\nCause: Hit an object\n" in call["prompt"] and call["prompt"].endswith("</claim>")
    assert "Statement: Backed into a bollard in a parking lot." in call["prompt"]
    assert "1. Rear bumper, bumper damage, moderate, new: Rear bumper cover pushed in (damage_closeup, damage_wide photos)" in call["prompt"]
    assert "\n2. " not in call["prompt"]  # the fixture shows one dent in both photos: listed once
    [logged] = db_rows(client, "SELECT purpose, claim_id, item_id FROM model_calls WHERE purpose='consistency.story'")
    assert (logged["claim_id"], logged["item_id"]) == (claim["id"], None)


def test_findings_reach_the_adjuster_with_evidence(client, model, clock):
    model.set_image(WIDE, {**GOOD_DAMAGE, "damages": [GOOD_DAMAGE["damages"][0], OLD_HOOD, {**GOOD_DAMAGE["damages"][0], "area": "Front bumper"}]})
    model.set("story", {**GOOD_STORY, "consistent": "no", "impact_from_damage": "multiple",
                        "reasons": [{"text": "Front damage does not fit backing up.", "damages": [3]}]})  # 1 rear bumper (both photos), 2 old hood, 3 front bumper
    claim, items = captured_claim(client, clock)
    rows = rows_of(send(client, claim))
    wide = items["damage_wide"][0]["id"]
    assert (rows["consistency.areas"]["result"], rows["consistency.areas"]["reason_code"]) == ("unclear", "DAMAGE_OUTSIDE_STATEMENT")
    assert rows["consistency.areas"]["value"]["evidence"] == [{"item_id": wide, "slot_id": "damage_wide", "number": 3}]
    assert rows["consistency.prior_damage"]["reason_code"] == "PRIOR_DAMAGE_SUSPECTED"
    assert rows["consistency.prior_damage"]["value"]["old"] == [{"item_id": wide, "slot_id": "damage_wide", "number": 2}]
    story = rows["consistency.story"]
    assert (story["result"], story["reason_code"]) == ("fail", "STORY_INCONSISTENT")
    assert story["value"]["reasons"][0]["evidence"] == [{"item_id": wide, "slot_id": "damage_wide", "number": 3}]


def test_model_down_leaves_the_code_checks(client, model, clock):
    model.set("story", RuntimeError("Meta is down"))
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    rows = rows_of(d)
    assert (rows["consistency.story"]["result"], rows["consistency.story"]["reason_code"]) == ("unclear", "STORY_NOT_CHECKED")
    assert rows["consistency.areas"]["result"] == "pass"
    assert d["consistency"] == {"status": "done"}
    [row] = db_rows(client, "SELECT status, error FROM analyses WHERE claim_id=%s AND kind='consistency'", claim["id"])
    assert row["status"] == "done" and row["error"].startswith("RuntimeError")


def test_story_answer_of_the_wrong_shape(client, model, clock):
    model.set("story", {"consistent": "probably"})
    claim, _ = captured_claim(client, clock)
    assert rows_of(send(client, claim))["consistency.story"]["reason_code"] == "STORY_NOT_CHECKED"
    [row] = db_rows(client, "SELECT error FROM analyses WHERE claim_id=%s AND kind='consistency'", claim["id"])
    assert row["error"] == "bad_shape"


def test_no_damage_reading_means_no_story_call(client, model, clock):
    model.set("damage", RuntimeError("Meta is down"))
    claim, _ = captured_claim(client, clock)
    rows = rows_of(send(client, claim))
    assert {r["reason_code"] for r in rows.values()} == {"DAMAGE_NOT_READ"}
    assert story_calls(model) == []


def test_runs_once(client, model, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    client.app.state.store.run_consistency(claim["id"])
    assert len(story_calls(model)) == 1
    assert len([e for e in detail(client, claim)["events"] if e["type"] == "consistency.checked"]) == 1


def test_run_consistency_never_raises(client):
    assert client.app.state.store.run_consistency("CLM-NOPE") is None


def test_pending_then_stale_without_the_background_run(client, model, clock):
    claim, _ = captured_claim(client, clock)
    client.app.state.store.submit(claim["token"])
    assert detail(client, claim)["consistency"] == {"status": "pending"}
    clock.advance(seconds=121)
    assert detail(client, claim)["consistency"] == {"status": "failed"}


def test_no_damage_photos_means_no_consistency(client, model, clock):
    claim, _ = captured_claim(client, clock)
    assert detail(client, claim)["consistency"] == {"status": "none"}


def test_the_claimant_never_sees_the_checks(client, model, clock):
    model.set_image(CLOSEUP, {**GOOD_DAMAGE, "damages": [OLD_HOOD]})
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    r = client.get(f"/api/c/{claim['token']}")
    assert "consistency" not in r.json() and "checks" not in r.json()
    for word in ("PRIOR_DAMAGE_SUSPECTED", "DAMAGE_OUTSIDE_STATEMENT", "impact_from"):
        assert word not in r.text
