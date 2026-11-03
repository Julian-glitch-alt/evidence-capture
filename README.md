# Verified photo evidence for auto insurance claims

Insurers send the claimant a link; every photo is captured live in the phone's browser and checked against the policy and the claimant's story before it can be used, then sealed and returned as a report the adjuster decides on.
The adjuster keeps working in the claims system; this product delivers the verified evidence there.

## The problem

- In a 2026 Verisk survey (a vendor survey), 36% of US consumers said they would consider editing a claim photo or document (55% of Gen Z); 99% of claims staff have seen altered or AI-made documents, yet only 32% of carriers feel confident detecting them.
- UK insurers Allianz and Zurich reported a 300% rise in app-edited vehicle photos in 2024.
- The industry asks "is this photo fake?", a race detectors keep losing as editing tools improve. The question with a lasting answer is: was this photo taken live, of this car, matching this policy and this story?
- Bad or missing photos are also the main delay in photo claims: chasing the claimant, retakes, and sending someone out to inspect.

## What it does

1. The insurer opens the claim in its own system and orders a verified capture, through the API or a short console form.
2. The claimant gets the link (the insurer sends it by text or email; the console also shows a QR code and a copy button) and captures in the phone's browser. They say what happened in their own words and confirm the claim type the model suggests, or pick it from a list. Photos come only from the live camera; there is no gallery button.
3. While capturing, each photo is read and code-checked: the VIN (read twice, check digit, matched to the policy), the plate and odometer against the policy, blur and glare, and whether the photo shows the declared damage. A failed check asks for a retake on the spot.
4. The package is sealed (Ed25519 signature with a hash-chained event log anyone can verify). In the background the service reads the damage, compares the statement with the photos, flags old damage, and proposes a lane with an initial reserve.
5. The result lands in the claim file: the report PDF and sealed package under Documents, a 2–3 line note under Notes, and a "Review verified evidence, due in 2 business days" activity. After the adjuster decides, the decision is written back as a note.

```mermaid
flowchart LR
    A[Insurer orders capture] --> B[Claimant link: live capture + photo checks]
    B --> C[Seal: signature + hash-chained log]
    C --> D[Analysis: damage, statement, flags, lane]
    D --> E[Report and decision]
    E --> F[Insurer's claims system]
```

The insurer's system stays the system of record: this product recommends, the adjuster decides, and it never sets reserves or moves money.

## Why it is different

We do not ask "is this photo fake?". We ask "was it taken live, of this car, matching this policy and this story?".

- The AI proposes: it reads photos, points at damage, and raises flags, each with the photo and the reason attached.
- Code and humans decide: check digits, policy matching, and totals are re-checked by code, and the adjuster makes the call.
- Nothing is auto-rejected: a mismatch sends the claim back for re-capture or to a person, never to an automatic denial.
- Small carriers can stay in the console and email; larger ones get the API and webhook into the claim file.

## Meta models

Every model call goes through one client with a replay cache, timeouts, retries, and per-claim and per-day budget caps.

- **Muse Spark** reads and reasons, always returning JSON that code checks: plates, VINs and odometers; whether each photo shows the declared damage, and screen or print re-capture cues; the damage itself (area, type, severity, new or old); whether the claimant's statement fits that damage; the report's short AI summary, which code verifies against the checks before it is shown; and, on the first screen, the closest claim type for what the claimant wrote (a suggestion the claimant confirms; nothing is saved until they do).
- **SAM 3.1** draws the damage outline: Muse Spark boxes the damage, SAM 3.1 segments inside the crop, and code measures the area. When the two disagree, that is a review note, never a fraud signal on its own. This step is built and tested but **off by default**, and it is always off when `MODEL_PROVIDER=fireworks`: SAM 3.1 is reached through the Meta Model API only.
- `MODEL_PROVIDER` switches hosts: `meta` uses the Meta Model API (Muse Spark 1.3); `fireworks` uses Fireworks (Muse Spark Open). The live demo runs on Fireworks with zero data retention (switched over on 3 Nov), so it does not run the SAM outline.
- SAM outline calls per claim are capped by `META_SAM_MAX_CALLS` (default `0`, which skips the step); the product works on Muse Spark alone.
- The evaluation below used prompts `prompts/v4`, `damage-prompts/v3`, and `consistency-prompts/v3`.

## How well the AI reads photos

Hits / n with 95% Wilson intervals, measured on 3 Nov 2026 with Muse Spark Open on Fireworks:

| What was checked | Result | 95% range |
| --- | --- | --- |
| Reads the license plate exactly | 15 / 20 = 75% | 53% to 89% |
| Reads the VIN exactly | 3 / 6 = 50% | 19% to 81% |
| Reads the odometer exactly | 7 / 7 = 100% | 65% to 100% |
| Asks for another shot of a blurred, dark, wrong, or unreadable photo | 25 / 25 = 100% | 87% to 100% |
| Asks for another shot of a good photo (lower is better) | 5 / 47 = 11% | 5% to 23% |
| Finds each damaged area a person marked | 21 / 21 = 100% | 85% to 100% |
| Names the marked area or the one next to it | 32 / 37 = 86% | 72% to 94% |
| Names the right kind of damage | 21 / 21 = 100% | 85% to 100% |
| Rates severity the way a person did | 14 / 21 = 67% | 45% to 83% |
| Calls an undamaged car damaged (lower is better) | 0 / 9 = 0% | 0% to 30% |
| Raises the planted flag on a claim with a planted problem | 6 / 6 = 100% | 61% to 100% |
| Share of flags on those claims that were planted | 6 / 13 = 46% | 23% to 71% |

