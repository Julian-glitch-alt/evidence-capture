"""T52 console in a real browser (tasks/t52-design.md, part B): Today, the loss notice beside the review,
the approve guard, the SIU reason, and the diary. Run after `cd app && npm run build`:
`cd server && uv run --with playwright==1.64.1 python -m pytest tests_e2e/test_console_e2e.py`."""

import re
import time
from datetime import UTC, datetime, timedelta

import os

import pytest

os.environ.setdefault("CONTACT_EMAIL", "trials@example.com")  # T54: read when the stack builds the app

# Imported first: it skips the module when Playwright or the app build is missing (CI's server job).
from test_claimant_e2e import ADMIN_EMAIL, ADMIN_PASSWORD, GLASS, OUT, page, playwright_api, stack  # noqa: F401
from claimant_screenshots import ANSWERS, SHOTS, SIGNED, Phone

DESK = {"width": 1440, "height": 900}
# A parked hit and run with no police report yet: the checks raise POLICE_REPORT_NEEDED for review.
HIT_AND_RUN = ANSWERS | SIGNED | {"cause": "Hit while parked", "other_vehicle": "Yes", "other_driver_left": "Yes",
                                  "police_contacted": "Not yet"}
expect = playwright_api.expect


def ready(seeder, answers):
    claim = seeder.claim(answers, tuple(SHOTS))
    seeder.api.post(f"/api/c/{claim['token']}/submit", json={}).raise_for_status()
    for _ in range(150):  # the reading, proposal and report run after the response
        d = seeder.api.get(f"/api/admin/claims/{claim['id']}").json()
        if (d.get("proposal") or {}).get("status") == "ready" and d.get("review_due"):
            return claim
        time.sleep(0.1)
    raise AssertionError("the claim never got a ready proposal")


@pytest.fixture
def desk(stack):  # noqa: F811
    ctx = stack["browser"].new_context(viewport=DESK)
    pg = ctx.new_page()
    pg.goto(f"{stack['base']}/login")
    pg.get_by_label("Email").fill(ADMIN_EMAIL)
    pg.get_by_label("Password").fill(ADMIN_PASSWORD)
    pg.get_by_role("button", name="Sign in").click()
    pg.wait_for_url(re.compile(r"/(claims/.*)?$"))
    yield pg
    ctx.close()


def shot(pg, name):
    OUT.mkdir(parents=True, exist_ok=True)
    pg.screenshot(path=str(OUT / f"{name}.png"), full_page=True)


def test_today_lists_claims_and_the_tiles_filter_them(stack, desk):  # noqa: F811
    sent = ready(stack["seeder"], GLASS | SIGNED)
    waiting = stack["seeder"].claim()
    desk.goto(stack["base"] + "/")
    expect(desk.get_by_role("heading", name="Today", level=1)).to_be_visible()
    expect(desk.get_by_role("button", name="Needs me")).to_have_attribute("aria-pressed", "true")
    rows = desk.get_by_role("row")
    expect(rows.filter(has_text=sent["id"])).to_have_count(1)
    expect(rows.filter(has_text=waiting["id"])).to_have_count(0)
    shot(desk, "20-console-today")

    desk.get_by_role("button", name=re.compile(r"^Waiting for the claimant")).click()
    expect(rows.filter(has_text=waiting["id"])).to_have_count(1)
    expect(rows.filter(has_text=sent["id"])).to_have_count(0)

    desk.get_by_role("button", name=re.compile(r"^All\b")).click()
    expect(rows.filter(has_text=waiting["id"])).to_have_count(1)
    expect(rows.filter(has_text=sent["id"])).to_have_count(1)

    desk.get_by_role("button", name="How to read the labels").click()
    expect(desk.get_by_text("Signs of fraud", exact=False)).to_be_visible()


def test_the_claim_page_shows_the_notice_beside_the_review(stack, desk):  # noqa: F811
    claim = ready(stack["seeder"], HIT_AND_RUN)
    desk.goto(f"{stack['base']}/claims/{claim['id']}")
    notice = desk.get_by_role("article", name="Loss notice as submitted by the claimant")
    expect(notice.get_by_role("heading", name="Automobile Loss Notice")).to_be_visible()
    expect(notice.get_by_text("What day did it happen?", exact=True)).to_be_visible()  # the label the claimant saw
    review = desk.get_by_role("article", name="Review of the claim")
    for name in ("In short", "1. Coverage and vehicle", "2. What happened and liability", "3. Damage",
                 "4. Estimate and reserve", "Suggested next step"):
        expect(review.get_by_role("heading", name=name)).to_be_visible()
    expect(review.get_by_role("group", name=re.compile(r"^Your review of finding \d+$")).first).to_be_visible()
    shot(desk, "21-console-claim")

    police = desk.locator("#answer-police_contacted")
    expect(police.locator("mark")).to_have_count(1)
    finding = review.get_by_role("listitem").filter(has_text="police report")
    finding.get_by_role("button", name="Show in the form").click()
    expect(police).to_be_in_viewport()


