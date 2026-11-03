"""Track 6: what code derives from Muse Spark's box and SAM 3.1's regions (outline.py). Pure, no database."""

import base64
import io
import json
from pathlib import Path

import pytest
from PIL import Image

from evidence_server import damage, outline, segment

FIXTURES = Path(__file__).parent / "fixtures" / "sam"
ANSWERS = json.loads((FIXTURES / "responses.json").read_text())


def fixture(image: str, concept: str) -> str:
    return next(a["output_text"] for a in ANSWERS if a["image"] == image and a["concept"] == concept)


def dmg(**kw) -> dict:
    return {"area": "Rear bumper", "damage_type": "BUMPER_DAMAGE", "severity": "moderate", "age": "new",
            "box": [200, 450, 800, 700], "observed": "", **kw}


# --- concept and panel tables ---


@pytest.mark.parametrize("d, expected", [
    (dmg(), "damage"),
    (dmg(damage_type="GLASS_DAMAGE"), "crack"),
    (dmg(damage_type="LIGHTING_DAMAGE"), "crack"),
    (dmg(observed="Rust hole through the fender"), "hole"),
    (dmg(observed="Bumper cover punctured near the plate"), "hole"),
    (dmg(observed="Cracked bumper cover"), "crack"),
    (dmg(damage_type="GLASS_DAMAGE", observed="hole in the window"), "hole"),
    (dmg(observed="Shoulder dented"), "damage"),  # whole words only
])
def test_concept_is_picked_by_code(d, expected):
    assert outline.concept(d) == expected


def test_panel_only_for_areas_that_are_one_panel():
    assert outline.panel(dmg(area="Front bumper")) == "bumper"
    assert outline.panel(dmg(area="Hood")) == "hood"
    assert outline.panel(dmg(area="Left side")) is None


# --- crop and cap ---


def test_crop_adds_a_quarter_margin_and_clamps():
    assert outline.crop_box([200, 400, 600, 800], 1000, 500) == [100, 150, 700, 450]
    assert outline.crop_box([0, 0, 1000, 1000], 378, 205) == [0, 0, 378, 205]


def test_sam_input_is_never_larger_than_the_limit():
    assert outline.sized_for_sam(Image.new("RGB", (4032, 3024))).size == (1024, 768)
    assert outline.sized_for_sam(Image.new("RGB", (300, 200))).size == (300, 200)


def keyed(item: str, entries: list[dict]) -> list[dict]:
    return [{**d, "item_id": item, "slot_id": "damage_closeup", "number": n, "key": f"{item}:{n}"}
            for n, d in enumerate(entries, 1)]


def test_plan_outlines_each_physical_damage_once_before_its_other_photos():
    close = keyed("A", [dmg(), dmg(area="Hood", damage_type="SCRATCH_SCUFF")])
    wide = keyed("B", [dmg(box=[100, 100, 300, 300])])
    damages = close + wide
    p = outline.plan(damages, damage.merge(damages), 2)
    assert p == {"run": ["A:1", "A:2"], "capped": ["B:1"], "no_box": []}
    assert outline.plan(damages, damage.merge(damages), 0)["run"] == []


def test_damages_without_a_box_are_never_sent():
    damages = keyed("A", [dmg(box=None), dmg()])
    assert outline.plan(damages, damage.merge(damages), 6) == {"run": ["A:2"], "capped": [], "no_box": ["A:1"]}


# --- agreement and area (fixture crop used as the whole photo) ---


def found(image: str, concept: str) -> tuple[list[dict], tuple]:
    size = Image.open(FIXTURES / image).size
    return outline.regions(segment.parse(fixture(image, concept)), [0, 0, *size]), size


