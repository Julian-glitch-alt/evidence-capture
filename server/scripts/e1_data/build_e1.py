"""Build data/e1: chosen public photos (labelled by eye), synthetic plates / VIN labels / odometers with
exact answers, hard variants (messaging-app compression, blur, dark), and two label files:
labels.json (reads, damage, retake; no policy) and claims.json (planted claims against DEMO-AUTO-1001)."""
import io, json, random, shutil, sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

ROOT = Path(sys.argv[1])            # data/e1
RAW, IMG = ROOT / "raw", ROOT / "images"
IMG.mkdir(parents=True, exist_ok=True)
rng = random.Random(57)
BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
VIN_CHARS = "ABCDEFGHJKLMNPRSTUVWXYZ0123456789"
VALUES = {**{str(i): i for i in range(10)}, **dict(zip("ABCDEFGH", range(1, 9))), **dict(zip("JKLMN", range(1, 6))),
          "P": 7, "R": 9, **dict(zip("STUVWXYZ", range(2, 10)))}
WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]
photos, sources = [], json.loads((RAW / "SOURCES.json").read_text())
used_sources = {}


def add(pid, path, slot, expected, origin):
    photos.append({"id": pid, "path": f"images/{path}", "slot": slot, "expected": expected})
    used_sources[path] = origin


def real(name, pid, slot, expected, answers=None):
    shutil.copy(RAW / name, IMG / f"{pid}.jpg")
    add(pid, f"{pid}.jpg", slot, expected, {"kind": "public", **sources[name]})
    if answers:
        photos[-1]["answers"] = answers


def save(im, pid):
    im.convert("RGB").save(IMG / f"{pid}.jpg", quality=90)


def vin(wmi):
    body = wmi + "".join(rng.choice(VIN_CHARS) for _ in range(5)) + "0" + "".join(rng.choice(VIN_CHARS) for _ in range(8))
    assert len(body) == 17
    r = sum(VALUES[c] * w for c, w in zip(body, WEIGHTS)) % 11
    return body[:8] + ("X" if r == 10 else str(r)) + body[9:]


