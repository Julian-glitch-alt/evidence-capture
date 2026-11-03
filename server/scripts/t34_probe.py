"""T34 live probe: the real photo_prompts against Muse Spark on public / synthetic images.

Runs the production transport (meta_client.openai_transport) and the production decision
(photo_check.decide) against policy DEMO-AUTO-1001, so it shows what the phone would show.
Answers are cached under data/t34_probe/calls/ (key = model + prompt + image), so a re-run
costs nothing. Only public (Wikimedia Commons) or synthetic images: free credits mean Meta
may keep what we send.

    cd server && uv run python scripts/t34_probe.py IMAGE_DIR [--synthetic]
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from evidence_server import photo_check, photo_prompts
from evidence_server.meta_client import PRICES_PER_M, openai_transport

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "data" / "t34_probe"
CALLS = OUT / "calls"

# image file -> (slot, what a person sees in it)
CASES = {
    "plate_ca_000K000.jpg": ("plate", "CA plate 000K000 on a Toyota (not the policy plate)"),
    "plate_synthetic_demo101.jpg": ("plate", "synthetic CA plate DEMO101 (the policy plate)"),
    "odo_malibu.jpg": ("odometer", "LCD 9840 MI"),
    "odo_peugeot_km.jpg": ("odometer", "222222 km"),
    "odo_100k.jpg": ("odometer", "100000 main drum, trip 208.2, MPH dial"),
    "vin_synthetic_ok.jpg": ("vin_photo", "synthetic label 1HGCV1F39LA526018 (policy VIN)"),
    "vin_gmc.jpg": ("vin_photo", "pre-1981 GM plate TZE063V101152 (13 chars)"),
    "vin_mazda3.jpg": ("vin_photo", "windshield, VIN label blank / unreadable"),
    "vin_manipulated.jpg": ("vin_photo", "stamped VIN partly scraped off"),
    "license_synthetic.jpg": ("driver_license", "synthetic CA licence JORDAN AVERY exp 2029-05-01"),
    "dmg_close_mirror.jpg": ("damage_closeup", "shattered side mirror"),
    "dmg_close_hitrun.jpg": ("damage_closeup", "hit-and-run damage"),
    "dmg_wide_dodge.jpg": ("damage_wide", "wrecked Dodge Stratus"),
    "dmg_wide_letterbox.jpg": ("damage_wide", "car after hitting a letterbox"),
    # wrong subject: an odometer photo in the plate slot
    "odo_malibu.jpg#plate": ("plate", "odometer photo sent as the plate"),
}

ANSWERS = {
    "vin": "1HGCV1F39LA526018",
    "driver_is_holder": "Yes",
    "loss_date": "2026-10-20",
    "damaged_areas": ["Front bumper", "Hood"],
}


def api_key() -> str:
    if os.environ.get("LLAMA_API_KEY"):
        return os.environ["LLAMA_API_KEY"]
    for line in (ROOT / ".env").read_text().splitlines():
        if line.startswith("LLAMA_API_KEY="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit("LLAMA_API_KEY not found in the environment or .env")


def policy() -> dict:
    seed = json.loads((ROOT / "server/src/evidence_server/seed/policies.json").read_text())
    return next(p for p in seed["policies"] if p["policy_no"] == "DEMO-AUTO-1001")


def make_synthetic(image_dir: Path) -> None:
    """Printed-label style images with invented data (DEMO-AUTO-1001)."""
    from PIL import Image, ImageDraw, ImageFilter, ImageFont

    def font(size):
        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
                     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            if Path(path).exists():
                return ImageFont.truetype(path, size)
        return ImageFont.load_default()

    img = Image.new("RGB", (1280, 720), (70, 74, 78))
    d = ImageDraw.Draw(img)
    d.rectangle((140, 230, 1140, 500), fill=(236, 236, 228))
    d.text((180, 255), "VEHICLE IDENTIFICATION NUMBER", fill=(40, 40, 40), font=font(30))
    d.text((180, 320), "1HGCV1F39LA526018", fill=(15, 15, 15), font=font(72))
    for x in range(180, 1100, 9):
        d.rectangle((x, 430, x + (x * 7 % 5), 480), fill=(20, 20, 20))
    img.rotate(4, fillcolor=(70, 74, 78)).filter(ImageFilter.GaussianBlur(1.2)).save(
        image_dir / "vin_synthetic_ok.jpg", quality=80)

    img = Image.new("RGB", (1280, 720), (120, 125, 130))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((340, 220, 940, 520), 24, fill=(245, 245, 245), outline=(40, 40, 40), width=6)
    d.text((500, 245), "California", fill=(180, 30, 40), font=font(48))
    d.text((395, 330), "DEMO101", fill=(20, 30, 110), font=font(130))
    img.filter(ImageFilter.GaussianBlur(1.0)).save(image_dir / "plate_synthetic_demo101.jpg", quality=80)

    img = Image.new("RGB", (1280, 800), (60, 60, 60))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((200, 150, 1080, 690), 30, fill=(225, 235, 245))
    d.text((240, 175), "CALIFORNIA  DRIVER LICENSE", fill=(20, 50, 120), font=font(44))
    d.rectangle((240, 270, 480, 590), fill=(170, 180, 190))
    rows = [("DL", "D0000101"), ("EXP", "05/01/2029"), ("LN", "AVERY"), ("FN", "JORDAN"),
            ("DOB", "07/14/1988"), ("CLASS", "C")]
    for i, (k, v) in enumerate(rows):
        d.text((520, 280 + i * 55), f"{k}  {v}", fill=(20, 20, 20), font=font(38))
    d.text((240, 620), "SAMPLE - NOT A REAL LICENSE", fill=(160, 30, 30), font=font(28))
    img.filter(ImageFilter.GaussianBlur(0.8)).save(image_dir / "license_synthetic.jpg", quality=80)


def read(transport, prompt: str, image: bytes) -> dict:
    key = hashlib.sha256(json.dumps(
        {"model": photo_prompts.MODEL, "effort": "low", "prompt": prompt, "image": hashlib.sha256(image).hexdigest()},
        sort_keys=True).encode()).hexdigest()[:24]
    path = CALLS / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text()) | {"cached": True}
    start = time.monotonic()
    try:
        res = transport(photo_prompts.MODEL, prompt, image, "image/jpeg", 60.0)
        record = {"text": res["text"], "input_tokens": res["input_tokens"],
                  "output_tokens": res["output_tokens"], "error": None}
    except Exception as e:  # report, do not cache
        return {"text": None, "error": f"{type(e).__name__}: {e}"[:300], "input_tokens": None,
                "output_tokens": None, "latency_ms": round((time.monotonic() - start) * 1000), "cached": False}
    record["latency_ms"] = round((time.monotonic() - start) * 1000)
    path.write_text(json.dumps(record, indent=1))
    return record | {"cached": False}


def parse(text):
    try:
        t = (text or "").strip()
        if t.startswith("```"):
            t = "\n".join(t.splitlines()[1:]).rstrip("`").strip()
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except ValueError:
        return None


def run_case(transport, image_dir: Path, name: str) -> dict:
    slot, truth = CASES[name]
    image = (image_dir / name.split("#")[0]).read_bytes()
    reads = [read(transport, p, image) for p in photo_prompts.READS[slot]]
    readings = [parse(r["text"]) for r in reads]
    ctx = {"policy": policy(), "answers": ANSWERS, "today": date(2026, 10, 30)}
    decision = photo_check.decide(slot, None if None in readings else readings, ctx)
    return {"name": name, "slot": slot, "truth": truth, "reads": reads, "readings": readings, "decision": decision}


def main() -> None:
    image_dir = Path(sys.argv[1])
    if "--synthetic" in sys.argv:
        make_synthetic(image_dir)
    CALLS.mkdir(parents=True, exist_ok=True)
    transport = openai_transport(api_key())
    names = [n for n in CASES if (image_dir / n.split("#")[0]).exists()]
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda n: run_case(transport, image_dir, n), names))
    (OUT / "results.json").write_text(json.dumps(results, indent=1, default=str))

    price_in, price_out = PRICES_PER_M[photo_prompts.MODEL]
    cost = latency = 0.0
    fresh = 0
    for r in results:
        d = r["decision"]
        print(f"\n## {r['name']}  [{r['slot']}]  truth: {r['truth']}")
        for rd, reading in zip(r["reads"], r["readings"]):
            print(f"   {rd['latency_ms']:>6} ms  in {rd['input_tokens']} out {rd['output_tokens']}"
                  f"{'  (cached)' if rd['cached'] else ''}  {rd['error'] or json.dumps(reading)}")
            if not rd["cached"] and rd["input_tokens"] is not None:
                cost += rd["input_tokens"] * price_in / 1e6 + rd["output_tokens"] * price_out / 1e6
                latency += rd["latency_ms"]
                fresh += 1
        print(f"   -> {d['result']} {d['reason_code'] or ''} | value {d['value']} | hint: {d['hint']}")
    print(f"\n{fresh} new calls, ${cost:.4f}, mean latency {latency / fresh if fresh else 0:.0f} ms")


if __name__ == "__main__":
    main()
