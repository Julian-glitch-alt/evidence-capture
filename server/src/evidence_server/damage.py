"""Damage reading after submit (T20, tasks/t20-design.md): the model reads, this code decides.

Pure: no database, no clock, no network. A reading never blocks a claim;
the adjuster sees the table and the boxes, the claimant never does.
"""

from datetime import datetime, timedelta

from .photo_prompts import AREAS
from . import viewpoint as _viewpoint
from .vocab import DAMAGE_TYPES

VERSION = "damage/v2"
DAMAGE_SLOTS = ("damage_closeup", "damage_wide")
SEVERITIES = ("minor", "moderate", "severe")
AGES = ("new", "old", "unsure")
MAX_DAMAGES = 12
STALE_AFTER = timedelta(seconds=120)
SAME_PHOTO_OVERLAP = 0.5  # share of the smaller box two listings must cover to be one damage
_SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


def shape(answer) -> dict | None:
    """One model answer -> {"is_vehicle": bool, "unsure": bool, "damages": [damage, ...], "dropped": int},
    or None when the answer is not usable (the wrong shape entirely)."""
    if not isinstance(answer, dict):
        return None
    if type(answer.get("is_vehicle")) is not bool:
        return None
    listed = answer.get("damages")
    if not isinstance(listed, list):
        return None
    unsure = answer.get("unsure")
    view = answer.get("viewpoint")
    view = view if view in _viewpoint.VIEWPOINTS else "unknown"
    out = {
        "is_vehicle": answer["is_vehicle"],
        "viewpoint": view,
        "unsure": unsure if type(unsure) is bool else False,
        "damages": [],
        "dropped": 0,
    }
    if not answer["is_vehicle"]:
        out["dropped"] = len(listed)
        return out
    for entry in listed:
        kept = _damage(entry, view)
        if kept is None or len(out["damages"]) >= MAX_DAMAGES:
            out["dropped"] += 1
        else:
            out["damages"].append(kept)
    return out


def view(rows: list[dict], now: datetime) -> dict:
    """rows: the claim's `damage` analyses for its CURRENT photos, in photo order, each
    {"item_id", "slot_id", "status": "pending"|"done"|"failed", "result": dict | None, "error": str | None, "started_at": str}.
    -> {"status": "none"|"pending"|"done"|"partial"|"failed", "photos": [...], "damages": [...],
        "merged": merge(damages), "areas": [...], "old_damage": bool}
    `damages` stays per photo (boxes for display); `merged` counts each physical damage once."""
    photos = [_photo(row, now) for row in rows]
    damages = [
        {"item_id": photo["item_id"], "slot_id": photo["slot_id"], **d}
        for photo in photos
        if photo["status"] == "done"
        for d in photo["damages"]
    ]
    if not photos:
        status = "none"
    elif any(photo["status"] == "pending" for photo in photos):
        status = "pending"
    elif all(photo["status"] == "done" for photo in photos):
        status = "done"
    elif all(photo["status"] == "failed" for photo in photos):
        status = "failed"
    else:
        status = "partial"
    return {
        "status": status,
        "photos": photos,
        "damages": damages,
        "merged": merge(damages),
        "areas": [area for area in AREAS if any(d["area"] == area and d["age"] != "old" for d in damages)],
        "old_damage": any(d["age"] == "old" for d in damages),
    }


def merge(damages: list[dict]) -> list[dict]:
    """Per-photo damages (view() order, each with item_id and slot_id) -> one entry per physical damage.

    The close-up and the wide photo usually show the same dent, so a damage is
    counted once: listings with the same area, damage type and age group (old or
    not old) in different photos are one damage; in the same photo only when their
    boxes overlap (two separate dents on one panel stay two). Each photo joins a
    damage at most once, so the count is never below the most any one photo shows.
    The merged damage keeps the highest severity and every photo reference; the
    first reference with that severity is its primary (item_id, slot_id, number,
    box, observed). Old and not-old listings never merge: old damage is never
    priced, so a disagreement on age keeps one priced copy at most.
    """
    groups: list[dict] = []
    counts: dict = {}
    for d in damages or []:
        item = d.get("item_id")
        counts[item] = counts.get(item, 0) + 1
        ref = {"item_id": item, "slot_id": d.get("slot_id"), "number": counts[item],
               "box": d.get("box"), "severity": d.get("severity"), "age": d.get("age"),
               "observed": d.get("observed") if isinstance(d.get("observed"), str) else ""}
        key = (d.get("area"), d.get("damage_type"), d.get("age") == "old")
        target = None
        for group in groups:
            if group["key"] != key:
                continue
            same_photo = [m for m in group["refs"] if m["item_id"] == item]
            if not same_photo or any(_overlaps(m["box"], ref["box"]) for m in same_photo):
                target = group
                break
        if target is None:
            groups.append({"key": key, "area": d.get("area"), "damage_type": d.get("damage_type"), "refs": [ref]})
        else:
            target["refs"].append(ref)
    return [_merged(group) for group in groups]


