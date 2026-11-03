"""T45 end-to-end: the claimant PWA in a real browser against the fake model (tasks/t45-design.md).

Needs `cd app && npm run build` first and Playwright (`uv run --with playwright pytest tests_e2e`).
Uses system Chrome locally (E2E_CHANNEL=chrome, the default) or the bundled Chromium in CI
(E2E_CHANNEL=chromium after `playwright install --with-deps chromium`). TEST_DATABASE_URL must name a
test database: it is emptied first.
"""

import os
import re
import sys
import tempfile
import time
from pathlib import Path

import pytest

REQUIRED = os.environ.get("E2E_REQUIRED") == "1"  # CI: a missing browser or build fails instead of skipping
ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "app" / "dist"
sys.path.insert(0, str(ROOT / "server" / "tests"))
sys.path.insert(0, str(ROOT / "server" / "scripts"))

try:
    import playwright.sync_api as playwright_api
except ImportError:
    if REQUIRED:
        raise
    pytest.skip("playwright is not installed", allow_module_level=True)
if not (DIST / "index.html").exists():
    if REQUIRED:
        raise RuntimeError("no PWA build; run `npm run build` in app/ first")
    pytest.skip("no PWA build; run `npm run build` in app/ first", allow_module_level=True)

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from claimant_screenshots import PHONE_UA, SHOTS, SIGNED, VIEWPORT, Seeder, free_port, serve  # noqa: E402
from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, GOOD_DAMAGE, FakeModel  # noqa: E402
from evidence_server.blobstore import LocalBlobStore  # noqa: E402
from evidence_server.db import Database  # noqa: E402
from evidence_server.main import create_app  # noqa: E402
from pg import TEST_DATABASE_URL, reset  # noqa: E402

CHANNEL = os.environ.get("E2E_CHANNEL", "chrome")
OUT = ROOT / "server" / "tests_e2e" / "screens"
GLASS = {"loss_type": "glass", "loss_date": "2026-10-27", "loss_city": "San Jose", "loss_state": "CA",
         "description": "A rock hit the windshield on the highway.", "glass_which": ["Windshield"],
         "glass_kind": "A crack", "glass_cause": "A rock or road debris", "drivable": "Yes",
         "damaged_areas": ["Windshield"], "prior_damage": "No"}
WINDSHIELD = {"is_vehicle": True, "damage_visible": True, "parts": ["Windshield"], "unsure": False, "problem": "none"}


def glass_model() -> FakeModel:
    """The fake model sees a cracked windshield, so the photo check agrees with the glass claim."""
    model = FakeModel()
    model.set("damage_closeup", WINDSHIELD)
    model.set("damage_wide", WINDSHIELD)
    model.set("damage", GOOD_DAMAGE | {"damages": [{"area": "Windshield", "damage_type": "GLASS_DAMAGE",
        "severity": "minor", "age": "new", "box": [300, 200, 700, 400], "observed": "Crack across the windshield"}]})
    return model


@pytest.fixture(scope="module")
def stack():
    assert "test" in TEST_DATABASE_URL.rsplit("/", 1)[-1], "TEST_DATABASE_URL must name a test database"
    db = Database(TEST_DATABASE_URL)
    db.migrate()
    db.close()
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        reset(conn)
    with tempfile.TemporaryDirectory() as tmp:
        app = create_app(
            data_dir=Path(tmp) / "data", frontend_dist=DIST, admin_password=ADMIN_PASSWORD,
            session_secret="e2e", database_url=TEST_DATABASE_URL,
            blobstore=LocalBlobStore(Path(tmp) / "blobs"), model_transport=glass_model(), check_license=False,
        )
        port = free_port()
        server = serve(app, port)
        seeder = Seeder(TestClient(app, base_url="https://testserver"))
        with playwright_api.sync_playwright() as p:
            browser = p.chromium.launch(
                channel=None if CHANNEL == "chromium" else CHANNEL,
                args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"],
            )
            yield {"base": f"http://127.0.0.1:{port}", "seeder": seeder, "browser": browser}
            browser.close()
        server.should_exit = True
        app.state.store.db.close()