def test_approve_with_unreviewed_findings_asks_first(stack, desk):  # noqa: F811
    claim = ready(stack["seeder"], HIT_AND_RUN)
    desk.goto(f"{stack['base']}/claims/{claim['id']}")
    desk.get_by_role("button", name="Approve", exact=True).click()
    expect(desk.get_by_text(re.compile(r"not reviewed yet"))).to_be_visible()
    expect(desk.get_by_role("button", name="Review findings")).to_be_visible()
    expect(desk.get_by_role("button", name="Approve anyway")).to_be_visible()
    assert stack["seeder"].api.get(f"/api/admin/claims/{claim['id']}").json()["decision"] is None


def test_refer_to_siu_needs_a_written_reason(stack, desk):  # noqa: F811
    claim = ready(stack["seeder"], HIT_AND_RUN)
    desk.goto(f"{stack['base']}/claims/{claim['id']}")
    desk.get_by_role("button", name="Refer to SIU", exact=True).click()
    reason = desk.get_by_label("Reason for the referral")
    send = desk.get_by_role("button", name="Refer to SIU with this reason")
    reason.fill("")
    expect(send).to_be_disabled()
    reason.fill("No police report for a hit and run")
    expect(send).to_be_enabled()
    send.click()
    for _ in range(50):
        decision = stack["seeder"].api.get(f"/api/admin/claims/{claim['id']}").json()["decision"]
        if decision:
            break
        time.sleep(0.1)
    assert decision["action"] == "refer_siu"
    assert decision["reason"] == "No police report for a hit and run"


def test_a_diary_set_on_the_claim_shows_on_today(stack, desk):  # noqa: F811
    claim = ready(stack["seeder"], HIT_AND_RUN)
    due = (datetime.now(UTC).date() + timedelta(days=1)).isoformat()
    desk.goto(f"{stack['base']}/claims/{claim['id']}")
    desk.get_by_label("Follow-up date").fill(due)
    desk.get_by_label("Reason", exact=True).fill("Call about the police report")
    desk.get_by_role("button", name="Set diary").click()
    expect(desk.get_by_text("Call about the police report")).to_be_visible()
    diaries = stack["seeder"].api.get(f"/api/admin/claims/{claim['id']}").json()["diaries"]
    assert [(d["due_on"], d["reason"]) for d in diaries] == [(due, "Call about the police report")]

    desk.goto(stack["base"] + "/")
    desk.get_by_role("button", name=re.compile(r"^All\b")).click()
    expect(desk.get_by_text(re.compile(r"Diary due .*Call about the police report"))).to_be_visible()


# --- T53 (tasks/t53-design.md): a trial with the tester's own car, and Today while it loads ---


def test_an_adjuster_opens_a_claim_with_the_policy_details(stack, desk):  # noqa: F811
    # T53 + T56: the policy as it is in the insurer's system (a tester enters their own car).
    desk.goto(stack["base"] + "/")
    desk.locator(".ev-pagehead").get_by_role("button", name="New capture link").click()  # the guide card has one too
    expect(desk.get_by_role("button", name="Open a claim")).to_have_attribute("aria-pressed", "true")
    desk.get_by_label("Policy number").fill("pas-778812")
    desk.get_by_label("Policyholder name").fill("Mike Tester")
    desk.get_by_label("State").select_option("TX")
    desk.get_by_label("Year").fill("2023")
    desk.get_by_label("Make").fill("Toyota")
    desk.get_by_label("Model", exact=True).fill("Camry")
    desk.get_by_label("Plate").fill("8ABC123")
    desk.get_by_label("VIN", exact=True).fill("JTDKARFU1PA186091")
    expect(desk.get_by_label("Collision deductible")).to_have_value("500")
    desk.get_by_label("Comprehensive deductible").fill("100")
    expect(desk.get_by_text("please don't use customer data.", exact=False)).to_be_visible()
    shot(desk, "50-console-open-claim-form")
    desk.get_by_role("button", name="Create capture link").click()
    desk.wait_for_url(re.compile(r"/claims/CLM-"))
    claim_id = desk.url.rsplit("/", 1)[-1]
    policy = stack["seeder"].api.get(f"/api/admin/claims/{claim_id}").json()["policy"]
    assert (policy["policy_no"], policy["source"]) == ("PAS-778812", "trial")
    assert policy["data"]["vehicles"][0]["plate"] == "8ABC123"
    assert policy["data"]["coverages"]["comprehensive"] == {"deductible": 100}
    expect(desk.get_by_text("2023 Toyota Camry", exact=False).first).to_be_visible()

    desk.goto(stack["base"] + "/?tab=waiting")
    row = desk.get_by_role("row").filter(has_text=claim_id)
    expect(row).to_have_count(1)
    expect(row.get_by_text("Trial", exact=True)).to_have_count(0)


