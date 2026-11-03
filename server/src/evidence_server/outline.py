"""Damage outlines (track 6): Muse Spark points, SAM 3.1 outlines, code measures.

Pure except `ask`, which sends one image through MetaClient.segment. For each damage Muse Spark boxed, the store crops
the photo around the box with a margin and asks SAM 3.1 to segment a concept
this module picks from the damage type. S0 (28 Oct) found SAM on a full photo
finds no "dent" or "car damage", but on the crop "damage", "hole" and "crack"
outline the damage tightly.

Code then derives, per damage:
- sam_agrees: "yes" when SAM found a region overlapping Muse's box, "no" when it
  did not (two models disagree: a review note, never removed, never a fraud
  signal), "not_run" when SAM was not asked or failed. Model-only.
- area as a share of the photo and, when the panel was segmented too, of the
  panel. "mask" when SAM's mask was decoded; otherwise "box estimate".
Nothing here touches money, severity or routing.
"""

import base64
import io

from PIL import Image, ImageChops, ImageFilter

from . import segment

VERSION = "outline/v1"
CROP_MARGIN = 0.25         # each side, as a share of the box (S0 used 25%)
SEND_MAX_SIDE = 1024       # longest side of an image sent to SAM; SAM downscales masks to 1024 anyway
AGREE_OVERLAP = 0.1        # share of the smaller box a SAM region must cover to overlap Muse's box
AREA_GRID = 512            # longest side of the grid areas are counted on (shares do not depend on it)
OUTLINE_RGB = (0, 184, 255)
CAPTION = "outline: SAM 3.1 · box: Muse Spark"
NOTE_DISAGREE = "The outline model found nothing in this box."
NOTE_MISSING = "Outline not available."

# The concept SAM is asked for, chosen by code. "damage" is the general concept;
# words in Muse's own description pick "crack" or "hole" when they name one.
CONCEPTS = {
    "GLASS_DAMAGE": "crack",
    "LIGHTING_DAMAGE": "crack",
}
GENERAL_CONCEPT = "damage"
OBSERVED_CONCEPTS = (  # first match wins
    (("hole", "holes", "puncture", "punctured"), "hole"),
    (("crack", "cracked", "cracks", "shattered"), "crack"),
)
# The panel SAM segments on the whole photo, for the damage's share of its panel.
# Areas that span several panels (a side, a corner) have none.
PANELS = {
    "Front bumper": "bumper",
    "Rear bumper": "bumper",
    "Hood": "hood",
    "Trunk or tailgate": "trunk lid",
    "Roof": "roof",
    "Windshield": "windshield",
    "Rear window": "rear window",
    "Wheels or tires": "wheel",
}


def concept(damage: dict) -> str:
    """The SAM concept for one Muse damage: from its description words, else its type, else "damage"."""
    words = set("".join(c if c.isalpha() else " " for c in (damage.get("observed") or "").lower()).split())
    for keys, word in OBSERVED_CONCEPTS:
        if words.intersection(keys):
            return word
    return CONCEPTS.get(damage.get("damage_type"), GENERAL_CONCEPT)


def panel(damage: dict) -> str | None:
    return PANELS.get(damage.get("area"))


def crop_box(box: list, width: int, height: int, margin: float = CROP_MARGIN) -> list[int]:
    """Muse box (0-1000 of the photo) -> the crop around it in photo pixels, half-open, clamped."""
    x1, y1, x2, y2 = box
    mx, my = (x2 - x1) * margin, (y2 - y1) * margin
    left = max(0, int((x1 - mx) * width / 1000))
    top = max(0, int((y1 - my) * height / 1000))
    right = min(width, max(left + 1, round((x2 + mx) * width / 1000)))
    bottom = min(height, max(top + 1, round((y2 + my) * height / 1000)))
    return [left, top, right, bottom]