@pytest.fixture
def page(stack):
    ctx = stack["browser"].new_context(viewport=VIEWPORT, device_scale_factor=2, is_mobile=True, has_touch=True,
                                       user_agent=PHONE_UA, permissions=["camera"])  # touch: not the desktop QR gate
    pg = ctx.new_page()
    yield pg
    ctx.close()


def shot(pg, name):
    OUT.mkdir(parents=True, exist_ok=True)
    pg.screenshot(path=str(OUT / f"{name}.png"), full_page=True)


def start(pg, base, token):
    pg.goto(f"{base}/c/{token}")
    pg.get_by_role("checkbox").first.check()
    pg.get_by_role("button", name="Start").click()


def step(pg):
    return pg.get_by_test_id("step-label").inner_text()


def on_screen(pg, title):
    """Wait until the step label names this screen (answers save and re-render asynchronously)."""
    playwright_api.expect(pg.get_by_test_id("step-label")).to_contain_text(title)


def choose(pg, option):
    """A single-choice option: a button with role radio (multiselect options stay plain buttons)."""
    pg.get_by_role("radio", name=option, exact=True).click()


def choose_and_save(pg, option):
    """Choose, then wait for the draft save the app sends after its short delay (the reply carries the new layout)."""
    with pg.expect_response(lambda r: r.url.endswith("/draft") and r.request.method == "PUT"):
        choose(pg, option)
    pg.wait_for_timeout(300)  # the new layout renders


def total(pg):
    return int(step(pg).split(" of ")[1].split(":")[0])


def next_screen(pg):
    before = step(pg)
    pg.get_by_test_id("next").click()
    pg.wait_for_function("b => document.querySelector('[data-testid=step-label]')?.innerText !== b", arg=before)


# --- picker, pacing, the parts screen and the brand ---


def test_glass_claim_screens_one_topic_at_a_time(stack, page):
    seeder = stack["seeder"]
    seeder.api.put("/api/admin/settings", json={"brand_color": "#0b6e4f"}).raise_for_status()
    claim = seeder.claim()
    start(page, stack["base"], claim["token"])
    logo = page.get_by_test_id("brand-logo")
    assert logo.evaluate("e => getComputedStyle(e).backgroundColor") == "rgb(11, 110, 79)"
    shot(page, "01-picker")
    page.get_by_test_id("type-glass").click()

    on_screen(page, "When and where")
    page.get_by_label("What day did it happen?").fill("2026-10-27")
    page.get_by_label("City").fill("San Jose")
    page.get_by_label("State").fill("CA")
    shot(page, "02-when-where")
    next_screen(page)

    on_screen(page, "About the glass")
    page.get_by_role("button", name="Windshield", exact=True).click()  # which glass: several may be chosen
    choose(page, "A crack")
    choose(page, "A rock or road debris")
    shot(page, "03-details")
    next_screen(page)

    on_screen(page, "In your own words")
    page.get_by_label("What happened, in your own words?").fill("A rock hit the windshield on the highway.")
    next_screen(page)

    on_screen(page, "Your car")
    assert page.get_by_label("VIN").input_value() == "1HGCV1F39LA526018"  # from the policy
    choose(page, "Yes")
    next_screen(page)

    on_screen(page, "Where is the damage?")
    chip = page.get_by_test_id("part-Windshield")
    chip.click()
    assert chip.get_attribute("aria-pressed") == "true"
    lit = page.locator('svg [data-area="Windshield"]').first
    assert "on" in (lit.get_attribute("class") or "")
    shot(page, "04-parts")
    next_screen(page)

    on_screen(page, "Damage from before")
    choose(page, "No")
    next_screen(page)

    on_screen(page, "Photos")
    page.get_by_test_id("shoot-plate").click()
    page.get_by_test_id("shutter").wait_for()
    page.get_by_test_id("camera-cancel").click()
    shot(page, "05-photos")


def test_a_parked_hit_and_run_has_no_driver_screen(stack, page):
    claim = stack["seeder"].claim({"loss_type": "hit_and_run", "loss_date": "2026-10-27", "loss_city": "San Jose",
                                   "loss_state": "CA"})
    start(page, stack["base"], claim["token"])
    next_screen(page)  # when and where is already filled
    on_screen(page, "About the other vehicle")
    choose_and_save(page, "Being driven")
    driven = total(page)
    choose_and_save(page, "Parked")
    # no "Driver" screen when the car was parked
    playwright_api.expect(page.get_by_test_id("step-label")).to_contain_text(f" of {driven - 1}:")