The set is 72 photos and 6 planted claims, public or synthetic, not a production benchmark.

When a VIN was read, it was right (3 / 3); the three windshield-style synthetic VIN images were refused as not a VIN. The full set took 126 model calls, about $0.58 with an empty cache; the median call took 10 s, the 95th percentile 24 s.
Extra flags on the planted claims came mostly from one reused public photo read as old damage, which is defensible for that photo. Every planted claim went to adjuster review; none went to fast track.

Full report: [server/eval/e1-report-2026-11-03.md](server/eval/e1-report-2026-11-03.md).

## What is built

| Part | What it is |
| --- | --- |
| Claimant link | Guided capture in the phone browser, live camera only, no gallery button. |
| Checks before signing | Each photo is read and code-checked during capture; a failed check asks for a retake, and a required photo that still fails or is skipped (with a reason) is flagged for the adjuster. |
| Sealed package | Ed25519 signature plus a hash-chained event log; anyone holding the package can verify it. |
| Review desk | Queue, claim view with the pipeline, findings with evidence, decisions, and reviewer feedback labels. |
| Report PDF | A verified-evidence report with the sealed package, per claim. |
| Signed webhooks | Evidence and decision events pushed to the insurer's system. |
| Mock claims system | A demo insurer claims system showing the full round trip (order, link, result, decision). |
| System health | Failed checks, deliveries, model calls, and the measured accuracy line. |

## What is not built

- No native app, so a virtual camera is not blocked. The link raises the cost of fakes; the full fix needs an app.
- Screen re-capture is the model's opinion, not a pixel test. Pixel-level forensics were dropped on purpose.
- One workspace is shared by all trial users, who can see each other's claims.
- No shared photo pool across insurers.
- The mock claims system is a demo, not a real integration. Real connectors come later.
- Accuracy is measured on public and synthetic photos, not on real claim photos.
- No vendor security paperwork yet.

## Try it

- Live demo: https://phamdinhluu0712--evidence-capture.us-east.modal.run
- **Try the demo** opens a claim on the sample policy `DEMO-AUTO-1001` (2020 Honda Accord, plate `DEMO101`). Photograph any other car and the claim is flagged as a different vehicle: that is the check working.
- **Open a claim** builds a trial policy from your own car's details (plate, VIN, year, make, model); photos are checked against that car. Do not use customer data.
- The claimant needs only the link: no app install, no login.
- Four sample policies ship in the seed data (`DEMO-AUTO-1001` to `DEMO-AUTO-1004`): `DEMO-AUTO-1003` carries liability only, and `DEMO-AUTO-1004` is expired, for testing those paths.
- Trial users share one workspace and see each other's claims.

## Run it locally

Install `uv` and Node 20. Tests need a Postgres database (the tests default to `postgresql://postgres:test@127.0.0.1:55432/claims_test`). From the repository root:

```bash
docker run -d --name evidence-test-pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=claims_test -p 127.0.0.1:55432:5432 postgres:16
(cd server && uv sync && uv run pytest -q -p no:cacheprovider)
(cd app && npm ci && npm run build && npm run lint)
```

Browser tests, after the app is built:

```bash
cd server
uv run --with playwright==1.64.1 python -m playwright install --with-deps chromium
E2E_CHANNEL=chromium uv run --with playwright==1.64.1 python -m pytest tests_e2e -q -rs -p no:cacheprovider
```

Run the stack locally, in two terminals:

```bash
cd server && set -a && . ../.env && set +a && uv run evidence-server
cd app && npm run dev
```

The app serves on `http://localhost:5173` and proxies `/api` to `:8000`.

Deploy (needs `app/dist` built first):

```bash
cd app && npm run build
cd server && uv run modal deploy deploy/modal_app.py
```

## Repository map

| Folder | What lives there |
| --- | --- |
| `app/` | Claimant PWA and adjuster console (React + TypeScript, Tailwind). |
| `server/` | FastAPI backend: claims, capture, checks, seal, webhooks, and the deploy app. |
| `data/` | Runtime and evaluation data, kept out of git (for example the E1 photo set). |
| `server/scripts/` | Smoke, load, evaluation and live-check scripts (for example `e1_eval.py`, `live_check.py`). |
| `.github/` | CI: server tests on Postgres, app build and lint, browser tests. |

## Licences

This repository is source-available under the [PolyForm Noncommercial License 1.0.0](LICENSE): anyone may read, run and modify it for noncommercial purposes, including evaluation and research. Commercial use needs a separate licence from the author.

Third-party notices live in `server/licenses/`, including the SAM License covering the ported SAM 3.1 mask decoder (`server/licenses/SAM-LICENSE.txt`).