def plan(damages: list[dict], merged: list[dict], max_calls: int) -> dict:
    """Which damages SAM outlines, within the per-claim cap.

    damages: the per-photo damages that can be sent (their photo opens), with "key" (f"{item_id}:{number}")
    and "box". merged: damage.merge(every damage); each physical damage's primary listing goes first, then
    the other photos of the same damage. -> {"run": [key, ...], "capped": [key, ...], "no_box": [key, ...]}"""
    boxed = {d["key"] for d in damages if d.get("box")}
    order: list[str] = []
    for group in merged:
        primary = f"{group['item_id']}:{group['number']}"
        if primary in boxed and primary not in order:
            order.append(primary)
    for group in merged:
        order.extend(k for k in group["keys"] if k in boxed and k not in order)
    order.extend(d["key"] for d in damages if d["key"] in boxed and d["key"] not in order)
    limit = max(0, max_calls)
    return {"run": order[:limit], "capped": order[limit:],
            "no_box": [d["key"] for d in damages if d["key"] not in boxed]}


def sized_for_sam(image: Image.Image) -> Image.Image:
    """The image downscaled so its longest side is at most SEND_MAX_SIDE (never upscaled)."""
    w, h = image.size
    scale = SEND_MAX_SIDE / max(w, h)
    if scale >= 1:
        return image
    return image.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.LANCZOS)


def ask(meta, image: Image.Image, concept_text: str, *, purpose: str, claim_id: str | None,
        item_id: str | None) -> dict:
    """One SAM call on a Pillow image (a crop or the whole photo), downscaled and sent as JPEG.
    -> MetaClient.segment's result; used by the store and the evaluation harness alike."""
    buf = io.BytesIO()
    sized_for_sam(image).save(buf, "JPEG", quality=90)
    return meta.segment(buf.getvalue(), concept_text, purpose=purpose, claim_id=claim_id, item_id=item_id)


def regions(answer: dict, crop: list) -> list[dict]:
    """segment.parse output for an image made from `crop` -> regions in photo pixels:
    [{"box": [x1, y1, x2, y2] (photo px, half-open), "mask": bytes | None, "mask_size": [w, h] | None}]"""
    out = []
    for inst in answer["instances"]:
        mask = segment.raster(inst)
        out.append({
            "box": segment.to_photo(inst["box"], inst["size"], crop),
            "mask": mask,
            "mask_size": [inst["mask"]["width"], inst["mask"]["height"]] if mask is not None else None,
        })
    return out


def derive(muse_box: list, found: list[dict], panels: list[dict] | None, width: int, height: int) -> dict:
    """One damage's outline result from SAM's regions (photo pixels) and, if run, the panel's regions.

    -> {"sam_agrees": "yes"|"no", "regions": [...kept regions...], "area": {...}}
    Kept regions are the ones overlapping Muse's box. Area is counted on a grid: masks when every kept
    region's mask decoded ("mask"), else the regions' boxes ("box estimate"). The panel share uses the
    panel instance that overlaps the damage most; none -> missing (None), never 0."""
    muse_px = [muse_box[0] * width / 1000, muse_box[1] * height / 1000,
               muse_box[2] * width / 1000, muse_box[3] * height / 1000]
    kept = [r for r in found if _overlap(r["box"], muse_px) >= AGREE_OVERLAP]
    if not kept:
        return {"sam_agrees": "no", "regions": [], "area": _muse_area(muse_px, width, height)}
    grid = _Grid(width, height)
    basis = "mask" if all(r["mask"] is not None for r in kept) else "box estimate"
    damage_px = grid.paint(kept, use_masks=basis == "mask")
    damage_count = grid.count(damage_px)
    area = {"photo_pct": round(100 * damage_count / grid.total, 2), "panel_pct": None, "basis": basis,
            "panel": None}
    best = None
    for p in panels or []:
        p_basis = "mask" if p["mask"] is not None and basis == "mask" else "box estimate"
        panel_px = grid.paint([p], use_masks=p_basis == "mask")
        inside = grid.count(ImageChops.logical_and(damage_px, panel_px))
        if inside and (best is None or inside > best[0]):
            best = (inside, grid.count(panel_px), p_basis)
    if best is not None and best[1]:
        area["panel_pct"] = round(100 * best[0] / best[1], 1)
        if best[2] != basis:
            area["basis"] = "box estimate"
    return {"sam_agrees": "yes", "regions": kept, "area": area}


