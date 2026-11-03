"""Fixed post-deploy check: the same checks against the deployed app, every time.

    uv run --project server --with playwright==1.64.1 python server/scripts/live_check.py --base-url URL [--claim CLM-...] [--out DIR]

Needs ADMIN_PASSWORD in the environment (ADMIN_EMAIL defaults to "admin").
Prints one PASS / FAIL / WARN line per step and exits 1 if any step failed.
Screenshots go to --out (default server/eval/out/live-<UTC timestamp>/, gitignored).
"""

import argparse
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = ROOT / "server" / "eval" / "out" / f"live-{datetime.now(UTC):%Y%m%dT%H%M%SZ}"


class Check:
    """One PASS / FAIL / WARN line per step; remembers whether anything failed."""

    def __init__(self) -> None:
        self.failed = False

    def report(self, name: str, status: str, detail: str = "") -> None:
        print(f"{status}  {name}" + (f"  ({detail})" if detail else ""), flush=True)
        if status == "FAIL":
            self.failed = True


def step_bundle(check: Check, http: httpx.Client) -> str | None:
    """The bundle named in the served / equals the one in the local app/dist/assets."""
    try:
        r = http.get("/")
        r.raise_for_status()
    except Exception as e:
        check.report("served bundle matches local build", "FAIL", f"GET / failed: {e}")
        return None
    m = re.search(r'/assets/(index-[^"\']+\.js)', r.text)
    if not m:
        check.report("served bundle matches local build", "FAIL", "no /assets/index-*.js in /")
        return None
    served = m.group(1)
    local = sorted((ROOT / "app" / "dist" / "assets").glob("index-*.js"))
    if not local:
        check.report("served bundle matches local build", "WARN", f"no local build; deploy serves {served}")
        return served
    if served in {p.name for p in local}:
        check.report("served bundle matches local build", "PASS", served)
        return served
    check.report(
        "served bundle matches local build", "FAIL", f"deploy serves {served}, local has {local[0].name}"
    )
    return served


def step_cache(check: Check, http: httpx.Client, bundle: str | None) -> None:
    """/ answers Cache-Control: no-store; the bundle answers Cache-Control with immutable."""
    try:
        root_cc = http.get("/").headers.get("cache-control", "")
    except Exception as e:
        check.report("cache headers", "FAIL", f"GET / failed: {e}")
        return
    if "no-store" in root_cc:
        check.report("index is no-store", "PASS", root_cc)
    else:
        check.report("index is no-store", "FAIL", f"Cache-Control: {root_cc!r}")
    if bundle is None:
        check.report("bundle is immutable", "FAIL", "no bundle name from /")
        return
    try:
        bundle_cc = http.get(f"/assets/{bundle}").headers.get("cache-control", "")
    except Exception as e:
        check.report("bundle is immutable", "FAIL", f"GET /assets/{bundle} failed: {e}")
        return
    if "immutable" in bundle_cc:
        check.report("bundle is immutable", "PASS", bundle_cc)
    else:
        check.report("bundle is immutable", "FAIL", f"Cache-Control: {bundle_cc!r}")


def step_contact(check: Check, http: httpx.Client) -> None:
    """GET /api/contact: WARN on a missing or example address; print the model and host."""
    try:
        r = http.get("/api/contact")
        r.raise_for_status()
        body = r.json()
    except Exception as e:
        check.report("contact", "FAIL", f"GET /api/contact failed: {e}")
        return
    email = body.get("email")
    model = body.get("model") or {}
    detail = f"model {model.get('model', '?')} via {model.get('host', '?')}"
    if not isinstance(email, str) or not email:
        check.report("contact", "WARN", f"no contact email; {detail}")
    elif email.endswith("@example.com"):
        check.report("contact", "WARN", f"placeholder address {email}; {detail}")
    else:
        check.report("contact", "PASS", f"{email}; {detail}")


