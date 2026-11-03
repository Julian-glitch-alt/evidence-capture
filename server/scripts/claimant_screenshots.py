"""Screenshots of the claimant's capture screens at 390x844 (track 4).

Seeds claims through the API with the fake model from tests/conftest.py (no Meta
call, no secrets), serves the built PWA, and drives it in Chrome with a fake camera.

    cd app && npm run build
    cd server && TEST_DATABASE_URL=postgresql://... \
        uv run --with playwright python scripts/claimant_screenshots.py --out ../docs/explorations/claimant-capture/after

TEST_DATABASE_URL must name a test database: it is emptied first. Uses the system
Chrome (`--channel chrome`, the default) or Playwright's bundled Chromium (`--channel chromium`).
"""

import argparse
import io
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server" / "tests"))

import psycopg  # noqa: E402
import uvicorn  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402
from playwright.sync_api import Page, sync_playwright  # noqa: E402

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, MP4, FakeModel, Phone  # noqa: E402
from evidence_server.blobstore import LocalBlobStore  # noqa: E402
from evidence_server.db import Database  # noqa: E402
from evidence_server.main import create_app  # noqa: E402
from pg import TEST_DATABASE_URL, reset  # noqa: E402

VIEWPORT = {"width": 390, "height": 844}
PHONE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"
            " (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1")
ANSWERS = {
    "loss_type": "vehicle_damage",
    "loss_date": "2026-10-27",
    "loss_time": "17:30",
    "loss_street": "Westfield Valley Fair, level 2",
    "loss_city": "San Jose",
    "loss_state": "CA",
    "cause": "Hit while parked",
    "other_driver_left": "Yes",
    "description": "I came back to my parked car and found the rear bumper pushed in. Nobody left a note.",
    "police_contacted": "Yes",
    "police_agency": "San Jose Police Department",
    "police_report_no": "24-118803",
    "vin": "1HGCV1F39LA526018",
    "odometer_reading": "56947",
    "drivable": "Yes",
    "driver_is_holder": "Yes",
    "damaged_areas": ["Rear bumper"],
    "prior_damage": "No",
    "other_vehicle": "Yes",
    "other_plate": "8ABC123",
    "other_vehicle_desc": "White pickup truck",
    "injuries": "No",
}
SIGNED = {"full_name": "Jordan Avery", "agree": True}


def photo(label: str, color: tuple) -> bytes:
    """A plain JPEG with a label, so thumbnails render (the fake model reads nothing from it)."""
    img = Image.new("RGB", (640, 480), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle((40, 160, 600, 320), outline=(255, 255, 255), width=6)
    draw.text((60, 220), label, fill=(255, 255, 255))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=80)
    return out.getvalue()


def signature_png() -> bytes:
    img = Image.new("RGB", (600, 200), (255, 255, 255))
    ImageDraw.Draw(img).line([(40, 150), (160, 60), (260, 140), (380, 50), (540, 130)], fill=(20, 30, 40), width=6)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


