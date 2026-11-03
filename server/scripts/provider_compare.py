"""Same photos, same product prompts, two hosts: Meta's Muse Spark 1.3 vs Muse Spark Open on Fireworks.

    uv run --project server python server/scripts/provider_compare.py data/t34_probe data/t43_live OUT_DIR

Needs LLAMA_API_KEY and FIREWORKS_API_KEY (set -a && . .env). Public or synthetic photos only:
the Meta account runs on free credits, where Meta may use what it receives.

Each photo is read once per host with the prompt the product uses for its slot. A case passes when
the answer matches the known truth (from data/t34_probe/results.json, written 30 Oct, and the
t43 photo descriptions below). Writes OUT_DIR/results.json and prints a table.
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import os

from evidence_server import photo_prompts
from evidence_server.meta_client import openai_transport

HOSTS = {
    "meta": ("LLAMA_API_KEY", "https://api.meta.ai/v1", "muse-spark-1.3"),
    "fireworks": ("FIREWORKS_API_KEY", "https://api.fireworks.ai/inference/v1",
                  "accounts/fireworks/models/muse-spark-open"),
}
PRICE = (1.25, 4.25)  # USD per million input / output tokens, the same list price on both hosts

PROMPTS = {
    "plate": photo_prompts.PLATE,
    "vin_photo": photo_prompts.VIN_READ,
    "odometer": photo_prompts.ODOMETER,
    "driver_license": photo_prompts.LICENSE,
    "damage_closeup": photo_prompts.DAMAGE_CLOSEUP,
    "damage_wide": photo_prompts.DAMAGE_WIDE,
}


def _norm(s):
    return "".join(ch for ch in str(s or "").upper() if ch.isalnum())


def _parts(a):
    return {str(p) for p in (a.get("parts") or [])}


# name -> (slot, truth text, check(answer) -> bool). Truths for the t34 photos are those of
# data/t34_probe/results.json; the t43 ones are what the public photo shows (data/t43_live/SOURCES.txt).
CASES = {
    "plate_ca_000K000.jpg": ("plate", "CA plate 000K000", lambda a: a.get("is_plate") is True and _norm(a.get("text")) == "000K000"),
    "plate_synthetic_demo101.jpg": ("plate", "CA plate DEMO101", lambda a: a.get("is_plate") is True and _norm(a.get("text")) == "DEMO101"),
    "odo_malibu.jpg#plate": ("plate", "odometer photo sent as the plate", lambda a: a.get("is_plate") is False),
    "odo_malibu.jpg": ("odometer", "9840 mi", lambda a: a.get("reading") == 9840 and a.get("unit") == "mi"),
    "odo_peugeot_km.jpg": ("odometer", "222222 km", lambda a: a.get("reading") == 222222 and a.get("unit") == "km"),
    "odo_100k.jpg": ("odometer", "100000 mi (trip 208.2 is not the total)", lambda a: a.get("reading") == 100000),
    "vin_synthetic_ok.jpg": ("vin_photo", "VIN 1HGCV1F39LA526018", lambda a: _norm(a.get("vin")) == "1HGCV1F39LA526018"),
    "vin_gmc.jpg": ("vin_photo", "pre-1981 VIN TZE063V101152", lambda a: _norm(a.get("vin")) == "TZE063V101152"),
    "vin_mazda3.jpg": ("vin_photo", "VIN label unreadable: no VIN may be invented", lambda a: not a.get("vin")),
    "vin_manipulated.jpg": ("vin_photo", "scraped VIN: no VIN may be invented", lambda a: not a.get("vin")),
    "license_synthetic.jpg": ("driver_license", "JORDAN AVERY, expires 2029-05-01",
                              lambda a: _norm(a.get("name")) == "JORDANAVERY" and a.get("expires") == "2029-05-01"),
    "dmg_close_mirror.jpg": ("damage_closeup", "shattered side mirror", lambda a: a.get("damage_visible") is True),
    "dmg_close_hitrun.jpg": ("damage_closeup", "front damage after a hit and run", lambda a: a.get("damage_visible") is True),
    "dmg_wide_dodge.jpg": ("damage_wide", "wrecked front of a Dodge Stratus", lambda a: a.get("damage_visible") is True),
    "dmg_wide_letterbox.jpg": ("damage_wide", "car after hitting a letterbox", lambda a: a.get("damage_visible") is True),
    "t43:glass.jpg": ("damage_closeup", "cracked windshield", lambda a: a.get("damage_visible") is True and "Windshield" in _parts(a)),
    "t43:hail.jpg": ("damage_wide", "rear window smashed by hail", lambda a: a.get("damage_visible") is True and "Rear window" in _parts(a)),
    "t43:vandal.jpg": ("damage_closeup", "keyed paint on the side", lambda a: a.get("damage_visible") is True),
    "t43:fire.jpg": ("damage_wide", "burned-out car", lambda a: a.get("damage_visible") is True),
    "t43:flood.jpg": ("damage_wide", "car sunk in flood water", lambda a: a.get("is_vehicle") is True),
}


def image_path(name: str, probe: Path, t43: Path) -> Path:
    if name.startswith("t43:"):
        return t43 / name[4:]
    return probe / "images" / name.split("#")[0]


def read(host: str, name: str, probe: Path, t43: Path) -> dict:
    key_env, base_url, model = HOSTS[host]
    transport = openai_transport(os.environ[key_env], base_url=base_url)
    slot, truth, check = CASES[name]
    image = image_path(name, probe, t43).read_bytes()
    start = time.monotonic()
    try:
        res = transport(model, PROMPTS[slot], image, "image/jpeg", 90)
        answer = json.loads(res["text"])
        error = None
    except Exception as e:  # recorded, counted as a miss
        res, answer, error = {}, None, f"{type(e).__name__}: {e}"[:200]
    seconds = round(time.monotonic() - start, 1)
    in_t, out_t = res.get("input_tokens"), res.get("output_tokens")
    cost = (in_t * PRICE[0] + out_t * PRICE[1]) / 1e6 if in_t is not None and out_t is not None else None
    return {"host": host, "name": name, "slot": slot, "truth": truth, "answer": answer, "error": error,
            "pass": bool(answer) and bool(check(answer)), "seconds": seconds,
            "input_tokens": in_t, "output_tokens": out_t, "cost_usd": cost}


def main(probe: Path, t43: Path, out: Path) -> None:
    jobs = [(h, n) for n in CASES for h in HOSTS]
    with ThreadPoolExecutor(max_workers=8) as pool:
        rows = list(pool.map(lambda job: read(job[0], job[1], probe, t43), jobs))
    out.mkdir(parents=True, exist_ok=True)
    (out / "results.json").write_text(json.dumps(rows, indent=1))
    by = {(r["host"], r["name"]): r for r in rows}
    print(f"{'photo':28} {'truth':44} {'meta':6} {'fireworks':9}")
    for name, (_, truth, _) in CASES.items():
        m, f = by[("meta", name)], by[("fireworks", name)]
        print(f"{name:28} {truth[:44]:44} {'pass' if m['pass'] else 'MISS':6} {'pass' if f['pass'] else 'MISS':9}")
    for host in HOSTS:
        mine = [r for r in rows if r["host"] == host]
        secs = sorted(r["seconds"] for r in mine)
        cost = sum(r["cost_usd"] or 0 for r in mine)
        print(f"{host}: {sum(r['pass'] for r in mine)}/{len(mine)} pass, errors {sum(1 for r in mine if r['error'])}, "
              f"median {secs[len(secs) // 2]} s, max {secs[-1]} s, cost ${cost:.3f}")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