def plate_image(state, text, color=(20, 30, 90)):
    w, h = 1200, 600
    im = Image.new("RGB", (w, h), (235, 236, 240))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle([20, 20, w - 20, h - 20], 40, outline=(60, 60, 60), width=10, fill=(250, 250, 248))
    d.text((w // 2, 95), state, font=ImageFont.truetype(BOLD, 90), fill=(170, 20, 30), anchor="mm")
    size = 250
    while ImageFont.truetype(BOLD, size).getlength(text) > w - 160:  # the whole text must fit inside the plate
        size -= 10
    d.text((w // 2, 340), text, font=ImageFont.truetype(BOLD, size), fill=color, anchor="mm")
    bg = Image.new("RGB", (1600, 1100), (rng.randint(40, 90),) * 3)
    bg.paste(im.rotate(rng.uniform(-6, 6), expand=True, fillcolor=bg.getpixel((0, 0))), (180, 220))
    return bg


def vin_image(value, sticker):
    if sticker:  # door-jamb label
        im = Image.new("RGB", (1500, 520), (245, 245, 240))
        d = ImageDraw.Draw(im)
        d.text((40, 40), "MFD. BY: SAMPLE MOTORS  05/23", font=ImageFont.truetype(BOLD, 40), fill="black")
        d.text((40, 110), "VEHICLE IDENTIFICATION NUMBER", font=ImageFont.truetype(BOLD, 38), fill="black")
        d.text((40, 180), value, font=ImageFont.truetype(MONO, 96), fill="black")
        for x in range(40, 1440, 9):
            d.rectangle([x, 330, x + rng.choice((2, 4, 6)), 470], fill="black")
    else:  # windshield plate
        im = Image.new("RGB", (1500, 360), (30, 32, 36))
        d = ImageDraw.Draw(im)
        d.text((750, 180), value, font=ImageFont.truetype(MONO, 100), fill=(205, 208, 212), anchor="mm")
    bg = Image.new("RGB", (1700, 900), (rng.randint(100, 160),) * 3)
    bg.paste(im.rotate(rng.uniform(-3, 3), expand=True, fillcolor=bg.getpixel((0, 0))), (90, 200))
    return bg


def odometer_image(miles, trip):
    im = Image.new("RGB", (1400, 800), (12, 14, 18))
    d = ImageDraw.Draw(im)
    d.ellipse([60, 60, 740, 740], outline=(200, 200, 200), width=8)
    d.rounded_rectangle([780, 250, 1340, 560], 24, fill=(25, 40, 30))
    d.text((800, 280), f"TRIP A  {trip:.1f}", font=ImageFont.truetype(MONO, 46), fill=(150, 230, 170))
    d.text((800, 380), f"ODO {miles:>6d} mi", font=ImageFont.truetype(MONO, 70), fill=(150, 230, 170))
    for i, s in enumerate(range(0, 140, 20)):
        d.text((400 + 280 * __import__("math").cos(3.6 - i * 0.55), 400 - 280 * __import__("math").sin(3.6 - i * 0.55)),
               str(s), font=ImageFont.truetype(BOLD, 44), fill="white", anchor="mm")
    return im


def variant(src_pid, pid, kind, expected):
    im = Image.open(IMG / f"{src_pid}.jpg").convert("RGB")
    if kind == "msg":  # like a photo forwarded twice through a messaging app
        for q in (55, 50):
            im.thumbnail((1280, 1280))
            buf = io.BytesIO(); im.save(buf, "JPEG", quality=q); im = Image.open(io.BytesIO(buf.getvalue())).convert("RGB")
    elif kind == "blur":
        im = im.filter(ImageFilter.GaussianBlur(10))
    elif kind == "dark":
        im = ImageEnhance.Brightness(im).enhance(0.1)
    im.save(IMG / f"{pid}.jpg", quality=90)
    src = next(p for p in photos if p["id"] == src_pid)
    add(pid, f"{pid}.jpg", src["slot"], expected, {"kind": f"variant:{kind}", "of": src_pid})
    if "answers" in src:
        photos[-1]["answers"] = src["answers"]


# --- public photos, labelled by eye (Claude, 3 Nov) ---
for name, text in [("plca00", "10851"), ("plca01", "53597"), ("plca02", "57164"), ("plca03", "16861"),
                   ("plca06", "13802"), ("plca11", "21800"), ("plca12", "188998"), ("plca13", "478701"),
                   ("plmod00", "MMY3163"), ("plmod01", "LHD8448"), ("plmod02", "977QIQ")]:
    real(f"{name}.jpg", f"plate_{name}", "plate", {"plate": text, "retake": False})
for name in ("plmod04", "plmod05", "plmod06", "plmod10", "plmod07"):  # plate blurred by the uploader / no plate
    real(f"{name}.jpg", f"plate_none_{name}", "plate", {"retake": True})
real("odo00.jpg", "odo_odo00", "odometer", {"odometer": round(55055 * 0.621371), "retake": False})
real("odo10.jpg", "odo_odo10", "odometer", {"odometer": 34763, "retake": False})
for name in ("odo02", "odo08", "odo14"):  # not a car's odometer
    real(f"{name}.jpg", f"odo_none_{name}", "odometer", {"retake": True})
D = lambda area, typ, sev: {"area": area, "type": typ, "severity": sev, "age": "new"}
for name, slot, dmg in [
    ("dmg02", "damage_closeup", [D("Windshield", "GLASS_DAMAGE", "severe"), D("Interior", "INTERIOR_DAMAGE", "severe")]),
    ("glass00", "damage_closeup", [D("Windshield", "GLASS_DAMAGE", "moderate")]),
    ("glass02", "damage_closeup", [D("Windshield", "GLASS_DAMAGE", "severe")]),
    ("dmg01", "damage_wide", [D("Right side", "FIRE_DAMAGE", "severe")]),
    ("dmg06", "damage_wide", [D("Front right", "BODY_PANEL_DENT", "severe"), D("Right side", "BODY_PANEL_DENT", "moderate")]),
    ("dmg09", "damage_wide", [D("Front bumper", "BUMPER_DAMAGE", "moderate")]),
    ("dmg11", "damage_wide", [D("Front right", "BODY_PANEL_DENT", "severe"), D("Front bumper", "BUMPER_DAMAGE", "severe")]),
    ("dmg18", "damage_wide", [D("Windshield", "GLASS_DAMAGE", "severe"), D("Hood", "BODY_PANEL_DENT", "moderate")]),
    ("dmg19", "damage_wide", [D("Windshield", "GLASS_DAMAGE", "severe"), D("Hood", "BODY_PANEL_DENT", "moderate")]),
    ("dmg20", "damage_wide", [D("Roof", "BODY_PANEL_DENT", "severe"), D("Windshield", "GLASS_DAMAGE", "severe")]),
    ("glass01", "damage_wide", [D("Windshield", "GLASS_DAMAGE", "severe")]),
    ("bump00", "damage_wide", [D("Front bumper", "BUMPER_DAMAGE", "severe")]),
]:
    real(f"{name}.jpg", f"dmg_{name}", slot, {"damages": dmg, "retake": False},
         {"damaged_areas": sorted({d["area"] for d in dmg})})
for name, slot in [("accord00", "damage_wide"), ("camry00", "damage_wide"), ("camry01", "damage_wide"),
                   ("camry03", "damage_wide"), ("camry04", "damage_wide"), ("camry05", "damage_wide"),
                   ("dent02", "damage_wide"), ("accord03", "damage_closeup"), ("accord04", "damage_closeup")]:
    real(f"{name}.jpg", f"clean_{name}", slot, {"damages": [], "retake": True})  # claim lists Front bumper
for name, slot in [("hail00", "damage_closeup"), ("hail03", "damage_wide")]:  # hail-damaged apples
    real(f"{name}.jpg", f"apple_{name}", slot, {"retake": True})

# --- synthetic, exact answers ---
for i, (state, text) in enumerate([("CALIFORNIA", "8KTR512"), ("TEXAS", "PJW-4718"), ("FLORIDA", "QZD R41"),
                                   ("NEW YORK", "KHL 2209"), ("ARIZONA", "CHN4027"), ("WASHINGTON", "BWX9184")]):
    save(plate_image(state, text), f"syn_plate{i}")
    add(f"syn_plate{i}", f"syn_plate{i}.jpg", "plate", {"plate": text.replace("-", "").replace(" ", ""), "retake": False},
        {"kind": "synthetic"})
for i, wmi in enumerate(["1HG", "5YJ", "1FT", "JTD", "2T1", "3VW"]):
    v = vin(wmi)
    save(vin_image(v, sticker=i % 2 == 0), f"syn_vin{i}")
    add(f"syn_vin{i}", f"syn_vin{i}.jpg", "vin_photo", {"vin": v, "retake": False}, {"kind": "synthetic"})
    photos[-1]["answers"] = {"vin": v}  # the claimant typed this VIN; the check compares the photo with it
for i, (miles, trip) in enumerate([(48213, 312.4), (102776, 18.9), (7310, 7310.0), (65002, 245.7)]):
    save(odometer_image(miles, trip), f"syn_odo{i}")
    add(f"syn_odo{i}", f"syn_odo{i}.jpg", "odometer", {"odometer": miles, "retake": False}, {"kind": "synthetic"})

# --- hard variants ---
for src in ("plate_plmod00", "plate_plca12", "odo_odo10", "dmg_dmg18", "dmg_glass02", "syn_plate0"):
    exp = {k: v for k, v in next(p["expected"] for p in photos if p["id"] == src).items()}
    variant(src, f"{src}_msg", "msg", exp)
for src in ("plate_plmod01", "odo_odo10", "dmg_dmg06"):
    variant(src, f"{src}_blur", "blur", {"retake": True})
for src in ("plate_plmod02", "syn_vin1", "clean_camry04"):
    variant(src, f"{src}_dark", "dark", {"retake": True})

answers = {"loss_type": "vehicle_damage", "loss_date": "2026-11-01", "loss_time": "17:30", "loss_city": "San Jose",
           "loss_state": "CA", "cause": "Hit an object", "description": "Hit a concrete post with the front of the car; the bumper cover came off.",
           "police_contacted": "No", "vin": "1HGCV1F39LA526018", "drivable": "Yes", "driver_is_holder": "Yes",
           "damaged_areas": ["Front bumper"], "prior_damage": "No", "other_vehicle": "No", "injuries": "No",
           "full_name": "Jordan Avery"}
json.dump({"version": 1, "_note": "T57 E1: public photos labelled by eye, synthetic photos with exact answers, hard variants. No policy, so reads are scored against the labels only.",
           "policy": None, "today": "2026-11-03", "answers": answers, "photos": photos, "claims": []},
          open(ROOT / "labels.json", "w"), indent=1)

# --- E2: planted claims against the demo policy (Honda Accord, plate DEMO101, VIN 1HGCV1F39LA526018) ---
save(plate_image("CALIFORNIA", "DEMO101"), "e2_plate_policy")
save(vin_image("1HGCV1F39LA526018", sticker=True), "e2_vin_policy")
e2_photos = [
    {"id": "e2_plate", "path": "images/e2_plate_policy.jpg", "slot": "plate", "expected": {"plate": "DEMO101"}},
    {"id": "e2_vin", "path": "images/e2_vin_policy.jpg", "slot": "vin_photo", "expected": {"vin": "1HGCV1F39LA526018"}},
    {"id": "e2_other_plate", "path": "images/syn_plate1.jpg", "slot": "plate", "expected": {"plate": "PJW4718"}},
    {"id": "e2_other_vin", "path": "images/syn_vin0.jpg", "slot": "vin_photo",
     "expected": {"vin": next(p["expected"]["vin"] for p in photos if p["id"] == "syn_vin0")}},
    {"id": "e2_front", "path": "images/dmg_bump00.jpg", "slot": "damage_wide",
     "expected": {"damages": [D("Front bumper", "BUMPER_DAMAGE", "severe")]}},
    {"id": "e2_glass_close", "path": "images/dmg_glass02.jpg", "slot": "damage_closeup",
     "expected": {"damages": [D("Windshield", "GLASS_DAMAGE", "severe")]}},
    {"id": "e2_glass_wide", "path": "images/dmg_glass01.jpg", "slot": "damage_wide",
     "expected": {"damages": [D("Windshield", "GLASS_DAMAGE", "severe")]}},
    {"id": "e2_clean", "path": "images/clean_camry04.jpg", "slot": "damage_wide", "expected": {"damages": []}},
]
glass = {"description": "A rock hit the windshield on the highway and it cracked.", "damaged_areas": ["Windshield"]}
claims = [
    {"id": "c1", "photos": ["e2_plate", "e2_vin", "e2_front"], "note": "Clean: front bumper story, front bumper photo.",
     "expected_flags": []},
    {"id": "c2", "photos": ["e2_other_plate", "e2_vin", "e2_front"], "note": "Planted: the plate photo is another car (Texas PJW-4718).",
     "expected_flags": ["PLATE_MISMATCH"]},
    {"id": "c3", "photos": ["e2_plate", "e2_vin", "e2_front"],
     "note": "Planted: the story says rear bumper, the photo shows the front bumper.",
     "answers": {"description": "Backed into a bollard in a parking lot.", "damaged_areas": ["Rear bumper"]},
     "expected_flags": ["DAMAGE_AREA_MISMATCH", "DAMAGE_OUTSIDE_STATEMENT", "STORY_INCONSISTENT"]},
    {"id": "c4", "photos": ["e2_plate", "e2_vin", "e2_glass_close", "e2_glass_wide"], "note": "Clean: a rock cracked the windshield.",
     "answers": glass, "expected_flags": []},
    {"id": "c5", "photos": ["e2_plate", "e2_vin", "e2_clean"], "note": "Planted: front bumper claimed, the photo shows an undamaged car.",
     "expected_flags": ["DAMAGE_PART_NOT_FOUND"]},
    {"id": "c6", "photos": ["e2_plate", "e2_other_vin", "e2_front"], "note": "Planted: the VIN photo is another vehicle's VIN.",
     "expected_flags": ["VIN_MISMATCH"]},
]
json.dump({"version": 1, "_note": "T57 E2: planted claims against the demo policy.", "policy": "DEMO-AUTO-1001",
           "today": "2026-11-03", "answers": answers, "photos": e2_photos, "claims": claims},
          open(ROOT / "claims.json", "w"), indent=1)
json.dump(used_sources, open(ROOT / "SOURCES.json", "w"), indent=1)
print(len(photos), "photos in labels.json;", len(claims), "claims in claims.json")