def test_a_bad_vin_in_the_claim_form_says_why(stack, desk):  # noqa: F811
    desk.goto(stack["base"] + "/")
    desk.locator(".ev-pagehead").get_by_role("button", name="New capture link").click()  # the guide card has one too
    for label, value in (("Policyholder name", "Mike Tester"), ("Year", "2023"), ("Make", "Toyota"), ("Plate", "8ABC123")):
        desk.get_by_label(label).fill(value)
    desk.get_by_label("Model", exact=True).fill("Camry")
    desk.get_by_label("VIN", exact=True).fill("5YJ3E1EA8MA159084")
    desk.get_by_role("button", name="Create capture link").click()
    expect(desk.get_by_text("This VIN is not valid. Check each character.", exact=False)).to_be_visible()
    assert "/claims/" not in desk.url


def test_try_the_demo_opens_a_claim_on_the_sample_policy(stack, desk):  # noqa: F811
    desk.goto(stack["base"] + "/")
    desk.locator(".ev-pagehead").get_by_role("button", name="New capture link").click()
    desk.get_by_role("button", name="Try the demo").click()
    expect(desk.get_by_text("plate DEMO101", exact=False)).to_be_visible()
    desk.get_by_role("button", name="Open a demo claim").click()
    desk.wait_for_url(re.compile(r"/claims/CLM-"))
    claim_id = desk.url.rsplit("/", 1)[-1]
    policy = stack["seeder"].api.get(f"/api/admin/claims/{claim_id}").json()["policy"]
    assert (policy["policy_no"], policy["source"]) == ("DEMO-AUTO-1001", "on_file")


def test_today_shows_a_loading_state_not_an_empty_queue(stack, desk):  # noqa: F811
    claim = ready(stack["seeder"], GLASS | SIGNED)
    held = []
    desk.route("**/api/admin/queue", lambda route: held.append(route))  # answers wait until released
    desk.goto(stack["base"] + "/")
    expect(desk.get_by_text("Loading claims…")).to_be_visible()
    expect(desk.locator("[aria-busy='true']").first).to_be_visible()
    for empty in ("queue empty", "No claim matches", "Nothing overdue", "No one waiting", "0 need a decision"):
        expect(desk.get_by_text(empty, exact=False)).to_have_count(0)
    shot(desk, "51-console-today-loading")
    for route in held:  # released before unroute, which would settle them itself
        route.continue_()
    desk.unroute("**/api/admin/queue")
    expect(desk.get_by_role("row").filter(has_text=claim["id"])).to_have_count(1)
    expect(desk.locator("[aria-busy='true']")).to_have_count(0)


def test_claims_from_a_test_key_never_count_on_today(stack, desk):  # noqa: F811
    # A test API key (T48) runs claims for real in their own lane; Today must not count them as work.
    seeder = stack["seeder"]
    live = ready(seeder, GLASS | SIGNED)
    desk.goto(stack["base"] + "/")
    hero = desk.locator(".ev-kpi.hero .v")
    expect(desk.get_by_role("row").filter(has_text=live["id"])).to_have_count(1)
    before = hero.inner_text()  # other tests share this server: compare, don't assume a number
    key = seeder.api.post("/api/admin/api-keys", json={"name": "IT sandbox", "mode": "test"}).json()["key"]
    r = seeder.api.post("/api/v1/capture-requests", headers={"Authorization": f"Bearer {key}"}, json={
        "insurer_claim_number": "SANDBOX-1", "policy_number": "DEMO-AUTO-1001",
        "callback_url": "https://testserver/api/demo-insurer/webhook"})
    assert r.status_code == 201, r.text
    case = r.json()
    token = case["capture_url"].rsplit("/", 1)[-1]
    seeder.api.put(f"/api/c/{token}/draft", json={"answers": GLASS | SIGNED}).raise_for_status()
    phone = Phone(seeder.api, token)
    for slot in SHOTS:
        data, mime = SHOTS[slot]
        phone.upload(slot, data, mime).raise_for_status()
    seeder.api.post(f"/api/c/{token}/submit", json={}).raise_for_status()
    for _ in range(150):
        d = seeder.api.get(f"/api/admin/claims/{case['case_id']}").json()
        if (d.get("proposal") or {}).get("status") == "ready":
            break
        time.sleep(0.1)
    assert d["test_mode"] is True and d["proposal"]["lane"]

    desk.goto(stack["base"] + "/")
    rows = desk.get_by_role("row")
    expect(rows.filter(has_text=live["id"])).to_have_count(1)
    expect(rows.filter(has_text=case["case_id"])).to_have_count(0)
    expect(hero).to_have_text(before)
    expect(desk.get_by_role("button", name=re.compile(r"^Needs me"))).to_contain_text(before)
    expect(desk.get_by_role("link", name=re.compile(r"^Today"))).to_contain_text(before)
    desk.get_by_role("button", name=re.compile(r"^All\b")).click()
    expect(rows.filter(has_text=case["case_id"])).to_have_count(0)
    desk.get_by_role("button", name=re.compile(r"^Test claims")).click()
    expect(rows.filter(has_text=case["case_id"])).to_have_count(1)
    expect(rows.filter(has_text=live["id"])).to_have_count(0)


