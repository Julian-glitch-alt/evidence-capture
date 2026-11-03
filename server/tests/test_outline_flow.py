"""Track 6 through the API: after submit, Muse Spark's damage box -> crop -> SAM 3.1 -> outline stored with
the damage. Fake model replaying the S0 SAM answers; no Meta calls."""

import io
import json
from pathlib import Path

import pytest
from PIL import Image

from conftest import GOOD_DAMAGE, JPEG
from evidence_server import outline
from test_damage_flow import captured_claim, db_rows, send

FIXTURES = Path(__file__).parent / "fixtures" / "sam"
ANSWERS = json.loads((FIXTURES / "responses.json").read_text())
PHOTO = (FIXTURES / "crops" / "damage_red_front_0.jpg").read_bytes()  # 378 x 205, a rust hole
HOLE = next(a["output_text"] for a in ANSWERS
            if a["image"] == "crops/damage_red_front_0.jpg" and a["concept"] == "hole")
HOLE_DAMAGE = {**GOOD_DAMAGE["damages"][0], "box": [0, 0, 1000, 1000], "observed": "Rust hole in the bumper"}


@pytest.fixture(autouse=True)
def _sam_on(client):
    """SAM is off by default since T42 (Julian, 2 Nov); these tests switch it on."""
    client.app.state.store.sam_max_calls = 6


def reading(*damages):
    return {"is_vehicle": True, "damages": list(damages), "unsure": False}


def claim_with_photo(client, clock, model, *damages, photo=PHOTO):
    model.set("damage", reading(*(damages or (HOLE_DAMAGE,))))
    claim, items = captured_claim(client, clock, {"damage_closeup": [photo], "damage_wide": [JPEG + b"-wide"]})
    return claim, items


def without_ids(value):
    """A proposal with photo ids and damage keys dropped, to compare two claims' outcomes."""
    if isinstance(value, dict):
        return {k: without_ids(v) for k, v in value.items() if k not in ("item_id", "keys", "key", "evidence")}
    if isinstance(value, list):
        return [without_ids(v) for v in value]
    return value


def sam_calls(model):
    return [c for c in model.calls if c["slot"] == "sam"]


def test_sam_outlines_the_damage_muse_boxed(client, model, clock):
    model.set_sam("hole", HOLE)
    claim, items = claim_with_photo(client, clock, model)
    d = send(client, claim)
    close, wide = d["damage"]["damages"]
    o = close["outline"]
    assert (o["status"], o["sam_agrees"], o["concept"], o["found"]) == ("done", "yes", "hole", 1)
    [region] = o["regions"]
    # SAM's box (191,32)-(262,112) in the 378 x 205 crop, which is the whole photo here.
    assert region["box"] == pytest.approx([191 / 378 * 1000, 32 / 205 * 1000, 263 / 378 * 1000, 113 / 205 * 1000],
                                          abs=0.2)
    assert region["mask"] == "decoded" and isinstance(region["overlay"], str)
    assert o["area"]["basis"] == "mask" and 4.5 < o["area"]["photo_pct"] < 5.6  # 68% of a 72 x 81 box
    assert o["area"]["panel_pct"] is None  # "bumper" found nothing: missing, not 0
    # The wide photo is not an image: Muse's box stays, no outline, no SAM call for it.
    assert (wide["outline"]["status"], wide["outline"]["sam_agrees"], wide["outline"]["error"]) == (
        "not_run", "not_run", "image_unreadable")
    # One SAM call on the crop with the concept code picked, then the panel on the whole photo.
    calls = sam_calls(model)
    assert [c["prompt"] for c in calls] == ["hole", "bumper"]
    assert Image.open(io.BytesIO(calls[0]["image"])).size == (378, 205)
    rows = db_rows(client, "SELECT purpose, model, item_id, status, cost_usd FROM model_calls"
                           " WHERE model='sam-3.1' ORDER BY purpose")
    assert [(r["purpose"], r["item_id"], r["status"], r["cost_usd"]) for r in rows] == [
        ("outline.damage", items["damage_closeup"][0]["id"], "ok", None),
        ("outline.panel", items["damage_closeup"][0]["id"], "ok", None)]
    assert d["ai_usage"]["cost_unknown_calls"] == 2


