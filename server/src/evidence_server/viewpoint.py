"""Vehicle side worked out from the photo angle (T49, tasks/t49-design.md).

Pure: no database, no clock, no network, no model. The model reports where the
camera is (viewpoint) and where the damage is in the photo (image_side); this
module works out which side of the vehicle that is. The model is weakest at
mirroring (in a photo of the front, the car's left is on the photo's right),
so code swaps the side and keeps the model's word for the audit trail.
"""

VIEWPOINTS = (
    "front",
    "rear",
    "left side",
    "right side",
    "front left corner",
    "front right corner",
    "rear left corner",
    "rear right corner",
    "inside",
    "underneath",
    "unknown",
)

IMAGE_SIDES = ("left", "right", "middle", "unknown")

_SIDED = {
    "Front left": "left",
    "Front right": "right",
    "Left side": "left",
    "Right side": "right",
    "Rear left": "left",
    "Rear right": "right",
}

_SWAP = {
    "Front left": "Front right",
    "Front right": "Front left",
    "Left side": "Right side",
    "Right side": "Left side",
    "Rear left": "Rear right",
    "Rear right": "Rear left",
}


def vehicle_side(viewpoint, image_side) -> str | None:
    """The photo angle + the damage's side in the photo -> the vehicle side ("left"|"right"), or None.

    A missing viewpoint or photo side, or a value outside the lists (older cached
    answers, a model slip), counts as "unknown" and never makes an answer unusable.
    """
    view = viewpoint if viewpoint in VIEWPOINTS else "unknown"
    side = image_side if image_side in IMAGE_SIDES else "unknown"
    if view == "front":
        if side == "left":
            return "right"
        if side == "right":
            return "left"
        return None
    if view == "rear":
        if side == "left":
            return "left"
        if side == "right":
            return "right"
        return None
    if view == "left side":
        return "left"
    if view == "right side":
        return "right"
    return None


def image_side_of_box(box) -> str:
    """The centre x of a damage box (0-1000) -> "left"|"right"|"middle"; a missing or malformed box -> "unknown"."""
    if not isinstance(box, list) or len(box) != 4:
        return "unknown"
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in box):
        return "unknown"
    try:
        centre = (float(box[0]) + float(box[2])) / 2
    except (OverflowError, ValueError):
        return "unknown"
    if centre != centre:  # NaN
        return "unknown"
    if centre < 400:
        return "left"
    if centre > 600:
        return "right"
    return "middle"


_CORNER_SWAPS = {
    "front left corner": {"Right side": "Left side", "Rear right": "Rear left"},
    "front right corner": {"Left side": "Right side", "Rear left": "Rear right"},
    "rear left corner": {"Right side": "Left side", "Front right": "Front left"},
    "rear right corner": {"Left side": "Right side", "Front left": "Front right"},
}


def resolve(area, viewpoint, image_side) -> tuple:
    """A sided area + the photo angle -> (area, corrected).

    Front/rear views swap via vehicle_side; left/right side views take that
    side. From a corner only the far-side areas that cannot show are swapped
    (table above); everything else returns the area unchanged.
    """
    view = viewpoint if viewpoint in VIEWPOINTS else "unknown"
    if view in _CORNER_SWAPS:
        fixed = _CORNER_SWAPS[view].get(area)
        if fixed is not None:
            return fixed, True
        return area, False
    side = vehicle_side(viewpoint, image_side)
    want = _SIDED.get(area)
    if side is None or want is None or side == want:
        return area, False
    return _SWAP[area], True