# --- T54 (tasks/t54-design.md): privacy notice, trial guide, feedback link, sign-in check ---


def test_the_privacy_notice_is_public_and_names_the_facts(stack):  # noqa: F811
    ctx = stack["browser"].new_context(viewport=DESK)
    pg = ctx.new_page()
    pg.goto(stack["base"] + "/privacy")
    expect(pg.get_by_role("heading", name="Privacy notice", level=1)).to_be_visible()
    # T57: the AI line follows the running model (the test stack runs the Meta host)
    for fact in ("an AI model from Meta", "90 days after the insurer's decision", "do not use customer data",
                 "We do not collect your location", "trials@example.com"):
        expect(pg.get_by_text(fact, exact=False).first).to_be_visible()
    assert "/login" not in pg.url
    shot(pg, "53-privacy-notice")
    ctx.close()


def test_the_claimant_start_screen_links_the_privacy_notice(stack, page):  # noqa: F811
    claim = stack["seeder"].claim()
    page.goto(f"{stack['base']}/c/{claim['token']}")
    link = page.get_by_role("link", name="Privacy notice")
    expect(link).to_be_visible()
    expect(link).to_have_attribute("href", "/privacy")
    expect(link).to_have_attribute("target", "_blank")


def test_today_shows_the_trial_guide_until_dismissed(stack, desk):  # noqa: F811
    desk.goto(stack["base"] + "/")
    guide = desk.get_by_role("heading", name="Try it in 3 steps")
    expect(guide).to_be_visible()
    shot(desk, "52-console-try-guide")
    desk.get_by_role("button", name="Dismiss the guide").click()
    expect(guide).to_have_count(0)
    desk.reload()
    expect(desk.get_by_role("heading", name="Today", level=1)).to_be_visible()
    expect(guide).to_have_count(0)


def test_the_console_warns_that_the_trial_workspace_is_shared(stack, desk):  # noqa: F811
    desk.goto(stack["base"] + "/")
    note = desk.get_by_role("note", name="Shared workspace notice")
    expect(note).to_be_visible()
    expect(note).to_contain_text("Shared trial workspace")
    expect(note).to_contain_text("your own car and made-up details")
    assert note.get_by_role("link", name="Privacy").get_attribute("href") == "/privacy"
    desk.get_by_role("button", name="Dismiss the workspace notice").click()
    expect(note).to_have_count(0)
    desk.reload()
    expect(desk.get_by_role("heading", name="Today", level=1)).to_be_visible()
    expect(note).to_have_count(0)


def test_the_console_offers_a_feedback_link(stack, desk):  # noqa: F811
    desk.goto(stack["base"] + "/")
    link = desk.get_by_role("link", name="Send feedback")
    expect(link).to_be_visible()
    assert link.get_attribute("href").startswith("mailto:trials@example.com")


def test_the_console_never_shows_before_the_sign_in_check(stack):  # noqa: F811
    ctx = stack["browser"].new_context(viewport=DESK)
    pg = ctx.new_page()
    held = []
    pg.route("**/api/admin/me", lambda route: held.append(route))
    pg.goto(stack["base"] + "/")
    pg.wait_for_timeout(800)
    expect(pg.get_by_role("heading", name="Today", level=1)).to_have_count(0)
    expect(pg.get_by_role("button", name="New capture link")).to_have_count(0)
    for route in held:
        route.continue_()
    pg.unroute("**/api/admin/me")
    pg.wait_for_url(re.compile(r"/login"))
    expect(pg.get_by_role("link", name="Privacy")).to_be_visible()
    ctx.close()


