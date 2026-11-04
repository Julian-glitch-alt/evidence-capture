# Threat model

What this product protects, against whom, and what it does not protect yet.
Wording matters: the capture page only offers the camera in this page. It does
not make uploads from elsewhere impossible, and nothing here claims it does.

## Assets

- Claim evidence: photos, video, the signature image, the claimant's answers,
  and the policy snapshot as it stood when the claim opened.
- The sealed package: `manifest.json`, `signature.txt`, `public_key.txt`,
  `answers.json`, `policy_snapshot.json`, `events.jsonl` (every claim event
  with `seq`, `prev_hash` and `hash`) and the sealed files. The manifest names
  the event chain head (`event_chain_head`) and count (`event_count`) and the
  signing key (`key_id`).
- Adjuster accounts and sessions, API keys (live and test), the webhook
  secret, and the delivery outbox.
- The sealing private key. Whoever holds it can make packages that verify.

## Attackers

### Dishonest claimant

Goal: make the adjuster pay on evidence that was edited, borrowed, staged or
taken of another vehicle, or coach the answers after seeing a hint.

What helps:

- The capture page only offers the camera in this page; there is no option
  to pick existing photos. That raises the cost, it does not end the game.
- Each shot needs a single-use nonce for that slot inside an open capture
  session. A replayed nonce with different bytes is refused.
- Uploads are not trusted by their declared type: the server sniffs the magic
  bytes and, for images, decodes with Pillow (`Image.open` + `load`). Bytes
  that do not match the declared mime or do not decode are rejected.
- With each photo the page sends two downscaled frames (160 px wide JPEG)
  grabbed from the same video stream about 300 ms before and after the shot.
  The server compares them with the shot (downscaled mean absolute pixel
  difference). Missing frames, undecodable frames, or frames near-identical
  to the shot add a `static_source` flag shown to the adjuster with its
  reason. It is a flag, never a block.
- The server records, per item, the time it received the file, the
  client-reported capture time (labelled client-reported, untrusted), and
  the nonce-issued-to-upload latency. Latency over 60 s is flagged as
  `slow_upload`.
- Photo checks read the plate, VIN and odometer and compare them in code
  against the policy and the claimant's answers; earlier mismatched shots
  stay visible to the adjuster as a retake trail.
- On submit the server seals what it received and appends every step to the
  hash-chained event log, so later edits to the stored log break the chain.

Known gap: a determined claimant can call the API directly, feed a virtual
camera, or photograph a screen. The liveness and latency flags make that
visible, they do not stop it. Closing this gap needs a native app with
App Attest / Play Integrity, or C2PA provenance from the camera, none of
which a web link can provide.

### Network attacker

Goal: read or change evidence in transit, steal a session, guess a link, or
make the server call an internal address (SSRF through the callback URL).

What helps:

- Capture links and report links are unguessable tokens; report and package
  links carry private, no-store headers.
- Callback URLs must be `https://` and on the deployment's allowed host list
  (or the server's own host). `http://` is allowed only in test mode for
  localhost / 127.0.0.1 or the server's own host.
- Webhooks are signed (`X-Evidence-Signature`, HMAC over timestamp and body)
  and deliveries are retried from the outbox, never silently dropped.
- Adjuster sessions are signed cookies; login is limited per IP (5 failures
  in 15 minutes) and per account (10 failures for one email in 1 hour, then
  429 for that email), both stored in Postgres.
- Upload size, body size and package size are capped before parsing, and the
  sealed-package check hashes files in chunks.
- The server only trusts forwarded proxy headers from
  `FORWARDED_ALLOW_IPS` (default `127.0.0.1`), so a client cannot spoof its
  address through `X-Forwarded-For` unless the deployment allows it.

### Operator (including us)

Goal, by malice or mistake: change evidence after the fact, re-sign a
package, read trial claims from another company, or keep files too long.

What helps:

- The event log is append-only in the database (UPDATE and DELETE raise) and
  each event hashes the previous one. The seal signs the chain head and
  count, and `events.jsonl` in the package lets anyone recompute the chain
  and compare it with the manifest.
- A package verifies offline with `tools/verify_package.py`
  (`python verify_package.py package.zip --public-key <b64>`), choosing the
  key by `manifest.key_id` from the current key plus any old public keys in
  `SEAL_OLD_PUBLIC_KEYS`, so key rotation does not break old packages and a
  re-signed package names its key.
- Media removal writes a signed removal receipt; the fingerprints stay in
  the manifest, so Verify still passes and says what was removed and why.
- Retention deletes photos, video and report files 90 days after the
  decision by default (the insurer can set 30 to 3650 days).

Known gaps: the seal is server-attested. The server signs what it received;
there is no third-party timestamp yet, so the package proves the files are
the ones the server received, not — to an outside party — the exact moment
they were taken. All trial users currently share one workspace and can see
each other's trial claims.

## What each layer protects

| Layer | Protects | Does not prove |
| --- | --- | --- |
| Capture page (camera only, nonce, session code) | That a shot was taken in this page, in an open session, for that slot | That the camera pointed at a real vehicle (virtual cameras, screens) |
| Upload validation (magic bytes + Pillow decode) | That the bytes are really the declared image or video type and decode | That the picture is unedited |
| Liveness frames + nonce latency | A flag when the source looks static or the upload was slow | Liveness of a person; it is a signal for the adjuster |
| Photo and coverage checks (code) | That readable plate, VIN and odometer match the policy and answers | That damage photos show the same vehicle |
| Event log (`events.jsonl`, hash chain) | That the recorded history was not reordered, edited or truncated without breaking the chain or the seal | That an event's contents were true when written |
| Seal (Ed25519 over the manifest) | That files, answers, snapshot and the event head are the ones the server received | That a photo shows what it claims to show, or a third-party time |
| Report PDF and Loss Notice flags | That a flagged item (`static_source`, `slow_upload`) is shown next to the item, not under a neutral caption | — |

## EXIF

Photos go through `canvas.toBlob`, so EXIF is stripped by design: the files
the server receives carry no camera EXIF, and the server keeps none. The
server instead records its own facts per item: `received_at` (server clock),
the client capture timestamp from `client_meta` labelled client-reported and
untrusted, and the nonce-to-upload latency. Times an adjuster can rely on
are the server's; the client's clock is a claim, never a fact.

## Known gaps (summary)

- Direct API upload bypass needs a native app with App Attest / Play
  Integrity or C2PA; the web link flags it, it cannot close it.
- The seal is server-attested, with no third-party timestamp yet.
- A virtual camera is not blocked without a native app.
- Screen re-capture is a model opinion, not a pixel test.
- One workspace is shared by all trial users.
- Accuracy is measured on public and synthetic photos, not real claims.
