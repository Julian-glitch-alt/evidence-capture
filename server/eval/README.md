# Evaluation data (E1 / E2)

- `fixtures/`: a tiny synthetic set (placeholder images + recorded model answers in `replay.json`) that runs in CI. It tests the harness, not the model.
- `out/`: run outputs (gitignored).
- Real labelled photos live outside the repo (`data/e1/`, gitignored at the root) because they hold plates and VINs.

Runner: `server/scripts/e1_eval.py`. Latest run on the E1 photo set (public and synthetic photos): [e1-report-2026-11-03.md](e1-report-2026-11-03.md).

## Labels format

One JSON file; photo paths are relative to it.

```json
{
  "version": 1,
  "policy": "DEMO-AUTO-1001",
  "today": "2026-11-02",
  "answers": {"loss_type": "vehicle_damage", "loss_date": "2026-11-01", "vin": "1HGCV1F39LA526018",
              "driver_is_holder": "Yes", "full_name": "Jordan Avery", "damaged_areas": ["Rear bumper"],
              "prior_damage": "No", "other_vehicle": "No", "injuries": "No", "cause": "Hit an object",
              "description": "Backed into a bollard in a parking lot."},
  "photos": [
    {"id": "p01", "path": "images/plate.jpg", "slot": "plate", "expected": {"plate": "7ABC123"}},
    {"id": "p02", "path": "images/vin.jpg", "slot": "vin_photo", "expected": {"vin": "1HGCV1F39LA526018"}},
    {"id": "p03", "path": "images/odo.jpg", "slot": "odometer", "expected": {"odometer": 56947}},
    {"id": "p04", "path": "images/close.jpg", "slot": "damage_closeup",
     "expected": {"damages": [{"area": "Rear bumper", "type": "BUMPER_DAMAGE", "severity": "moderate", "age": "new"}]}},
    {"id": "p05", "path": "images/clean.jpg", "slot": "damage_wide", "expected": {"damages": []}}
  ],
  "claims": [
    {"id": "e2-01", "photos": ["p04", "p05"], "note": "planted: story says front, photos show rear",
     "answers": {"description": "Hit a pole with the front left corner.", "damaged_areas": ["Front left"]},
     "expected_flags": ["DAMAGE_AREA_MISMATCH", "DAMAGE_OUTSIDE_STATEMENT", "STORY_INCONSISTENT"],
     "expected_lane": "adjuster_review", "submit_codes": []}
  ]
}
```

| Key | Meaning |
|---|---|
| `policy` | a policy number from `seed/policies.json`, an inline policy object, or `null` (no policy: plate / VIN cannot be matched) |
| `today` | date for the mileage plausibility check |
| `answers` | the claimant's form, shared by every photo and claim; a photo or claim may override keys with its own `answers` |
| `photos[].slot` | one of `plate`, `vin_photo`, `odometer`, `driver_license`, `damage_closeup`, `damage_wide` |
| `expected.plate` / `vin` / `odometer` | only on the matching slot; odometer in whole miles |
| `expected.damages` | only on damage slots; `[]` means an undamaged car; area from `photo_prompts.AREAS`, type from `vocab.DAMAGE_TYPES`, severity `minor` / `moderate` / `severe`, age `new` / `old` / `unsure` (unsure is not scored) |
| `claims[].expected_flags` | every reason code a careful adjuster expects on that claim (`[]` for a clean claim) |
| `claims[].expected_lane` | optional: `siu_referral`, `adjuster_review`, `fast_track`, `standard` |
| `claims[].submit_codes` | optional: reason codes the loss notice raises at submit (e.g. `MULTI_PARTY`); the harness does not rebuild the loss notice layout |

`load_labels` rejects anything else with a clear message (10 tests).
