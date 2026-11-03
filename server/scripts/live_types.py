"""One live claim per T43 claim type on a deployed server; prints what verification found.

Usage (needs ADMIN_PASSWORD from .env): uv run --project server python server/scripts/live_types.py BASE_URL data/t34_probe/images data/t43_live OUT.json
Sends public or synthetic photos only (free Meta credits: never real plates or VINs)."""
import concurrent.futures as cf
import json
import os
import sys
import time
from pathlib import Path

import httpx

BASE, PROBE, T43 = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
ID_SHOTS = {"plate": PROBE / "plate_synthetic_demo101.jpg", "vin_photo": PROBE / "vin_synthetic_ok.jpg",
            "odometer": PROBE / "odo_malibu.jpg", "driver_license": PROBE / "license_synthetic.jpg",
            "keys": PROBE / "license_synthetic.jpg", "documents": PROBE / "license_synthetic.jpg"}
COMMON = {"loss_date": "2026-10-30", "loss_city": "San Jose", "loss_state": "CA", "drivable": "Yes",
          "driver_is_holder": "Yes", "prior_damage": "No", "injuries": "No", "full_name": "Jordan Avery", "agree": True}
CASES = {
    "collision_vehicle": ({"description": "A car pulled out of a driveway and hit my front left side.", "police_contacted": "No",
                           "damaged_areas": ["Front left", "Left side"], "other_driver_name": "Pat Lee"},
                          "dmg_close_mirror.jpg", "dmg_wide_dodge.jpg"),
    "hit_and_run": ({"car_was": "Parked", "description": "Someone hit my parked car and drove off.", "police_contacted": "Yes",
                     "police_agency": "San Jose Police Department", "damaged_areas": ["Front left", "Left side"]},
                    "dmg_close_mirror.jpg", "dmg_wide_dodge.jpg"),
    "single_vehicle": ({"hit_what": "A pole or sign", "description": "I turned into a pole in a parking lot with the front.",
                        "police_contacted": "No", "damaged_areas": ["Front bumper", "Hood"]}, "dmg_close_hitrun.jpg", "dmg_wide_dodge.jpg"),
    "animal": ({"animal": "Deer", "description": "A deer ran across the road and hit the front of the car.", "police_contacted": "No",
                "damaged_areas": ["Front bumper", "Hood"]}, "dmg_close_hitrun.jpg", "dmg_wide_dodge.jpg"),
    "vandalism": ({"vandalism_kind": ["Scratched or keyed"], "description": "Someone keyed the side of my car overnight.",
                   "police_contacted": "Not yet", "damaged_areas": ["Left side"]}, "t43:vandal.jpg", "t43:vandal.jpg"),
    "glass": ({"glass_which": ["Windshield"], "glass_kind": "A crack", "glass_cause": "A rock or road debris",
               "description": "A rock hit the windshield on the highway and it cracked.", "damaged_areas": ["Windshield"]},
              "t43:glass.jpg", "t43:glass.jpg"),
    "glass_mismatch": ({"glass_which": ["Windshield"], "glass_kind": "A crack", "glass_cause": "A rock or road debris",
                        "description": "A rock cracked the windshield.", "damaged_areas": ["Windshield"]},
                       "dmg_close_hitrun.jpg", "dmg_wide_dodge.jpg"),
    "weather": ({"weather_kind": "Hail", "description": "A hail storm hit the car while it was parked outside.",
                 "damaged_areas": ["Roof", "Hood"]}, "t43:hail.jpg", "t43:hail.jpg"),
    "weather_flood": ({"weather_kind": "Flood or high water", "water_level": "Up to the seats",
                       "description": "The street flooded overnight and water came into the car.", "damaged_areas": ["Interior"]},
                      "t43:flood.jpg", "t43:flood.jpg"),
    "parts_theft": ({"parts_taken": ["Catalytic converter"], "description": "Someone cut the catalytic converter overnight.",
                     "police_contacted": "No", "damaged_areas": ["Underneath"]}, "dmg_close_hitrun.jpg", "dmg_wide_dodge.jpg"),
    "fire": ({"fire_start": "Engine", "fire_dept": "Yes", "description": "The engine caught fire while parked.", "police_contacted": "No",
              "damaged_areas": ["Hood", "Front bumper"]}, "t43:fire.jpg", "t43:fire.jpg"),
    "vehicle_theft": ({"last_seen_place": "Driveway at home", "keys_count": "2", "keys_all": "No, some are missing",
                       "car_locked": "Yes", "ownership": "I own it outright", "description": "My car was gone in the morning.",
                       "police_contacted": "Yes", "police_agency": "San Jose Police Department"}, None, None),
}