def test_share_of_panel_when_the_panel_is_segmented(client, model, clock):
    model.set_sam("hole", HOLE)
    # The panel answer: one instance covering the whole photo, mask not decodable -> box estimate.
    model.set_sam("bumper", "<0f>0<|box;x1=0;y1=0;x2=377;y2=204;w=378;h=205|><|mask;x=0;y=0;data=2,2,~zzzz|>")
    claim, _ = claim_with_photo(client, clock, model)
    o = send(client, claim)["damage"]["damages"][0]["outline"]
    assert o["area"]["panel"] == "bumper"
    assert o["area"]["panel_pct"] == pytest.approx(o["area"]["photo_pct"], abs=0.5)
    assert o["area"]["basis"] == "box estimate"


def test_sam_finding_nothing_in_the_box_adds_a_note_and_changes_nothing_else(client, model, clock):
    model.set_sam("hole", HOLE)
    agreed, _ = claim_with_photo(client, clock, model)
    agreed_detail = send(client, agreed)
    # Muse's box is the middle of the photo; SAM's only region is in the crop's top-left margin.
    model.set_sam("damage", "<0f>0<|box;x1=0;y1=0;x2=20;y2=10;w=284;h=154|><|mask;x=0;y=0;data=11,21,~zzzz|>")
    middle = {**GOOD_DAMAGE["damages"][0], "box": [250, 250, 750, 750]}
    other, _ = claim_with_photo(client, clock, model, middle, photo=PHOTO + b"\0")  # other bytes: no cache hit
    d = send(client, other)
    o = d["damage"]["damages"][0]["outline"]
    assert (o["status"], o["sam_agrees"], o["regions"]) == ("done", "no", [])
    assert len(d["damage"]["damages"]) == 2  # never removed
    assert without_ids(d["proposal"]) == without_ids(agreed_detail["proposal"])  # never a signal, never money


def test_sam_failure_keeps_muses_box_and_the_reading(client, model, clock):
    model.set_sam("hole", RuntimeError("SAM is down"))
    claim, _ = claim_with_photo(client, clock, model)
    d = send(client, claim)
    assert d["damage"]["status"] == "done"
    o = d["damage"]["damages"][0]["outline"]
    assert (o["status"], o["sam_agrees"], o["regions"]) == ("failed", "not_run", [])
    assert "SAM is down" in o["error"]
    assert o["area"]["basis"] == "box estimate"
    assert d["proposal"]["status"] == "ready"


def test_the_step_can_be_switched_off(client, model, clock):
    client.app.state.store.sam_max_calls = 0
    model.set_sam("hole", HOLE)
    claim, _ = claim_with_photo(client, clock, model)
    o = send(client, claim)["damage"]["damages"][0]["outline"]
    assert (o["status"], o["error"]) == ("not_run", "disabled")
    assert sam_calls(model) == []


def test_calls_per_claim_are_capped(client, model, clock):
    client.app.state.store.sam_max_calls = 1
    model.set_sam("hole", HOLE)
    second = {**HOLE_DAMAGE, "area": "Hood", "damage_type": "SCRATCH_SCUFF", "box": [0, 0, 400, 400],
              "observed": "Scratch"}
    claim, _ = claim_with_photo(client, clock, model, HOLE_DAMAGE, second)
    damages = send(client, claim)["damage"]["damages"]
    close = [d for d in damages if d["slot_id"] == "damage_closeup"]
    assert [(d["outline"]["status"], d["outline"]["error"]) for d in close] == [("done", None), ("not_run", "capped")]
    assert [c["prompt"] for c in sam_calls(model)] == ["hole"]  # no call left for the panel


def test_report_is_built_with_the_outlines(client, model, clock):
    model.set_sam("hole", HOLE)
    claim, _ = claim_with_photo(client, clock, model)
    d = send(client, claim)
    assert d["report"] is not None
    assert outline.CAPTION == "outline: SAM 3.1 · box: Muse Spark"
