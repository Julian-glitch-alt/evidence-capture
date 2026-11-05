# Verified photo evidence for auto insurance claims

Insurers send the claimant a link; every photo is submitted through a guided capture flow in the phone's browser and checked against the policy and the claimant's story before it can be used, then sealed and returned as a report the adjuster decides on.
The adjuster reviews the sealed report in the Console, decides, and the decision is written back into the insurer's claims system, which stays the system of record.

> **What “verified” means here: capture-flow verification.** The checks below test whether a photo arrived through the capture flow, in an open session, for the right claim and vehicle, and whether the sealed package was altered after the server received it. A browser link cannot prove a photo came from a physical camera: a virtual camera or a photo of a screen is flagged for the adjuster, not blocked. See [Security and capture-flow verification](#security-and-capture-flow-verification) and [What is not built](#what-is-not-built).

> **Built with Meta AI.** At runtime, Muse Spark reads every photo and statement and SAM 3.1 outlines the damage (see [Meta models](#meta-models)). The code was written by a solo developer working with AI coding agents: **Muse Code** (Meta) and **Claude Code** (Anthropic). Live demo: https://phamdinhluu0712--evidence-capture.us-east.modal.run

## Five layers

The model is only the innermost layer. Four more layers are what make its output usable in an insurance claim:

```
┌──────────────────────────────────────────────────────────┐
│ 5. Foundation: backend, Postgres, frontend, tests, CI    │
│ ┌──────────────────────────────────────────────────────┐ │
│ │ 4. Surfaces: capture link, console, Verify, API      │ │
│ │ ┌──────────────────────────────────────────────────┐ │ │
│ │ │ 3. Trust: liveness frames, nonce, upload sniff,  │ │ │
│ │ │    Ed25519 seal, hash-chained event log          │ │ │
│ │ │ ┌──────────────────────────────────────────────┐ │ │ │
│ │ │ │ 2. Decision: coverage, damage merge,         │ │ │ │
│ │ │ │    log-odds fusion, cost-based lanes         │ │ │ │
│ │ │ │ ┌──────────────────────────────────────────┐ │ │ │ │
│ │ │ │ │ 1. AI core: Muse Spark + SAM 3.1, inside │ │ │ │ │
│ │ │ │ │ a harness: gateway, guardrails,          │ │ │ │ │
│ │ │ │ │ self-correction, tracing, evals          │ │ │ │ │
│ │ │ │ └──────────────────────────────────────────┘ │ │ │ │
│ │ │ └──────────────────────────────────────────────┘ │ │ │
│ │ └──────────────────────────────────────────────────┘ │ │
│ └──────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

| Layer | Role | One line |
| --- | --- | --- |
| 1. AI core | The model reads photos and text; a harness keeps it right | The AI reads |
| 2. Decision | Code turns readings into conclusions with reasons | Code decides |
| 3. Trust | Checks capture-flow compliance and makes post-receipt tampering detectable | Capture flow checked, seal verifiable |
| 4. Surfaces | What claimant, adjuster and insurer system touch | What people see |
| 5. Foundation | Runs reliably, with tests and CI | It runs for real |

Layer 1 is deliberately a **workflow, not an autonomous agent**: fixed steps, no tool calling, no open-ended planning. Layer 3 is what makes this product different: every other team has AI; very few bind each photo to a capture session and seal the result so anyone can check it afterwards.

## AI engineering techniques, all in the code

Current AI-engineering practice, applied to one narrow problem. every row below is implemented in the server code in this repository.

| Technique | What it is here |
| --- | --- |
| **Guardrails** | Fourteen checked layers between any model output and any decision (task decomposition, constrained output, abstention, grounding, limited authority, and more) |
| **Structured outputs** | Every call returns JSON against an exact shape; a malformed answer becomes "unchecked", never a pass |
| **Evals / eval-driven development** | A 72-photo golden set; wrong answers and refusals counted separately, Wilson intervals, McNemar for prompt-to-prompt comparisons, detection measured at a fixed false-alarm rate |
| **Self-consistency** | The VIN is read twice with two differently worded prompts and the reads must agree before the ISO 3779 check digit is even tried; the plate gets the same treatment from a cropped re-read |
| **Reflection / self-correction** | A rejected AI summary is sent back once, with the rejection reasons, for the model to rewrite |
| **Verifier / critic** | Code scores every sentence the model writes: citations, numbers, polarity, forbidden words |
| **Grounding / citations** | Every summary sentence must cite a real fact key; invented keys fail verification |
| **Multimodal pipeline** | Muse Spark reads and boxes the damage, SAM 3.1 segments inside the box, code measures the area; disagreement is a review note, not a verdict |
| **Context engineering** | Later steps never see raw photos, only the merged, numbered damage list plus keyed facts |
| **Human-in-the-loop** | The adjuster makes every final decision and labels each flag "confirmed" or "false alarm" |
| **Prompt-injection defence** | Claimant text and photos are fenced data, fake fence tags are stripped, and the model has no tools and no authority |
| **Observability / tracing** | Every model call is logged with tokens, cost and latency; every conclusion traces back to the exact call (`call_id`) that produced it |
| **Cost control** | A content-hash replay cache, plus per-claim and per-day budget caps that act as circuit breakers |
| **Uncertainty / abstention** | The model may answer "unsure"; a signal that did not run is recorded as "missing", never as zero |
| **Content provenance** | Server-attested Ed25519 signature plus a hash-chained event log in every sealed package, verifiable offline. Not C2PA: there is no camera-side provenance or third-party timestamp |
| **Event sourcing / audit log** | Append-only, hash-linked event history; deletion is itself an event with a receipt |
| **Bayesian evidence fusion** | Log-odds added per root cause, related causes capped together, nothing double-counted, model-only causes capped at 0.3 and never enough alone |
| **Workflow, not an agent** | Five standard patterns (routing, evaluator-optimizer, bounded retries) and deliberately no autonomous agent |

Deliberately **not** used, and not claimed anywhere: RAG, vector databases, fine-tuning, multi-agent systems, tool calling, MCP, agent memory, and calibration — the evidence weights are documented assumptions, not fitted probabilities.


## The problem

- In a 2026 Verisk survey (a vendor survey), 36% of US consumers said they would consider editing a claim photo or document (55% of Gen Z); 99% of claims staff have seen altered or AI-made documents, yet only 32% of carriers feel confident detecting them.
- UK insurers Allianz and Zurich reported a 300% rise in app-edited vehicle photos in 2024.
- The industry asks "is this photo fake?", a race detectors keep losing as editing tools improve. The question with a lasting answer is: was this photo submitted through the capture flow, of this car, matching this policy and this story?
- Bad or missing photos are also the main delay in photo claims: chasing the claimant, retakes, and sending someone out to inspect.

## The claim workflow, end to end

A motor claim moves through five steps, from the customer's first call to the insurer's final processing. This product does not replace that workflow; it automates the checking work in the middle of it — the part an adjuster does by hand today, claim after claim.

```
01. Customer reports  →  02. Insurer opens the claim  →  03. Evidence checked (this product)  →  04. Adjuster decides  →  05. Insurer processes the claim
```

| Step | What happens | Who does it |
| --- | --- | --- |
| 01. Customer reports | The customer has an accident or finds damage, calls the insurer, and describes what happened. This first notice of loss (FNOL) starts everything. | Customer + insurer call centre |
| 02. Insurer opens the claim | The insurer creates the claim in its own claims system, attaches the policy, and assigns a claim number. | Insurer |
| 03. Evidence is collected and checked | The insurer orders a verified capture. The claimant gets a link, submits photos through the guided capture flow, and every photo is checked as it arrives: right vehicle, right policy, declared damage visible, nothing flagged as a static or re-used source. The package is then sealed and analysed — damage read, statement compared with the photos, old damage flagged, a lane and an initial reserve proposed. | This product (capture link + automated checks) |
| 04. Adjuster decides | The adjuster opens the finished report — photos with every flag and its reason, the checks that passed, and the sealed package — reviews it, and decides: approve, send back for re-capture, or escalate to a person. | Adjuster, in the Console |
| 05. Insurer processes the claim | The decision and the report are written back into the insurer's claims system, which stays the system of record and carries the claim forward (repair, payment, closure). | Insurer's claims system |

Today, steps 03–04 are manual: the adjuster opens each submission, looks at every photo one by one, hunts for anything unusual, and cross-checks the photos against the policy and the customer's story. It is slow, repetitive, and easy to rush. This product automates exactly that checking and evaluation work: the adjuster no longer inspects raw photos; they review a checked, sealed report where every problem is already flagged with its evidence.

The **Console is the adjuster's new AI workspace**: the claim queue, the checked photos with their flags, the sealed report, and the decision controls in one place. The product recommends; the adjuster decides. Nothing is auto-approved or auto-rejected, and the insurer's claims system remains the system of record.

## What it does

1. The insurer opens the claim in its own system and orders a capture-flow verification, through the API or a short console form.
2. The claimant gets the link (the insurer sends it by text or email; the console also shows a QR code and a copy button) and captures in the phone's browser. They say what happened in their own words and confirm the claim type the model suggests, or pick it from a list. The capture page only offers the in-page camera and has no gallery picker — this is capture-flow verification, not proof of a physical camera. Direct API uploads, a virtual camera, or a photo of a screen can still reach the server; the checks in the next section flag them for the adjuster. Only a native app with App Attest / Play Integrity, or camera-side C2PA provenance, can close that gap.
3. While capturing, each photo is read and code-checked: the VIN (read twice, check digit, matched to the policy), the plate and odometer against the policy, blur and glare, and whether the photo shows the declared damage. A failed check asks for a retake on the spot.
4. The package is sealed (server-attested Ed25519 signature with a hash-chained event log; anyone holding the package can verify offline that the files are the ones the server received, with `tools/verify_package.py`; see also `server/tools/verify_package.py`). In the background the service reads the damage, compares the statement with the photos, flags old damage, and proposes a lane with an initial reserve.
5. The result lands in the claim file: the report PDF and sealed package under Documents, a 2–3 line note under Notes, and a "Review verified evidence, due in 2 business days" activity. After the adjuster decides, the decision is written back as a note.

```mermaid
flowchart LR
    A[Insurer orders capture] --> B[Claimant link: capture in this page + photo checks]
    B --> C[Seal: signature + hash-chained log]
    C --> D[Analysis: damage, statement, flags, lane]
    D --> E[Report and decision]
    E --> F[Insurer's claims system]
```

The insurer's system stays the system of record: this product recommends, the adjuster decides, and it never sets reserves or moves money.

## Why it is different

We do not ask "is this photo fake?". We ask "was it submitted through the capture flow, of this car, matching this policy and this story?" — and we show the adjuster every flag where the answer is unclear.

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

The model never decides a claim. When it cannot read a photo, it asks the claimant for another shot; whatever it flags goes to the adjuster. So a miss comes in two kinds: a **wrong answer**, which could mislead the adjuster, and a **refusal**, which costs the claimant one more photo. The tables keep them apart.

Measured on 3 Nov 2026 with Muse Spark Open on Fireworks, the model the live server runs. Hits / n with 95% Wilson intervals; every set is small, so read the range.

### Wrong answers

| Wrong answer (lower is better) | Result | 95% range |
| --- | --- | --- |
| VIN read wrong | 0 / 6 = 0% | 0% to 39% |
| Odometer read wrong | 0 / 7 = 0% | 0% to 35% |
| Damaged area a person marked, missed | 0 / 21 = 0% | 0% to 15% |
| Undamaged car called damaged | 0 / 9 = 0% | 0% to 30% |
| Planted flag the checks missed | 0 / 6 = 0% | 0% to 39% |
| License plate read wrong | 3 / 20 = 15% | 5% to 36% |

The three wrong plates were two antique plates read with a repeated digit and one messaging-app copy read with one wrong letter. A wrong plate does not pass silently: it fails the match against the policy and reaches the adjuster as a flag.

### Reads and refusals

| Field | Read right | Refused, asked for another shot | Read wrong |
| --- | --- | --- | --- |
| License plate | 15 / 20 | 2 (a distant photo, an antique plate with no state name) | 3 |
| VIN | 3 / 6 | 3 (our synthetic windshield-style images, no car around the plate) | 0 |
| Odometer | 7 / 7 | 0 | 0 |

Every plate read from a modern close-up was right, and every VIN the model read was right.

### Photo quality, damage and flags

| What was checked | Result | 95% range |
| --- | --- | --- |
| Asks for another shot of a blurred, dark, wrong, or unreadable photo | 25 / 25 = 100% | 87% to 100% |
| Names the right kind of damage | 21 / 21 = 100% | 85% to 100% |
| Names the marked area or the one next to it | 32 / 37 = 86% | 72% to 94% |
| Rates severity the way a person did (otherwise one step off) | 14 / 21 = 67% | 45% to 83% |
| Asks for another shot of a good photo (lower is better) | 5 / 47 = 11% | 5% to 23% |
| Share of flags on planted claims that were the planted ones | 6 / 13 = 46% | 23% to 71% |

Severity only feeds the rough repair range, which the adjuster confirms. The extra flags came mostly from one reused public photo read as old damage, which is defensible for that photo; every planted claim went to adjuster review, none to fast track.

The set is 72 photos and 6 planted claims, public or synthetic, not a production benchmark. The full run took 126 model calls, about $0.58 with an empty cache; the median call took 10 s, the 95th percentile 24 s.

Full report: [server/eval/e1-report-2026-11-03.md](server/eval/e1-report-2026-11-03.md).

## What is built

| Part | What it is |
| --- | --- |
| Claimant link | Guided capture in the phone browser: in-page camera only, no gallery button. Capture-flow verification only — a virtual camera is flagged, not blocked (see below). |
| Checks before signing | Each photo is read and code-checked during capture; a failed check asks for a retake, and a required photo that still fails or is skipped (with a reason) is flagged for the adjuster. |
| Sealed package | Server-attested Ed25519 signature plus a hash-chained event log (`events.jsonl` in the zip); anyone holding the package can verify offline that the files are the ones the server received, with `tools/verify_package.py`. No third-party timestamp and no camera-side provenance. |
| Review desk | Queue, claim view with the pipeline, findings with evidence, decisions, and reviewer feedback labels. |
| Report PDF | A verified-evidence report with the sealed package, per claim. |
| Signed webhooks | Evidence and decision events pushed to the insurer's system. |
| Mock claims system | A demo insurer claims system showing the full round trip (order, link, result, decision). |
| System health | Failed checks, deliveries, model calls, and the measured accuracy line. |

## Security and capture-flow verification

Layer 3 in code, not just in prose. Full detail: [docs/threat-model.md](docs/threat-model.md).

**Capture session and upload integrity**

- Each photo needs a single-use nonce for that slot inside an open capture session (`server/src/evidence_server/capture.py`). A replayed nonce carrying different bytes is refused; a retry with the same bytes returns the earlier result.
- The server does not trust the declared file type: it sniffs the magic bytes and, for images, decodes with Pillow (`Image.open` + `load`). Bytes that do not match the declared MIME type or do not decode are rejected before any model call.
- Upload, body, and package sizes are capped before parsing, and the sealed-package check hashes files in chunks.

**Static-source and timing flags (signals for the adjuster, never a block or a verdict)**

- With each photo the page sends two downscaled frames (160 px wide JPEG) grabbed from the same video stream about 300 ms before and after the shot. The server compares them with the shot (downscaled mean absolute pixel difference). Missing frames, undecodable frames, or frames near-identical to the shot add a `static_source` flag, shown next to the photo in the report PDF and the loss notice with its reason.
- Per item the server records its own facts: `received_at` on the server clock, the client-reported capture time labelled client-reported and untrusted, and the nonce-issued-to-upload latency. Latency over 60 s is flagged as `slow_upload`. A direct API upload therefore shows up as missing or static frames and, usually, slow latency — visible, not silently accepted.
- Photos go through `canvas.toBlob`, so EXIF is stripped by design and the server keeps none. Times an adjuster can rely on are the server's; the client's clock is a claim, never a fact.

**Seal and audit log**

- On submit the server seals what it received: an Ed25519 signature over a manifest that names every file hash, the answers, the policy snapshot, and the event-chain head and count, plus the signing key (`key_id`). The zip carries `events.jsonl`: every claim event with `seq`, `prev_hash`, and `hash`, so anyone can recompute the chain and compare it with the manifest.
- Verification is offline and key-aware: `tools/verify_package.py` (also `server/tools/verify_package.py`) picks the key named by `manifest.key_id` from the current key plus any old public keys in `SEAL_OLD_PUBLIC_KEYS`, so key rotation does not break old packages and a re-signed package names its key.
- In the database the event log is append-only (UPDATE and DELETE raise) and each event hashes the previous one. Media removal writes a signed removal receipt; the fingerprints stay in the manifest, so Verify still passes and says what was removed and why.

**Transport, webhooks, and accounts**

- Capture and report links are unguessable tokens; report and package responses carry private, `no-store` headers.
- Callback and webhook URLs must be `https://` and on the deployment's allowed host list (or the server's own host). `http://` is allowed only in test mode for localhost / 127.0.0.1 or the server's own host. Webhooks are signed (`X-Evidence-Signature`, HMAC over timestamp and body) and deliveries are retried from the outbox, never silently dropped.
- Adjuster sessions are signed cookies. Login is throttled per IP (5 failures in 15 minutes) and per account (10 failures for one email in 1 hour, then 429 for that email), both stored in Postgres, and `ADMIN_EMAIL` must be an email address. The server only trusts forwarded proxy headers from `FORWARDED_ALLOW_IPS` (default `127.0.0.1`), so a client cannot spoof its address through `X-Forwarded-For` unless the deployment allows it.

None of this proves a browser photo came from a physical camera. It raises the cost of fakes, binds each upload to a session and a slot, and makes alteration after receipt detectable. The gaps that remain are listed next, without softening.

## What is not built

- No native app, so a virtual camera is not blocked and a photo of a screen is not blocked. The link raises the cost of fakes and flags static or slow sources; the full fix needs a native app with App Attest / Play Integrity, or camera-side C2PA provenance a web link cannot provide.
- Direct API uploads can bypass the capture page: the server flags them (missing or static liveness frames, slow nonce-to-upload latency), but closing the gap needs a native app with App Attest / Play Integrity or C2PA provenance.
- The seal is server-attested: the server signs what it received, with no third-party timestamp yet, so it does not prove when a photo was taken to an outside party, and it proves nothing about what the photo shows.
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