SHOTS = {
    "plate": (photo("PLATE", (70, 90, 110)), "image/jpeg"),
    "vin_photo": (photo("VIN", (90, 80, 70)), "image/jpeg"),
    "odometer": (photo("ODOMETER 56947", (30, 30, 40)), "image/jpeg"),
    "driver_license": (photo("LICENSE", (120, 110, 90)), "image/jpeg"),
    "walkaround": (MP4, "video/mp4"),
    "damage_closeup": (photo("DAMAGE CLOSE-UP", (130, 60, 50)), "image/jpeg"),
    "damage_wide": (photo("DAMAGE WIDE", (110, 70, 60)), "image/jpeg"),
    "signature": (signature_png(), "image/png"),
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve(app, port: int) -> uvicorn.Server:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            return server
        time.sleep(0.05)
    raise RuntimeError("server did not start")


class Seeder:
    """Claims in a known state, made through the same API the phone and the console use."""

    def __init__(self, api: TestClient):
        self.api = api
        self.model: FakeModel = api.app.state.store.meta.transport
        r = api.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
        r.raise_for_status()

    def claim(self, answers: dict | None = None, shots: tuple = ()) -> dict:
        claim = self.api.post("/api/admin/claims", json={"policy_number": "DEMO-AUTO-1001"}).json()
        if answers:
            self.api.put(f"/api/c/{claim['token']}/draft", json={"answers": answers}).raise_for_status()
        if shots:
            phone = Phone(self.api, claim["token"])
            for slot in shots:
                data, mime = SHOTS[slot]
                r = phone.upload(slot, data, mime)
                r.raise_for_status()
        return claim

    def sent_back(self, slots: list[str]) -> dict:
        claim = self.claim(ANSWERS | SIGNED, tuple(SHOTS))
        self.api.post(f"/api/c/{claim['token']}/submit", json={}).raise_for_status()
        for _ in range(100):  # the background reading and report run after the response
            d = self.api.get(f"/api/admin/claims/{claim['id']}").json()
            if (d.get("proposal") or {}).get("status") == "ready":
                break
            time.sleep(0.1)
        r = self.api.post(f"/api/admin/claims/{claim['id']}/decision",
                          json={"action": "send_back", "reason": "The wide shot is too dark.", "slots": slots})
        r.raise_for_status()
        return claim


def shoot(page: Page, out: Path, name: str, full: bool = False) -> None:
    page.wait_for_timeout(400)
    page.screenshot(path=str(out / f"{name}.png"), full_page=full)
    print("  saved", name)


def start(page: Page, base: str, token: str) -> None:
    page.goto(f"{base}/c/{token}")
    page.get_by_role("checkbox").check()
    page.get_by_role("button", name="Start").click()


def run(out: Path, base: str, seeder: Seeder, channel: str) -> None:
    out.mkdir(parents=True, exist_ok=True)
    empty = seeder.claim()
    partial = seeder.claim(ANSWERS, ("plate", "vin_photo", "odometer"))
    ready = seeder.claim(ANSWERS, tuple(s for s in SHOTS if s != "signature"))
    # A plate shot the photo check does not accept: the claimant sees a plain retake hint.
    SHOTS["plate"] = (photo("NOT A PLATE", (60, 60, 60)), "image/jpeg")
    seeder.model.set_image(SHOTS["plate"][0], {"is_plate": False, "text": "", "state": "", "unsure": False, "problem": "none"})
    retake = seeder.claim(ANSWERS, tuple(s for s in SHOTS if s != "signature"))
    round2 = seeder.sent_back(["damage_wide", "odometer"])
    with sync_playwright() as p:
        browser = p.chromium.launch(
            channel=None if channel == "chromium" else channel,
            args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
        )
        ctx = browser.new_context(viewport=VIEWPORT, device_scale_factor=2, is_mobile=True, has_touch=True,
                                  user_agent=PHONE_UA, permissions=["camera"])
        page = ctx.new_page()

        page.goto(f"{base}/c/{empty['token']}")
        shoot(page, out, "0-intro")
        page.get_by_role("checkbox").check()
        page.get_by_role("button", name="Start").click()
        page.get_by_text("What happened?").first.wait_for()
        shoot(page, out, "1-loss-type")

        start(page, base, partial["token"])
        page.get_by_role("button", name="Continue").wait_for()
        shoot(page, out, "2-form")
        shoot(page, out, "2-form-full", full=True)
        page.get_by_role("button", name="Continue").click()
        shoot(page, out, "3-photos")
        shoot(page, out, "3-photos-full", full=True)
        for slot, title in [("plate", "License plate"), ("vin_photo", "VIN plate"), ("odometer", "Odometer"),
                            ("damage_closeup", "Damage close-up"), ("damage_wide", "Damage in context")]:
            tile = page.locator("div.rounded-xl", has=page.get_by_text(title, exact=True)).last
            tile.locator("button", has_text="Take photo").or_(tile.locator("button", has_text="Retake")).first.click()
            page.wait_for_timeout(1500)  # the fake camera starts streaming
            shoot(page, out, f"4-camera-{slot}")
            page.get_by_role("button", name="Cancel").first.click()

        start(page, base, ready["token"])
        page.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Continue").click()
        page.wait_for_timeout(1500)  # photo checks finish in the background
        shoot(page, out, "5-check")
        shoot(page, out, "5-check-full", full=True)
        page.get_by_role("button", name="Continue").click()
        shoot(page, out, "6-send")

        start(page, base, retake["token"])
        page.get_by_role("button", name="Continue").click()
        page.get_by_role("button", name="Continue").click()
        page.wait_for_timeout(1500)
        shoot(page, out, "5b-check-retake")

        start(page, base, round2["token"])
        page.wait_for_timeout(800)
        shoot(page, out, "7-round2")
        shoot(page, out, "7-round2-full", full=True)
        browser.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dist", type=Path, default=ROOT / "app" / "dist")
    parser.add_argument("--channel", default="chrome", help="chrome (system) or chromium (bundled)")
    args = parser.parse_args()
    if "test" not in TEST_DATABASE_URL.rsplit("/", 1)[-1]:
        sys.exit("TEST_DATABASE_URL must name a test database; it is emptied first")
    if not (args.dist / "index.html").exists():
        sys.exit(f"No build at {args.dist}; run `npm run build` in app/ first")
    db = Database(TEST_DATABASE_URL)
    db.migrate()
    db.close()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        reset(conn)
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(
            data_dir=Path(tmp) / "data", frontend_dist=args.dist, admin_password=ADMIN_PASSWORD,
            session_secret="screenshots", database_url=TEST_DATABASE_URL,
            blobstore=LocalBlobStore(Path(tmp) / "blobs"), model_transport=FakeModel(), check_license=False,
        )
        port = free_port()
        server = serve(app, port)
        try:
            run(args.out.resolve(), f"http://127.0.0.1:{port}", Seeder(TestClient(app, base_url="https://testserver")),
                args.channel)
        finally:
            server.should_exit = True
            app.state.store.db.close()


if __name__ == "__main__":
    os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)
    main()
