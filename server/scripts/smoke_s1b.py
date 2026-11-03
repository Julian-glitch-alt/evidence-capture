"""End-to-end smoke test against a running server (local or the public Modal URL).

    uv run python scripts/smoke_s1b.py --base-url https://...modal.run
    uv run python scripts/smoke_s1b.py --base-url URL --expect-claim CLM-...   # after a restart

Needs ADMIN_PASSWORD in the environment (set -a && . ../.env).

Prints one PASS / FAIL line per step and exits non-zero on the first failure.
Grows with each S1b task; every step drives the public HTTP API, as a client would.
"""

import argparse
import io
import os
import zipfile
import sys
import time

import httpx
from PIL import Image


def picture(shade: int, fmt: str = "JPEG") -> bytes:
    """A real, plain gray image: the live model reads it (fake bytes made Meta answer 400 and
    filled System health with errors). A different shade gives different bytes."""
    buf = io.BytesIO()
    Image.new("RGB", (320, 240), (shade, shade, shade)).save(buf, fmt)
    return buf.getvalue()


class Smoke:
    def __init__(self, base_url: str):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=60, follow_redirects=True)

    def step(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))
        if not ok:
            sys.exit(1)


def run(base_url: str, expect_claim: str | None) -> None:
    s = Smoke(base_url)

    t0 = time.monotonic()
    r = s.http.get("/api/health")
    s.step("health", r.status_code == 200, f"{(time.monotonic() - t0) * 1000:.0f} ms incl. any cold start")

    r = s.http.get("/api/admin/claims")
    s.step("admin API refuses without login", r.status_code == 401, str(r.status_code))
    r = s.http.post("/api/admin/login", json={"email": os.environ.get("ADMIN_EMAIL", "admin"), "password": "wrong-password"})
    s.step("wrong password refused", r.status_code == 401, str(r.status_code))
    r = s.http.post("/api/admin/login", json={"email": os.environ.get("ADMIN_EMAIL", "admin"), "password": os.environ["ADMIN_PASSWORD"]})
    s.step("adjuster login", r.status_code == 200, str(r.status_code))

    if expect_claim:
        r = s.http.get(f"/api/admin/claims/{expect_claim}")
        s.step("claim survives restart", r.status_code == 200, f"{expect_claim} status={r.json().get('status')}")
        pkg = s.http.get(f"/api/admin/claims/{expect_claim}/package")
        s.step("sealed package still downloads after restart", pkg.status_code == 200, f"{len(pkg.content)} bytes")
        r = s.http.post("/api/verify", files={"file": ("p.zip", pkg.content, "application/zip")}).json()
        s.step("files in storage still match the seal", r["valid"] is True, str(r["problems"])[:120])
        return

    test_key = os.environ.get("SMOKE_API_KEY", "").strip()
    if test_key:  # a test API key: the claim lands in Test claims and never counts on Today (T54)
        r = s.http.post("/api/v1/capture-requests", headers={"Authorization": f"Bearer {test_key}"}, json={
            "insurer_claim_number": f"SMOKE-{int(time.time())}", "policy_number": "DEMO-AUTO-1001",
            "callback_url": str(s.http.base_url).rstrip("/") + "/api/demo-insurer/webhook"})
        ok = r.status_code == 201 and r.json().get("test_mode") is True
        s.step("open a test claim against a known policy", ok, r.text[:120])
        claim_id = r.json()["case_id"]
    else:
        print("note  SMOKE_API_KEY not set: this claim is a live one and shows on Today")
        r = s.http.post("/api/admin/claims", json={"policy_number": "DEMO-AUTO-1001"})
        s.step("open claim against a known policy", r.status_code == 200, r.text[:120])
        claim_id = r.json()["id"]

    detail = s.http.get(f"/api/admin/claims/{claim_id}").json()
    s.step("policy snapshot verified", detail["policy"]["verified"] is True)
    s.step("claim ID uses policy-state date", claim_id.startswith("CLM-2"), claim_id)

    r = s.http.get("/")
    s.step("frontend served", r.status_code == 200 and "<div id=\"root\">" in r.text)

    token = s.http.get(f"/api/admin/claims/{claim_id}").json()["token"]
    r = s.http.put(f"/api/c/{token}/draft", json={"answers": {"loss_type": "vehicle_damage"}})
    sections = [x["id"] for x in r.json()["layout"]["sections"]] if r.status_code == 200 else []
    s.step("loss notice has the 9 sections", len(sections) == 9, ", ".join(sections))
    phone = httpx.Client(base_url=s.http.base_url, timeout=60)  # no adjuster cookie
    jpeg = picture(128)

    r = phone.post(f"/api/c/{token}/items", data={"step_id": "plate"}, files={"file": ("x.jpg", jpeg, "image/jpeg")})
    s.step("upload without a capture session refused", r.status_code == 403, str(r.status_code))

    session = phone.post(f"/api/c/{token}/session").json()
    s.step("capture session started", len(session.get("code", "")) == 5, f"code {session.get('code')}")
    nonce = phone.post(
        f"/api/c/{token}/nonce", json={"session_id": session["session_id"], "slot_id": "plate"}
    ).json()["nonce"]
    form = {"step_id": "plate", "session_id": session["session_id"], "nonce": nonce}
    r = phone.post(f"/api/c/{token}/items", data=form, files={"file": ("x.jpg", jpeg, "image/jpeg")})
    s.step("upload with session + nonce accepted", r.status_code == 200, r.text[:80])
    first_id = r.json()["id"]
    r = phone.post(f"/api/c/{token}/items", data=form, files={"file": ("x.jpg", jpeg, "image/jpeg")})
    s.step("retry of the same upload returns the stored item", r.status_code == 200 and r.json()["id"] == first_id)
    r = phone.post(f"/api/c/{token}/items", data=form, files={"file": ("x.jpg", picture(129), "image/jpeg")})
    s.step("used nonce refused for a different file", r.status_code == 403, str(r.status_code))

    # Photos redirect to the file store; the page's CSP must allow that host or every photo
    # renders blank (live claim page, 2 Nov). Local runs serve files directly.
    r = s.http.get(f"/api/admin/items/{first_id}/file", follow_redirects=False)
    target = r.headers.get("location", "") if r.status_code in (302, 307) else ""
    origin = "/".join(target.split("/")[:3]) if target else ""
    csp = s.http.get("/").headers.get("content-security-policy", "")
    img = next((d.split()[1:] for d in csp.split(";") if d.split()[:1] == ["img-src"]), [])
    s.step("page may show stored photos", not origin or origin in img, "via the file store" if origin else "served directly")

    detail = s.http.get(f"/api/admin/claims/{claim_id}").json()
    s.step("adjuster sees the same session code", detail["sessions"][-1]["code"] == session["code"])
    types = [e["type"] for e in detail["events"]]
    s.step("timeline records capture", types[:4] == ["claim.opened", "claim.capturing", "capture.session_started", "item.received"], ", ".join(types))

    # Complete the claim through the API as the phone would, then seal and verify it.
    answers = {
        "loss_type": "vehicle_damage", "loss_date": "2026-10-27", "loss_city": "San Jose", "loss_state": "CA",
        "cause": "Hit an object", "description": "Smoke test: backed into a bollard.", "police_contacted": "No",
        "vin": "1HGCV1F39LA526018", "drivable": "Yes", "driver_is_holder": "Yes", "damaged_areas": ["Rear bumper"],
        "prior_damage": "No", "other_vehicle": "No", "injuries": "No", "full_name": "Jordan Avery", "agree": True,
    }
    layout = phone.put(f"/api/c/{token}/draft", json={"answers": answers}).json()["layout"]
    media = {"video": ("x.mp4", b"\x00\x00\x00\x18ftypmp42smoke", "video/mp4"), "signature": ("s.png", picture(255, "PNG"), "image/png")}
    shades = iter(range(140, 250, 5))
    for section in layout["sections"]:
        for slot in section["slots"]:
            if slot["required"] and slot["count"] == 0:
                n = phone.post(f"/api/c/{token}/nonce", json={"session_id": session["session_id"], "slot_id": slot["id"]}).json()["nonce"]
                name, data, mime = media.get(slot["kind"], ("x.jpg", picture(next(shades)), "image/jpeg"))
                r = phone.post(f"/api/c/{token}/items", data={"step_id": slot["id"], "session_id": session["session_id"], "nonce": n}, files={"file": (name, data, mime)})
                if r.status_code != 200:
                    s.step(f"upload {slot['id']}", False, r.text[:100])
    r = phone.post(f"/api/c/{token}/submit", json={})
    s.step("claim submitted", r.status_code == 200, r.text[:100])
    seal = s.http.get(f"/api/admin/claims/{claim_id}").json()["seal"]
    s.step("claim sealed on submit", bool(seal), f"key {seal and seal['key_id']}")
    pkg = s.http.get(f"/api/admin/claims/{claim_id}/package")
    s.step("sealed package downloads", pkg.status_code == 200, f"{len(pkg.content)} bytes")
    r = phone.post("/api/verify", files={"file": ("p.zip", pkg.content, "application/zip")}).json()
    s.step("untouched package verifies", r["valid"] is True, str(r["problems"])[:120])
    z = zipfile.ZipFile(io.BytesIO(pkg.content))
    files = {n: z.read(n) for n in z.namelist()}
    victim = next(n for n in files if n.startswith("files/plate-"))
    files[victim] = files[victim] + b"tampered"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as out:
        for n, d in files.items():
            out.writestr(n, d)
    r = phone.post("/api/verify", files={"file": ("p.zip", buf.getvalue(), "application/zip")}).json()
    s.step("tampered package is caught and named", r["valid"] is False and any(victim in p for p in r["problems"]), str(r["problems"])[:120])

    print(f"\nclaim for the restart check: {claim_id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--expect-claim")
    args = parser.parse_args()
    run(args.base_url, args.expect_claim)