def img(name):
    return (T43 / name[4:]) if name.startswith("t43:") else (PROBE / name)


def run(case):
    extra, close, wide = CASES[case]
    loss_type = case.split("_mismatch")[0].replace("weather_flood", "weather")
    adm = httpx.Client(base_url=BASE, timeout=120)
    adm.post("/api/admin/login", json={"email": os.environ.get("ADMIN_EMAIL", "admin"), "password": os.environ["ADMIN_PASSWORD"]})
    claim = adm.post("/api/admin/claims", json={"policy_number": "DEMO-AUTO-1001"}).json()
    token = claim["token"]
    ph = httpx.Client(base_url=BASE, timeout=120)
    sess = ph.post(f"/api/c/{token}/session").json()
    layout = ph.put(f"/api/c/{token}/draft", json={"answers": {"loss_type": loss_type, **COMMON, **extra}}).json()["layout"]
    files = {**ID_SHOTS, **({"damage_closeup": img(close), "damage_wide": img(wide)} if close else {})}
    media = {"video": ("x.mp4", b"\x00\x00\x00\x18ftypmp42live", "video/mp4"), "signature": ("s.png", b"\x89PNG\r\n\x1a\nlive", "image/png")}

    def up(slot):
        n = ph.post(f"/api/c/{token}/nonce", json={"session_id": sess["session_id"], "slot_id": slot["id"]}).json()["nonce"]
        f = (files[slot["id"]].name, files[slot["id"]].read_bytes(), "image/jpeg") if slot["id"] in files else media.get(slot["kind"], ("x.jpg", b"\xff\xd8\xff\xe0" + slot["id"].encode(), "image/jpeg"))
        return ph.post(f"/api/c/{token}/items", data={"step_id": slot["id"], "session_id": sess["session_id"], "nonce": n}, files={"file": f})

    for section in layout["sections"]:
        for slot in section["slots"]:
            if slot["required"]:
                up(slot)
    missing = layout.get("missing")
    for attempt in range(4):
        time.sleep(15)
        r = ph.post(f"/api/c/{token}/submit", json={})
        if r.status_code == 200:
            break
        # a retake was asked: shoot the same photo again (the third shot is flagged, not refused)
        st = ph.get(f"/api/c/{token}").json()
        for c in st.get("photo_checks") or []:
            if c.get("result") == "retake":
                slot = next(s for sec in st["layout"]["sections"] for s in sec["slots"] if s["id"] == c["slot_id"])
                up(slot)
    else:
        return case, {"submit_error": r.text[:300], "missing": missing}
    t0 = time.time()
    while time.time() - t0 < 240:
        d = adm.get(f"/api/admin/claims/{claim['id']}").json()
        if (d.get("proposal") or {}).get("status") == "ready" and d.get("report"):
            break
        time.sleep(8)
    p = d.get("proposal") or {}
    fit = next((c for c in d.get("checks") or [] if c["check_id"] == "claimtype.fit"), None)
    story = next((c for c in d.get("checks") or [] if c["check_id"] == "consistency.story"), None)
    return case, {
        "claim": claim["id"], "type": (d.get("claim_type") or {}).get("label"),
        "damages": [f"{x.get('area')}:{x.get('damage_type')}" for x in (d.get("damage") or {}).get("merged") or []],
        "fit": fit and (fit["result"], fit["reason"]), "story": story and (story["result"], story["reason"][:120]),
        "lane": p.get("lane"), "flags": [f["reason_code"] for f in p.get("flags") or []],
        "evidence": (p.get("evidence") or {}).get("band"), "to_verify": d.get("to_verify"),
        "summary": (d.get("ai_summary") or {}).get("status"), "calls": (d.get("ai_usage") or {}).get("calls"),
        "cost": (d.get("ai_usage") or {}).get("cost_usd"),
    }


with cf.ThreadPoolExecutor(4) as pool:
    results = dict(pool.map(run, CASES))
Path(sys.argv[4]).write_text(json.dumps(results, indent=1))
for k, v in results.items():
    print(f"\n## {k}")
    for kk, vv in v.items():
        print(f"  {kk}: {vv}")