def run(args) -> int:
    check = Check()
    out = args.out
    out.mkdir(parents=True, exist_ok=True)
    http = httpx.Client(base_url=args.base_url.rstrip("/"), timeout=30, follow_redirects=True)

    bundle = step_bundle(check, http)
    step_cache(check, http, bundle)
    step_contact(check, http)

    from playwright.sync_api import sync_playwright

    errors: list[str] = []
    first_claim: str | None = None
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=None if args.chromium else "chrome")
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        # Console errors from the login-to-claim flow fail the check at the end (step 7).
        def on_console(msg) -> None:
            if msg.type == "error":
                errors.append(msg.text)

        page.on("console", on_console)
        logged_in = False
        try:
            # 4. Sign in as the adjuster. The console polls every 5 s, so use
            # fixed waits and element waits, never networkidle.
            page.goto(f"{args.base_url.rstrip('/')}/login")
            page.get_by_label("Email").fill(os.environ.get("ADMIN_EMAIL", "admin"))
            page.get_by_label("Password").fill(os.environ["ADMIN_PASSWORD"])
            page.get_by_role("button", name="Sign in").click()
            # A predicate checked in Python: the app's CSP (no 'unsafe-eval') refuses string predicates.
            page.wait_for_url(lambda url: "/login" not in url, timeout=15000)
            page.wait_for_timeout(2000)
            logged_in = True
            check.report("adjuster sign in", "PASS", page.url)
        except Exception as e:
            check.report("adjuster sign in", "FAIL", str(e)[:160])

        try:
            # 5. Today: the first table row with a claim number, then a screenshot.
            if not logged_in:
                check.report("today shows claims", "FAIL", "skipped: sign in failed")
            else:
                page.goto(f"{args.base_url.rstrip('/')}/")
                try:
                    page.locator(".ev-cid").first.wait_for(timeout=25000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)  # the queue reloads every 5 s; let it settle
                page.screenshot(path=str(out / "today.png"))
                ids = page.locator(".ev-cid").all_inner_texts()
                claim_no = next((t.strip() for t in ids if t.strip().startswith("CLM-")), "")
                if claim_no:
                    first_claim = claim_no
                    check.report("today shows claims", "PASS", f"first claim {claim_no}")
                else:
                    check.report("today shows claims", "PASS", "no claims")
        except Exception as e:
            check.report("today shows claims", "FAIL", str(e)[:160])

        try:
            # 6. The claim page: scroll through it, then count loaded images.
            target = args.claim or first_claim
            if not logged_in:
                check.report("claim photos", "FAIL", "skipped: sign in failed")
            elif not target:
                check.report("claim photos", "FAIL", "no claims on Today and no --claim")
            else:
                page.goto(f"{args.base_url.rstrip('/')}/claims/{target}")
                page.locator(".ev-cid", has_text=target).first.wait_for(timeout=25000)
                for _ in range(6):
                    page.mouse.wheel(0, 800)
                    page.wait_for_timeout(500)
                counts = page.evaluate(
                    "() => { const imgs = [...document.querySelectorAll('img')];"
                    " return { total: imgs.length,"
                    " loaded: imgs.filter((i) => i.complete && i.naturalWidth > 0).length }; }"
                )
                total, loaded = counts["total"], counts["loaded"]
                page.screenshot(path=str(out / "claim.png"))
                if total > 0 and loaded == 0:
                    check.report("claim photos", "FAIL", f"0 of {total} images loaded")
                else:
                    check.report("claim photos", "PASS", f"{loaded} of {total} images loaded")
        except Exception as e:
            check.report("claim photos", "FAIL", str(e)[:160])

        # 7. Any console error during steps 4-6 fails the check.
        if errors:
            check.report("no console errors", "FAIL", f"{len(errors)} console error(s)")
            for text in errors[:3]:
                print(f"    ! {text[:300]}")
        else:
            check.report("no console errors", "PASS")

        try:
            # 8. The privacy page names the model actually running.
            page.goto(f"{args.base_url.rstrip('/')}/privacy")
            page.get_by_role("heading", name="Privacy notice").wait_for(timeout=15000)
            check.report("privacy notice", "PASS", page.url)
        except Exception as e:
            check.report("privacy notice", "FAIL", str(e)[:160])

        browser.close()
    return 1 if check.failed else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://localhost:8000")
    parser.add_argument("--claim", default=None, help="claim number to open (default: first claim on Today)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--chromium", action="store_true", help="bundled Chromium instead of system Chrome")
    args = parser.parse_args()
    if "ADMIN_PASSWORD" not in os.environ:
        sys.exit("ADMIN_PASSWORD is not set")
    sys.exit(run(args))


if __name__ == "__main__":
    main()