def not_run(damage: dict, size: tuple | None) -> dict:
    """The stored result when SAM was not asked or failed: Muse's box stays, "outline not available".
    The area is Muse's box share of the photo ("box estimate") when the photo opened, else missing."""
    box = damage.get("box")
    area = (_muse_area([box[0] * size[0] / 1000, box[1] * size[1] / 1000, box[2] * size[0] / 1000,
                        box[3] * size[1] / 1000], *size) if box and size else None)
    return {"concept": concept(damage) if box else None, "sam_agrees": "not_run", "regions": [], "area": area}


def public_regions(kept: list[dict], width: int, height: int) -> list[dict]:
    """Kept regions as stored and shown: box in 0-1000 of the photo plus an outline overlay PNG
    (base64, box-sized: opaque edge, translucent fill) when the mask decoded."""
    out = []
    for r in kept:
        x1, y1, x2, y2 = r["box"]
        out.append({
            "box": [round(x1 * 1000 / width, 1), round(y1 * 1000 / height, 1),
                    round(x2 * 1000 / width, 1), round(y2 * 1000 / height, 1)],
            "mask": "decoded" if r["mask"] is not None else "not_decoded",
            "overlay": overlay_png(r["mask"], *r["mask_size"]) if r["mask"] is not None else None,
        })
    return out


def overlay_png(mask: bytes, width: int, height: int) -> str:
    """A 0/1 raster -> base64 RGBA PNG: the outline edge opaque, the inside lightly tinted."""
    inside = Image.frombytes("L", (width, height), bytes(v * 255 for v in mask))
    edge = ImageChops.subtract(inside, inside.filter(ImageFilter.MinFilter(3)))
    alpha = ImageChops.lighter(inside.point(lambda v: 70 if v else 0), edge)
    img = Image.new("RGBA", (width, height), OUTLINE_RGB + (0,))
    img.putalpha(alpha)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()


def _overlap(a: list, b: list) -> float:
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return (w * h) / smaller if smaller > 0 else 0.0


def _muse_area(box_px: list, width: int, height: int) -> dict:
    share = max(0.0, box_px[2] - box_px[0]) * max(0.0, box_px[3] - box_px[1]) / (width * height)
    return {"photo_pct": round(100 * share, 2), "panel_pct": None, "basis": "box estimate", "panel": None}


class _Grid:
    """Photo pixels scaled onto a grid at most AREA_GRID a side; regions painted as 1-bit images."""

    def __init__(self, width: int, height: int):
        self.scale = min(1.0, AREA_GRID / max(width, height))
        self.size = (max(1, round(width * self.scale)), max(1, round(height * self.scale)))
        self.total = self.size[0] * self.size[1]

    def paint(self, found: list[dict], use_masks: bool) -> Image.Image:
        canvas = Image.new("1", self.size, 0)
        for r in found:
            x1, y1, x2, y2 = (round(v * self.scale) for v in r["box"])
            w, h = max(1, x2 - x1), max(1, y2 - y1)
            if use_masks and r["mask"] is not None:
                mw, mh = r["mask_size"]
                piece = Image.frombytes("L", (mw, mh), bytes(v * 255 for v in r["mask"]))
                piece = piece.resize((w, h), Image.NEAREST).point(lambda v: 255 if v >= 128 else 0).convert("1")
            else:
                piece = Image.new("1", (w, h), 1)
            canvas.paste(1, (x1, y1), piece)
        return canvas

    @staticmethod
    def count(image: Image.Image) -> int:
        return image.convert("L").histogram()[255]