# --- check, sign and send ---


def test_a_complete_glass_claim_is_signed_and_sent(stack, page):
    seeder = stack["seeder"]
    claim = seeder.claim(GLASS, ("plate", "vin_photo", "damage_closeup", "damage_wide"))
    start(page, stack["base"], claim["token"])
    for _ in range(20):
        if "Check" in step(page):
            break
        next_screen(page)
    on_screen(page, "Check")
    shot(page, "06-check")
    next_screen(page)

    on_screen(page, "Sign and send")
    page.get_by_label("Your full name").fill("Jordan Avery")
    page.get_by_role("checkbox").first.check()
    pad = page.get_by_test_id("signature-pad")
    box = pad.bounding_box()
    page.mouse.move(box["x"] + 20, box["y"] + box["height"] * 0.7)
    page.mouse.down()
    for x, y in ((0.3, 0.3), (0.5, 0.7), (0.7, 0.3), (0.9, 0.6)):
        page.mouse.move(box["x"] + box["width"] * x, box["y"] + box["height"] * y, steps=8)
    page.mouse.up()
    page.get_by_role("button", name="Use this signature").click()
    shot(page, "07-sign")
    send = page.get_by_test_id("send")
    send.wait_for()
    page.wait_for_function("() => !document.querySelector('[data-testid=send]')?.disabled", timeout=15000)
    send.click()
    page.get_by_test_id("done").wait_for(timeout=15000)
    shot(page, "08-done")
    assert seeder.api.get(f"/api/admin/claims/{claim['id']}").json()["status"] == "submitted"


# --- the adjuster sees the same car ---


def test_the_adjuster_sees_the_same_car_with_the_damage_lit(stack):
    seeder = stack["seeder"]
    claim = seeder.claim(GLASS | SIGNED, ("plate", "vin_photo", "damage_closeup", "damage_wide", "signature"))
    seeder.api.post(f"/api/c/{claim['token']}/submit", json={}).raise_for_status()
    for _ in range(100):  # the damage reading runs after the response
        if (seeder.api.get(f"/api/admin/claims/{claim['id']}").json().get("damage") or {}).get("damages"):
            break
        time.sleep(0.1)
    ctx = stack["browser"].new_context(viewport={"width": 1440, "height": 900})
    pg = ctx.new_page()
    try:
        pg.goto(f"{stack['base']}/login")
        pg.get_by_label("Email").fill(ADMIN_EMAIL)
        pg.get_by_label("Password").fill(ADMIN_PASSWORD)
        pg.get_by_role("button", name="Sign in").click()
        pg.wait_for_url(re.compile(r"/(claims/.*)?$"))
        pg.goto(f"{stack['base']}/claims/{claim['id']}")
        windshield = pg.locator('svg [data-area="Windshield"][role="button"]').first
        playwright_api.expect(windshield).to_have_class(re.compile(r"\bseen\b"))
        assert "declared" in (windshield.get_attribute("class") or "").split()
        assert "missing" not in (windshield.get_attribute("class") or "").split()
        hood = pg.locator('svg [data-area="Hood"][role="button"]').first
        assert "seen" not in (hood.get_attribute("class") or "").split()
        windshield.click()
        playwright_api.expect(windshield).to_have_attribute("aria-pressed", "true")
        shot(pg, "09-console-damage")
    finally:
        ctx.close()


# --- Send back: round 2 asks only for the requested photo ---