def _merged(group: dict) -> dict:
    refs = group["refs"]
    primary = max(refs, key=lambda r: _SEVERITY_RANK.get(r["severity"], -1))  # max() keeps the first on ties
    if group["key"][2]:
        age = "old"
    elif any(r["age"] == "new" for r in refs):
        age = "new"
    else:
        age = "unsure"
    return {
        "area": group["area"],
        "damage_type": group["damage_type"],
        "severity": primary["severity"],
        "age": age,
        "item_id": primary["item_id"],
        "slot_id": primary["slot_id"],
        "number": primary["number"],
        "box": primary["box"],
        "observed": primary["observed"],
        "evidence": [{"item_id": r["item_id"], "slot_id": r["slot_id"], "number": r["number"]} for r in refs],
        "keys": [f"{r['item_id']}:{r['number']}" for r in refs],
    }


def _overlaps(a, b) -> bool:
    """True when two boxes cover at least SAME_PHOTO_OVERLAP of the smaller one; no box never overlaps."""
    if not (isinstance(a, list) and isinstance(b, list) and len(a) == 4 and len(b) == 4):
        return False
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return False
    smaller = min((a[2] - a[0]) * (a[3] - a[1]), (b[2] - b[0]) * (b[3] - b[1]))
    return smaller > 0 and (w * h) / smaller >= SAME_PHOTO_OVERLAP


def _damage(entry, view: str = "unknown") -> dict | None:
    """One listed damage -> exactly the kept keys, or None when it fits no fixed list.

    The area is resolved with the photo angle and the box centre; a corrected
    damage keeps the model's word in "area_read". Everything downstream reads
    the resolved "area"."""
    if not isinstance(entry, dict):
        return None
    if not isinstance(entry.get("area"), str) or entry["area"] not in AREAS:
        return None
    if not isinstance(entry.get("damage_type"), str) or entry["damage_type"] not in DAMAGE_TYPES:
        return None
    if not isinstance(entry.get("severity"), str) or entry["severity"] not in SEVERITIES:
        return None
    age = entry.get("age")
    observed = entry.get("observed")
    box = _box(entry.get("box"))
    resolved, corrected = _viewpoint.resolve(entry["area"], view, _viewpoint.image_side_of_box(box))
    kept = {
        "area": resolved,
        "damage_type": entry["damage_type"],
        "severity": entry["severity"],
        "age": age if age in AGES else "unsure",
        "box": box,
        "observed": observed.strip()[:200] if isinstance(observed, str) else "",
    }
    if corrected:
        kept["area_read"] = entry["area"]
    return kept


def _box(value) -> list | None:
    """Four numbers rounded to ints and clamped to 0-1000, or None when not a box."""
    if not isinstance(value, list) or len(value) != 4:
        return None
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value):
        return None
    try:
        x1, y1, x2, y2 = (max(0, min(1000, round(v))) for v in value)
    except (OverflowError, ValueError):
        return None
    if x1 >= x2 or y1 >= y2:
        return None
    return [x1, y1, x2, y2]


_EMPTY = {"is_vehicle": None, "unsure": False, "damages": [], "dropped": 0}


def _photo(row: dict, now: datetime) -> dict:
    """One row -> {"item_id", "slot_id", "status", "error", "is_vehicle", "unsure", "damages", "dropped"}."""
    base = {"item_id": row["item_id"], "slot_id": row["slot_id"]}
    if row.get("status") == "done":
        result = row.get("result") or _EMPTY
        return {
            **base, "status": "done", "error": None,
            "is_vehicle": result.get("is_vehicle"), "unsure": result.get("unsure", False),
            "damages": result.get("damages", []), "dropped": result.get("dropped", 0),
        }
    if row.get("status") == "failed":
        return {**base, "status": "failed", "error": row.get("error"), **_EMPTY}
    started = _parse_dt(row.get("started_at"))
    if started is not None and now - started > STALE_AFTER:
        return {**base, "status": "failed", "error": "stale", **_EMPTY}
    return {**base, "status": "pending", "error": None, **_EMPTY}


def _parse_dt(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