# --- T55 (tasks/t55-design.md): rail, Settings tabs, Developers code blocks ---


def test_the_rail_has_one_item_per_page(stack, desk):  # noqa: F811
    desk.goto(stack["base"] + "/settings")
    rail = desk.get_by_role("complementary", name="Main navigation")
    expect(rail.get_by_role("link", name=re.compile(r"^All claims"))).to_have_count(0)
    expect(rail.get_by_role("link", name=re.compile(r"^Users"))).to_have_count(0)
    expect(rail.get_by_role("link", name=re.compile(r"^Settings"))).to_have_count(1)
    expect(rail.locator("a.on")).to_have_count(1)


def test_settings_are_split_into_four_tabs(stack, desk):  # noqa: F811
    desk.goto(stack["base"] + "/settings")
    expect(desk.get_by_role("button", name="Team")).to_have_attribute("aria-pressed", "true")
    expect(desk.get_by_role("heading", name="Add user")).to_be_visible()
    expect(desk.get_by_role("heading", name="API keys")).to_have_count(0)
    for tab, heading in (("Claimant page", "Brand"), ("Reports", "Report delivery"), ("Reports", "Data retention"),
                         ("Integrations", "API keys"), ("Integrations", "Webhook secret")):
        desk.get_by_role("button", name=tab).click()
        expect(desk.get_by_role("heading", name=heading, exact=True)).to_be_visible()
    shot(desk, "54-settings-integrations")
    desk.goto(stack["base"] + "/settings?tab=reports")
    expect(desk.get_by_role("button", name="Reports")).to_have_attribute("aria-pressed", "true")
    expect(desk.get_by_role("heading", name="Report delivery")).to_be_visible()


def test_developer_code_blocks_stay_inside_their_card(stack):  # noqa: F811
    ctx = stack["browser"].new_context(viewport=DESK)
    pg = ctx.new_page()
    pg.goto(stack["base"] + "/developers")
    expect(pg.get_by_role("link", name="Verify a sealed package")).to_be_visible()
    expect(pg.get_by_role("link", name="API reference", exact=True)).to_be_visible()
    pg.locator("pre").first.wait_for()
    spill = pg.evaluate("""() => [...document.querySelectorAll('pre')].filter(p => {
        const card = p.closest('.ev-docs, .ev-panel') || document.body
        return p.getBoundingClientRect().right > card.getBoundingClientRect().right + 1
    }).length""")
    assert spill == 0, f"{spill} code blocks run past their card"
    assert pg.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    shot(pg, "55-developers")
    ctx.close()


# --- T56 (tasks/t56-design.md): review at full length, Verify out of the daily path ---


def test_the_review_column_flows_with_the_page(stack, desk):  # noqa: F811
    claim = ready(stack["seeder"], GLASS | SIGNED)
    desk.goto(f"{stack['base']}/claims/{claim['id']}")
    aside = desk.get_by_role("complementary", name="Assessment")
    expect(aside).to_be_visible()
    style = aside.evaluate("el => [getComputedStyle(el).overflowY, getComputedStyle(el).maxHeight]")
    assert style == ["visible", "none"], style


def test_verify_lives_next_to_the_package_not_in_the_menu(stack, desk):  # noqa: F811
    claim = ready(stack["seeder"], GLASS | SIGNED)
    desk.goto(stack["base"] + "/")
    rail = desk.get_by_role("complementary", name="Main navigation")
    expect(rail.get_by_role("link", name=re.compile(r"Verify"))).to_have_count(0)
    expect(desk.locator(".ev-pagehead").get_by_role("link", name=re.compile(r"Verify"))).to_have_count(0)
    desk.goto(f"{stack['base']}/claims/{claim['id']}")
    link = desk.get_by_role("link", name="Verify this package")
    expect(link).to_have_attribute("href", "/verify")
    expect(link).to_have_attribute("target", "_blank")


# --- T57: System health shows the accuracy measured by the E1 set ---


def test_system_health_shows_the_measured_accuracy(stack, desk):  # noqa: F811
    desk.goto(stack["base"] + "/system")
    card = desk.get_by_role("region", name="Model accuracy")
    expect(card.get_by_role("heading", name=re.compile(r"How well the AI reads photos"))).to_be_visible()
    row = card.get_by_role("row").filter(has_text="Reads the license plate exactly")
    expect(row).to_contain_text(re.compile(r"\d+% \(\d+ of \d+\)"))
    expect(card.get_by_text("(lower is better)").first).to_be_visible()
    shot(desk, "56-system-health-accuracy")
