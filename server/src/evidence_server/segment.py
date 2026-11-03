"""Read SAM 3.1 answers (track 6): text -> instances -> boxes and masks in photo pixels.

Pure: no database, no network. The wire format is Meta's SAM 3 text protocol
(github.com/meta-models/meta-sam, protocol/sam3.md): one line per frame, a
frame marker `<Nf>`, then comma-separated records `ID<|box;...|><|mask;...|>`.
Box coordinates are inclusive pixels of the image as SAM saw it (`w`, `h`);
the mask is box-sized (downscaled to at most 1024 px a side) and decoded by
sam_mask. An empty answer means SAM found nothing.
"""

import re

from . import sam_mask

MODEL = "sam-3.1"

_FRAME = re.compile(r"<(\d+)f(?:;[^>]*)?>")
_RECORD = re.compile(r",?(\d+)((?:<\|[A-Za-z][A-Za-z0-9_.-]*(?:;[^|]*)?\|>)+)")
_TOKEN = re.compile(r"<\|([A-Za-z][A-Za-z0-9_.-]*)((?:;[^|]*)?)\|>")
_INT = re.compile(r"-?\d+")


def parse(text) -> dict:
    """SAM output_text -> {"instances": [instance, ...], "malformed": int}.

    instance = {"object_id": str, "box": [x1, y1, x2, y2] (inclusive), "size": [w, h],
                "mask": {"width", "height", "payload"} | None}
    Image answers use frame 0 only; other frames and anything unreadable count as malformed."""
    out = {"instances": [], "malformed": 0}
    if not isinstance(text, str):
        out["malformed"] = 1
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        frame = _FRAME.match(line)
        if frame is None or frame.group(1) != "0":
            out["malformed"] += 1
            continue
        body = line[frame.end():]
        pos = 0
        if not body:
            out["malformed"] += 1
        while pos < len(body):
            record = _RECORD.match(body, pos)
            if record is None:
                out["malformed"] += 1
                break
            instance = _instance(record.group(1), record.group(2))
            if instance is None:
                out["malformed"] += 1
            else:
                out["instances"].append(instance)
            pos = record.end()
    return out


def raster(instance: dict) -> bytes | None:
    """The instance's mask as width*height bytes of 0/1 (row-major), or None when it does not decode."""
    mask = instance.get("mask")
    if not mask:
        return None
    try:
        return sam_mask.decode(mask["payload"], mask["width"], mask["height"])
    except sam_mask.MaskError:
        return None


def to_photo(box: list, size: list, crop: list) -> list[float]:
    """An inclusive SAM box in the sent image (size = [w, h] as SAM saw it) -> a half-open box in photo
    pixels, given the crop [x1, y1, x2, y2] (photo pixels, half-open) that image was made from."""
    sx = (crop[2] - crop[0]) / size[0]
    sy = (crop[3] - crop[1]) / size[1]
    return [crop[0] + box[0] * sx, crop[1] + box[1] * sy,
            crop[0] + (box[2] + 1) * sx, crop[1] + (box[3] + 1) * sy]


def _fields(raw: str) -> dict | None:
    """';k=v;k=v' -> {k: v}; None when a key repeats (other than the optional confidence c)."""
    fields: dict = {}
    for part in raw.split(";"):
        if not part.strip():
            continue
        key, _, value = part.partition("=")
        key = key.strip()
        if key in fields and key != "c":
            return None
        fields[key] = value.strip()
    return fields


def _instance(object_id: str, tokens: str) -> dict | None:
    box = mask = None
    for name, raw in _TOKEN.findall(tokens):
        if name not in ("box", "mask"):
            continue  # unknown tokens are ignored, as the protocol says
        if (box if name == "box" else mask) is not None:
            return None
        fields = _fields(raw)
        if fields is None:
            return None
        if name == "box":
            box = fields
        else:
            mask = fields
    if box is None or mask is None:
        return None
    try:
        x1, y1, x2, y2 = (_int(box[k]) for k in ("x1", "y1", "x2", "y2"))
        w, h = _int(box["w"]), _int(box["h"])
    except (KeyError, ValueError):
        return None
    if w <= 0 or h <= 0 or not (0 <= x1 <= x2 < w and 0 <= y1 <= y2 < h):
        return None
    return {"object_id": object_id, "box": [x1, y1, x2, y2], "size": [w, h], "mask": _mask(mask)}


def _mask(fields: dict) -> dict | None:
    """data=H,W,payload -> {"width", "height", "payload"}; None when the tuple is unreadable."""
    height, _, rest = (fields.get("data") or "").partition(",")
    width, _, payload = rest.partition(",")
    try:
        h, w = _int(height), _int(width)
    except ValueError:
        return None
    if h <= 0 or w <= 0 or not payload:
        return None
    return {"width": w, "height": h, "payload": payload}


def _int(value: str) -> int:
    if not _INT.fullmatch(value or ""):
        raise ValueError(value)
    return int(value)