def test_sam_agrees_when_its_region_overlaps_muses_box_and_area_comes_from_the_mask():
    regions, (w, h) = found("crops/damage_red_front_0.jpg", "hole")
    out = outline.derive([450, 100, 750, 600], regions, None, w, h)
    assert out["sam_agrees"] == "yes" and len(out["regions"]) == 1
    mask = out["regions"][0]["mask"]
    assert out["area"]["basis"] == "mask"
    assert out["area"]["photo_pct"] == pytest.approx(100 * sum(mask) / (w * h), abs=0.2)
    assert out["area"]["panel_pct"] is None  # no panel asked: missing, not 0


def test_sam_finding_nothing_in_the_box_is_a_disagreement_not_a_removal():
    regions, (w, h) = found("crops/damage_red_front_0.jpg", "hole")
    out = outline.derive([0, 0, 200, 200], regions, None, w, h)  # Muse's box is elsewhere
    assert out["sam_agrees"] == "no" and out["regions"] == []
    assert out["area"]["basis"] == "box estimate"
    assert outline.derive([0, 0, 200, 200], [], None, w, h)["sam_agrees"] == "no"


def test_only_overlapping_instances_are_kept():
    regions, (w, h) = found("crops/damage_red_front_1.jpg", "hole")  # 8 small holes spread over the crop
    out = outline.derive([600, 100, 700, 350], regions, None, w, h)
    assert out["sam_agrees"] == "yes" and 0 < len(out["regions"]) < len(regions)


def test_undecoded_mask_falls_back_to_a_box_estimate():
    regions, (w, h) = found("crops/damage_red_front_0.jpg", "hole")
    undecoded = [{**r, "mask": None, "mask_size": None} for r in regions]
    out = outline.derive([450, 100, 750, 600], undecoded, None, w, h)
    x1, y1, x2, y2 = undecoded[0]["box"]
    assert out["area"]["basis"] == "box estimate"
    assert out["area"]["photo_pct"] == pytest.approx(100 * (x2 - x1) * (y2 - y1) / (w * h), abs=0.3)
    assert outline.public_regions(out["regions"], w, h)[0] == {
        "box": [round(x1 * 1000 / w, 1), round(y1 * 1000 / h, 1), round(x2 * 1000 / w, 1), round(y2 * 1000 / h, 1)],
        "mask": "not_decoded", "overlay": None}


def test_share_of_panel_uses_the_panel_instance_that_holds_the_damage():
    regions, (w, h) = found("crops/damage_red_front_0.jpg", "hole")
    hole = regions[0]
    panel = [{"box": [0, 0, w, h], "mask": None, "mask_size": None},          # the whole crop as a panel
             {"box": [0, 0, 10, 10], "mask": None, "mask_size": None}]        # another panel, not touching
    out = outline.derive([450, 100, 750, 600], [hole], panel, w, h)
    assert out["area"]["panel_pct"] == pytest.approx(out["area"]["photo_pct"], abs=0.5)
    assert out["area"]["basis"] == "box estimate"  # the panel had no mask
    far = [{"box": [0, 0, 10, 10], "mask": None, "mask_size": None}]
    assert outline.derive([450, 100, 750, 600], [hole], far, w, h)["area"]["panel_pct"] is None


def test_not_run_keeps_muses_box_and_says_so():
    out = outline.not_run(dmg(box=[0, 0, 500, 500]), (200, 100))
    assert out["sam_agrees"] == "not_run" and out["regions"] == []
    assert out["area"] == {"photo_pct": 25.0, "panel_pct": None, "basis": "box estimate", "panel": None}
    assert outline.not_run(dmg(box=None), None) == {"concept": None, "sam_agrees": "not_run", "regions": [],
                                                    "area": None}


def test_overlay_is_box_sized_with_an_opaque_edge_and_a_light_fill():
    regions, (w, h) = found("crops/damage_red_front_0.jpg", "hole")
    r = regions[0]
    img = Image.open(io.BytesIO(base64.b64decode(outline.overlay_png(r["mask"], *r["mask_size"]))))
    assert img.size == tuple(r["mask_size"]) and img.mode == "RGBA"
    alphas = {value for value, n in enumerate(img.getchannel("A").histogram()) if n}
    assert alphas == {0, 70, 255}
