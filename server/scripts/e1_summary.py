"""Write the measured accuracy shown on System health from two E1 runs (photos, planted claims).

    uv run python scripts/e1_summary.py eval/out/<photos run>/result.json eval/out/<claims run>/result.json \
        --measured-on 2026-11-03 --provider fireworks

Only counts and intervals leave the run: no plate, VIN or file name, so the output can live in the repo
(src/evidence_server/reference/model_accuracy.json). The set itself stays in data/e1 (gitignored).
"""

import argparse
import json
from pathlib import Path

from evidence_server.evaluation import wilson
from evidence_server.meta_client import PROVIDERS

OUT = Path(__file__).resolve().parents[1] / "src" / "evidence_server" / "reference" / "model_accuracy.json"


def row(rid: str, label: str, hits: int, n: int, lower_is_better: bool = False) -> dict:
    return {"id": rid, "label": label, "hits": hits, "n": n, "ci": wilson(hits, n), "lower_is_better": lower_is_better}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("photos", type=Path)
    p.add_argument("claims", type=Path)
    p.add_argument("--measured-on", required=True)
    p.add_argument("--provider", choices=tuple(PROVIDERS), required=True)
    p.add_argument("--out", type=Path, default=OUT)
    a = p.parse_args()
    r, c = json.loads(a.photos.read_text()), json.loads(a.claims.read_text())
    reads, retake, dmg, e2 = r["reads"], r["retake"], r["damage"], c["e2"]["overall"]
    good = retake["fine"] + retake["false_retakes"]
    rows = [
        row("plate", "Reads the license plate exactly", reads["plate"]["exact"], reads["plate"]["labelled"]),
        row("vin", "Reads the VIN exactly", reads["vin"]["exact"], reads["vin"]["labelled"]),
        row("odometer", "Reads the odometer exactly", reads["odometer"]["exact"], reads["odometer"]["labelled"]),
        row("retake_caught", "Asks for another shot of a blurred, dark, wrong or unreadable photo",
            retake["caught"], retake["should_retake"]),
        row("retake_false", "Asks for another shot of a good photo", retake["false_retakes"], good, True),
        row("damage_found", "Finds each damaged area a person marked", dmg["overall_strict"]["tp"],
            dmg["overall_strict"]["tp"] + dmg["overall_strict"]["fn"]),
        row("damage_placed", "Names areas a person also marked, or the one next to it",
            dmg["overall_adjacent"]["tp"], dmg["overall_adjacent"]["tp"] + dmg["overall_adjacent"]["fp"]),
        row("damage_type", "Names the right kind of damage", dmg["type_agreement"]["hits"], dmg["type_agreement"]["of"]),
        row("severity", "Rates the severity the way a person did", dmg["severity_agreement"]["hits"],
            dmg["severity_agreement"]["of"]),
        row("false_damage", "Calls an undamaged car damaged", dmg["undamaged"]["with_false_damage"],
            dmg["undamaged"]["photos"], True),
        row("planted_caught", "Raises the planted flag on a claim with a planted problem", e2["tp"], e2["tp"] + e2["fn"]),
        row("flags_planted", "Flags raised on those claims that were planted", e2["tp"], e2["tp"] + e2["fp"]),
    ]
    model = PROVIDERS[a.provider]
    out = {
        "measured_on": a.measured_on,
        "model": model["model"], "host": model["host"], "provider": a.provider,
        "set": {"photos": r["counts"]["photos"], "claims": c["counts"]["claims"],
                "about": "Public photos (Wikimedia Commons, free licences) labelled by hand, plates, VIN labels and "
                         "odometers drawn with exact answers, and blurred, dark and messaging-app copies. "
                         "A test set, not claims from this deployment."},
        "rows": rows,
        "cost": {"calls": r["cost"]["total"]["calls"] + c["cost"]["total"]["calls"],
                 "usd": round(r["cost"]["total"]["cost_usd_uncached"] + c["cost"]["total"]["cost_usd_uncached"], 4),
                 "p50_s": r["cost"]["total"]["p50_s"], "p95_s": r["cost"]["total"]["p95_s"]},
    }
    a.out.write_text(json.dumps(out, indent=1) + "\n")
    print(f"wrote {a.out}")
    for x in rows:
        print(f"  {x['label']}: {x['hits']}/{x['n']} {x['ci']}")


if __name__ == "__main__":
    main()
