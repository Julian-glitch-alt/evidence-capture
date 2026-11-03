"""Rough repair-cost range from the stored damage reading (T22, tasks/t22-t25-design.md).

Pure: no database, no clock, no network, no model. Money comes only from the
shipped ballpark table plus the policy deductible (applied by proposal.build):
no vehicle model, no local labour rate, no hidden damage, so this is a range
for triage, never an estimate.
"""

import json
import math
from pathlib import Path

from . import consistency, damage

VERSION = "estimate/v2"
Z80 = 1.2816  # two-sided 80% interval of a normal distribution
STEP = 50  # dollars; lows round down, highs round up

BASIS = (
    "PERT 80% interval from public repair-cost ballparks (repair_ballpark.json)."
    " Not an estimate: no vehicle model, local labour rate or hidden damage."
)


def load_table() -> dict:
    """The {"damage_type": {severity: [low, high] | None, "sources": [...]}} table next to this package."""
    path = Path(__file__).parent / "reference" / "repair_ballpark.json"
    return json.loads(path.read_text())["types"]


TABLE = load_table()


def rough_range(reading: dict, table: dict = TABLE) -> dict:
    """reading = damage.view(...) output -> priced/unpriced/excluded lists plus full, likely and midpoint.

    Prices damage.merge(reading["damages"]): a dent seen in the close-up and the wide
    photo is priced once, at its highest severity, with every photo as evidence."""
    if not consistency.usable(reading):
        return {
            "status": "none", "priced": [], "unpriced": [], "excluded_old": [],
            "full": None, "likely": None, "midpoint": None,
            "basis": BASIS, "version": VERSION,
        }
    priced, unpriced, excluded = [], [], []
    for d in damage.merge(reading.get("damages") or []):
        entry = {
            "item_id": d["item_id"], "slot_id": d["slot_id"], "number": d["number"],  # the primary photo
            "area": d["area"], "damage_type": d["damage_type"], "severity": d["severity"],
            "evidence": d["evidence"],  # every photo that shows it; number counts the photo's own damages
        }
        if d.get("age") == "old":
            excluded.append(entry)  # old damage is never priced
            continue
        cell = (table.get(d.get("damage_type")) or {}).get(d.get("severity"))
        if cell is None:  # no trustworthy figure: flag it, never silently shrink the range
            unpriced.append(entry)
        else:
            priced.append({**entry, "low": cell[0], "high": cell[1]})
    if not priced:
        full = likely = [0, 0]
        midpoint = 0
    else:
        lows = sum(p["low"] for p in priced)
        highs = sum(p["high"] for p in priced)
        mean = sum((p["low"] + p["high"]) / 2 for p in priced)
        sd = math.sqrt(sum(((p["high"] - p["low"]) / 6) ** 2 for p in priced))
        full = [_floor(lows), _ceil(highs)]
        likely = [_floor(max(lows, mean - Z80 * sd)), _ceil(min(highs, mean + Z80 * sd))]
        midpoint = _round(mean)
    return {
        "status": "incomplete" if unpriced else "complete",
        "priced": priced, "unpriced": unpriced, "excluded_old": excluded,
        "full": full, "likely": likely, "midpoint": midpoint,
        "basis": BASIS, "version": VERSION,
    }


def _floor(value) -> int:
    """Round money down to the step."""
    return math.floor(value / STEP) * STEP


def _ceil(value) -> int:
    """Round money up to the step."""
    return math.ceil(value / STEP) * STEP


def _round(value) -> int:
    """Round money to the nearest step, half up (banker's rounding would drift a range)."""
    return math.floor(value / STEP + 0.5) * STEP
