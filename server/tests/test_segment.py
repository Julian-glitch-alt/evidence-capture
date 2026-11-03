"""Track 6: SAM 3.1 answers -> instances, decoded masks and photo coordinates (segment.py, sam_mask.py).

Every recorded answer in fixtures/sam/ (S0 probe, 28 Oct) is parsed and every mask decoded."""

import json
import re
from pathlib import Path

import pytest

from evidence_server import sam_mask, segment

FIXTURES = Path(__file__).parent / "fixtures" / "sam"
ANSWERS = json.loads((FIXTURES / "responses.json").read_text())
BOX = re.compile(r"(\d+)<\|box;x1=(\d+);y1=(\d+);x2=(\d+);y2=(\d+);w=(\d+);h=(\d+)\|>")


@pytest.mark.parametrize("answer", ANSWERS, ids=[f"{i}-{a['kind']}-{a['concept']}" for i, a in enumerate(ANSWERS)])
def test_every_recorded_answer_parses_with_every_box(answer):
    parsed = segment.parse(answer["output_text"])
    expected = [tuple(map(int, m.groups())) for m in BOX.finditer(answer["output_text"])]
    assert parsed["malformed"] == 0
    assert [(int(i["object_id"]), *i["box"], *i["size"]) for i in parsed["instances"]] == expected


@pytest.mark.parametrize("answer", [a for a in ANSWERS if a["output_text"]],
                         ids=[f"{i}-{a['kind']}-{a['concept']}" for i, a in enumerate(ANSWERS) if a["output_text"]])
def test_every_recorded_mask_decodes_to_exactly_its_box(answer):
    """The mask is box-sized and its pixels reach all four edges of the box SAM declared:
    a decode that went wrong would not land exactly inside the box on 33 masks."""
    for inst in segment.parse(answer["output_text"])["instances"]:
        x1, y1, x2, y2 = inst["box"]
        mask = inst["mask"]
        assert (mask["width"], mask["height"]) == (x2 - x1 + 1, y2 - y1 + 1)
        raster = segment.raster(inst)
        assert raster is not None and len(raster) == mask["width"] * mask["height"] and set(raster) <= {0, 1}
        w, h = mask["width"], mask["height"]
        rows = [y for y in range(h) if any(raster[y * w:(y + 1) * w])]
        cols = [x for x in range(w) if any(raster[y * w + x] for y in range(h))]
        assert (rows[0], rows[-1], cols[0], cols[-1]) == (0, h - 1, 0, w - 1)


def test_empty_answer_means_nothing_found():
    empty = next(a for a in ANSWERS if a["kind"] == "empty")
    assert segment.parse(empty["output_text"]) == {"instances": [], "malformed": 0}


def test_multi_instance_answer_keeps_every_instance_in_order():
    answer = next(a for a in ANSWERS if a["image"] == "crops/damage_red_front_1.jpg")
    parsed = segment.parse(answer["output_text"])
    assert [i["object_id"] for i in parsed["instances"]] == [str(n) for n in range(8)]
    assert parsed["instances"][0]["box"] == [208, 37, 214, 45]


def test_known_mask_matches_the_crop_it_describes():
    """The rust-hole crop: SAM's 'hole' mask covers the dark hole (about 68% of its box), not the paint."""
    from PIL import Image
    answer = next(a for a in ANSWERS if a["image"] == "crops/damage_red_front_0.jpg" and a["concept"] == "hole")
    [inst] = segment.parse(answer["output_text"])["instances"]
    raster = segment.raster(inst)
    crop = Image.open(FIXTURES / answer["image"]).convert("L")
    x1, y1, x2, y2 = inst["box"]
    w = x2 - x1 + 1
    inside = [crop.getpixel((x1 + i % w, y1 + i // w)) for i, v in enumerate(raster) if v]
    outside = [crop.getpixel((x1 + i % w, y1 + i // w)) for i, v in enumerate(raster) if not v]
    assert 0.6 < sum(raster) / len(raster) < 0.75
    assert sum(inside) / len(inside) < sum(outside) / len(outside)  # the hole is darker than its rim


def test_coordinates_scale_back_to_the_photo():
    # The crop [100, 200, 513, 704) was sent as is (413 x 504): offsets only; x2/y2 are inclusive.
    assert segment.to_photo([50, 196, 307, 385], [413, 504], [100, 200, 513, 704]) == [150, 396, 408, 586]
    # The same crop sent at half size: SAM's pixels are two photo pixels each.
    assert segment.to_photo([25, 98, 153, 192], [207, 252], [100, 200, 514, 704]) == pytest.approx(
        [150, 396, 408, 586], abs=2)


@pytest.mark.parametrize("text", [
    "garbage",
    "<1f>0<|box;x1=1;y1=1;x2=2;y2=2;w=4;h=4|><|mask;x=0;y=0;data=2,2,~!!!!!|>",  # an image answer is frame 0
    "<0f>0<|box;x1=1;y1=1;x2=9;y2=2;w=4;h=4|><|mask;x=0;y=0;data=2,2,~!!!!!|>",  # box outside the image
    "<0f>0<|box;x1=1;y1=1;x2=2;y2=2;w=4;h=4|>",                                   # no mask
    "<0f>0<|box;x1=1;x1=1;y1=1;x2=2;y2=2;w=4;h=4|><|mask;x=0;y=0;data=2,2,~!|>",  # repeated key
    "<0f>",                                                                       # bare frame header
])
def test_malformed_records_are_counted_not_raised(text):
    assert segment.parse(text) == {"instances": [], "malformed": 1}


def test_unknown_tokens_and_fields_are_ignored():
    text = "<0f;x=1>,7<|box;x1=1;y1=1;x2=2;y2=2;w=4;h=4;c=0.9|><|extra|><|mask;x=0;y=0;new=1;data=2,2,~abc|>"
    [inst] = segment.parse(text)["instances"]
    assert (inst["object_id"], inst["box"], inst["mask"]["payload"]) == ("7", [1, 1, 2, 2], "~abc")


@pytest.mark.parametrize("payload, w, h", [
    ("", 2, 2), ("?abc", 2, 2), ("~!!", 2, 2), ("~!!!!#abc", 2, 2), ("~!!!!!", 0, 2), ("~!!!!!", 2048, 2048),
])
def test_bad_payloads_raise_mask_error(payload, w, h):
    with pytest.raises(sam_mask.MaskError):
        sam_mask.decode(payload, w, h)


def test_undecodable_mask_is_none_not_a_guess():
    [inst] = segment.parse("<0f>0<|box;x1=1;y1=1;x2=2;y2=2;w=4;h=4|><|mask;x=0;y=0;data=2,2,~zzzz|>")["instances"]
    assert segment.raster(inst) is None