def test_after_a_send_back_only_the_requested_photo_is_asked(stack, page):
    seeder = stack["seeder"]
    claim = seeder.claim(GLASS | SIGNED, ("plate", "vin_photo", "damage_closeup", "damage_wide", "signature"))
    seeder.api.post(f"/api/c/{claim['token']}/submit", json={}).raise_for_status()
    for _ in range(100):  # the reading and the proposal run after the response
        if (seeder.api.get(f"/api/admin/claims/{claim['id']}").json().get("proposal") or {}).get("status") == "ready":
            break
        time.sleep(0.1)
    seeder.api.post(f"/api/admin/claims/{claim['id']}/decision", json={
        "action": "send_back", "reason": "The wide shot is too dark.", "slots": ["damage_wide"]}).raise_for_status()
    start(page, stack["base"], claim["token"])
    playwright_api.expect(page.get_by_text("Your insurer asked for 1 more photo")).to_be_visible()
    playwright_api.expect(page.get_by_test_id("shoot-damage_wide")).to_be_visible()
    assert page.get_by_test_id("shoot-plate").count() == 0  # the other shots stay as sent
    playwright_api.expect(page.get_by_text("Your answers, as you sent them")).to_be_visible()
    shot(page, "10-round2")


def test_a_choice_is_a_suggestion_the_claimant_can_write_past(stack, page):
    claim = stack["seeder"].claim()
    start(page, stack["base"], claim["token"])
    page.get_by_test_id("type-glass").click()
    on_screen(page, "When and where")
    page.get_by_label("What day did it happen?").fill("2026-10-27")
    page.get_by_label("City").fill("San Jose")
    page.get_by_label("State").fill("CA")
    next_screen(page)

    on_screen(page, "About the glass")
    page.get_by_role("button", name="Windshield", exact=True).click()
    choose(page, "A crack")
    box = page.get_by_label("What caused it? (in your own words)")
    assert box.count() == 0  # the text box opens only for the escape option
    choose(page, "Something else")
    box = page.get_by_label("What caused it? (in your own words)")
    playwright_api.expect(box).to_be_visible()
    assert page.get_by_test_id("next").is_disabled()  # the words are asked for before going on
    with page.expect_response(lambda r: r.url.endswith("/draft") and r.request.method == "PUT"):
        box.fill("A stone thrown from a bridge")
    page.wait_for_timeout(300)
    shot(page, "03b-own-words")
    assert page.get_by_test_id("next").is_enabled()


# --- T60: say it in your own words, then confirm the card ---


def test_own_words_point_to_a_card_the_claimant_confirms(stack, page):
    claim = stack["seeder"].claim()
    start(page, stack["base"], claim["token"])
    box = page.get_by_label("Tell us what happened")
    playwright_api.expect(box).to_be_visible()
    playwright_api.expect(page.get_by_text("Tell us in a few words, or pick the closest answer below.")).to_be_visible()
    assert page.get_by_test_id("type-glass").is_visible()  # the list of cards stays below, always usable
    find = page.get_by_role("button", name="Find the right type")
    assert find.is_disabled()  # too short to read
    story = "A rock hit the windshield on the highway."
    box.fill(story)
    assert find.is_enabled()
    find.click()
    suggestion = page.get_by_test_id("type-suggestion")
    playwright_api.expect(suggestion).to_contain_text("It looks like: Glass only")
    shot(page, "01b-suggestion")
    page.get_by_test_id("type-accept").click()  # same as tapping the card

    on_screen(page, "When and where")
    page.get_by_label("What day did it happen?").fill("2026-10-27")
    page.get_by_label("City").fill("San Jose")
    next_screen(page)
    on_screen(page, "About the glass")
    page.get_by_role("button", name="Windshield", exact=True).click()
    choose(page, "A crack")
    choose(page, "A rock or road debris")
    next_screen(page)
    on_screen(page, "In your own words")
    assert page.get_by_label("What happened, in your own words?").input_value() == story  # not typed twice


def test_declining_the_suggestion_leaves_the_list_to_choose_from(stack, page):
    claim = stack["seeder"].claim()
    start(page, stack["base"], claim["token"])
    page.get_by_label("Tell us what happened").fill("A rock hit the windshield on the highway.")
    page.get_by_role("button", name="Find the right type").click()
    suggestion = page.get_by_test_id("type-suggestion")
    playwright_api.expect(suggestion).to_be_visible()
    page.get_by_test_id("type-reject").click()
    playwright_api.expect(suggestion).to_have_count(0)
    on_screen(page, "What happened")  # still on the first screen
    page.get_by_test_id("type-single_vehicle").click()
    on_screen(page, "When and where")
