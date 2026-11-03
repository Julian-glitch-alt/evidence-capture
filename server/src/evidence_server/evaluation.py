"""Evaluation harness E1 / E2 (track 1, docs/explorations/ai-engineering.md).

E1: labelled photos -> how well the model reads VIN, plate and odometer, and how well
the damage reading finds damage per area (and invents none on undamaged cars).
E2: labelled claims with planted statement / photo mismatches -> flag precision and recall.

Every item goes through the SAME code as production: MetaClient.read_json with the
production prompts (photo_prompts.READS, damage_prompts.DAMAGE,
consistency_prompts.story_prompt), then photo_check.decide / view / rows,
damage.shape / view, consistency.story_block / shape_story / check, coverage.check,
estimate.rough_range and proposal.build. Only the database and the claim lifecycle
are skipped. Cost and latency come from the `model_calls` log.

A missing answer is "missing", never 0: a photo whose damage reading failed is left
out of precision / recall and counted under "not read".
"""

from __future__ import annotations

import hashlib
import io
import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path

from PIL import Image

from . import (consistency, consistency_prompts, coverage, damage, damage_prompts, estimate, outline,
               photo_check, photo_prompts, proposal, segment)
from .meta_client import MetaClient
from .photo_check import ADJACENT
from .photo_prompts import AREAS
from .vocab import DAMAGE_TYPES

VERSION = "eval/v1"
FIELDS = {"plate": "plate", "vin": "vin_photo", "odometer": "odometer"}  # labelled field -> its slot
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
        ".heic": "image/heic"}
SEED_POLICIES = Path(__file__).parent / "seed" / "policies.json"


class LabelError(ValueError):
    """The labels file does not follow the format."""


# --- labels ---


def load_labels(path: Path | str) -> dict:
    """Read and check a labels file; photo paths are resolved against the file's folder."""
    path = Path(path)
    try:
        labels = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise LabelError(f"{path}: {e}") from e
    base = path.parent
    if not isinstance(labels, dict) or not isinstance(labels.get("photos"), list):
        raise LabelError("labels need a 'photos' array")
    seen: set[str] = set()
    for photo in labels["photos"]:
        _check_photo(photo, seen)
        photo["file"] = str((base / photo["path"]).resolve())
    claim_ids: set[str] = set()
    for claim in labels.get("claims") or []:
        _check_claim(claim, seen, claim_ids)
    if not isinstance(labels.get("answers", {}), dict):
        raise LabelError("'answers' must be an object")
    return labels


def _check_photo(photo, seen: set[str]) -> None:
    if not isinstance(photo, dict) or not isinstance(photo.get("id"), str) or not photo["id"]:
        raise LabelError("every photo needs an 'id'")
    pid = photo["id"]
    if pid in seen:
        raise LabelError(f"photo {pid}: duplicate id")
    seen.add(pid)
    if not isinstance(photo.get("path"), str):
        raise LabelError(f"photo {pid}: 'path' is required")
    if photo.get("slot") not in photo_check.CHECKED_SLOTS:
        raise LabelError(f"photo {pid}: slot must be one of {', '.join(photo_check.CHECKED_SLOTS)}")
    expected = photo.get("expected", {})
    if not isinstance(expected, dict):
        raise LabelError(f"photo {pid}: 'expected' must be an object")
    for field, slot in FIELDS.items():
        if field in expected and photo["slot"] != slot:
            raise LabelError(f"photo {pid}: '{field}' is labelled only on {slot} photos")
    if "odometer" in expected and (isinstance(expected["odometer"], bool) or not isinstance(expected["odometer"], int)):
        raise LabelError(f"photo {pid}: 'odometer' is whole miles (an integer)")
    for field in ("plate", "vin"):
        if field in expected and not isinstance(expected[field], str):
            raise LabelError(f"photo {pid}: '{field}' must be a string")
    if "damages" in expected:
        if photo["slot"] not in damage.DAMAGE_SLOTS:
            raise LabelError(f"photo {pid}: 'damages' is labelled only on damage photos")
        if not isinstance(expected["damages"], list):
            raise LabelError(f"photo {pid}: 'damages' must be an array ([] for an undamaged car)")
        for d in expected["damages"]:
            if not isinstance(d, dict) or d.get("area") not in AREAS:
                raise LabelError(f"photo {pid}: damage area must be one of the fixed areas")
            if d.get("type") not in DAMAGE_TYPES:
                raise LabelError(f"photo {pid}: damage type must be one of vocab.DAMAGE_TYPES")
            if d.get("severity") not in damage.SEVERITIES:
                raise LabelError(f"photo {pid}: severity must be minor, moderate or severe")
            if d.get("age", "unsure") not in damage.AGES:
                raise LabelError(f"photo {pid}: age must be new, old or unsure")
    if "retake" in expected and not isinstance(expected["retake"], bool):
        raise LabelError(f"photo {pid}: 'retake' is true (the check should ask for another shot) or false")
    if "answers" in photo and not isinstance(photo["answers"], dict):
        raise LabelError(f"photo {pid}: 'answers' must be an object")


def _check_claim(claim, photos: set[str], seen: set[str]) -> None:
    if not isinstance(claim, dict) or not isinstance(claim.get("id"), str) or not claim["id"]:
        raise LabelError("every claim needs an 'id'")
    cid = claim["id"]
    if cid in seen:
        raise LabelError(f"claim {cid}: duplicate id")
    seen.add(cid)
    refs = claim.get("photos")
    if not isinstance(refs, list) or not refs or any(r not in photos for r in refs):
        raise LabelError(f"claim {cid}: 'photos' must list known photo ids")
    flags = claim.get("expected_flags")
    if not isinstance(flags, list) or any(not isinstance(f, str) for f in flags):
        raise LabelError(f"claim {cid}: 'expected_flags' must be an array of reason codes ([] for none)")
    if "expected_lane" in claim and claim["expected_lane"] not in proposal.LANES:
        raise LabelError(f"claim {cid}: expected_lane must be one of {', '.join(proposal.LANES)}")
    for key in ("answers",):
        if key in claim and not isinstance(claim[key], dict):
            raise LabelError(f"claim {cid}: '{key}' must be an object")
    if not isinstance(claim.get("submit_codes", []), list):
        raise LabelError(f"claim {cid}: 'submit_codes' must be an array")


def policy_of(labels: dict) -> tuple[dict | None, bool]:
    """The labels' policy: a policy number from the seed file, an inline policy object, or null."""
    ref = labels.get("policy", "DEMO-AUTO-1001")
    if ref is None:
        return None, False
    if isinstance(ref, dict):
        return ref, True
    seed = json.loads(SEED_POLICIES.read_text())
    for p in seed["policies"]:
        if p["policy_no"] == ref:
            return p, True
    raise LabelError(f"policy {ref} is not in the seed file")


# --- replay transport (CI, and re-running without the API) ---


class ReplayTransport:
    """Answers recorded per photo and per claim, in the transport shape MetaClient expects.

    replay = {"photos": {photo_id: {"reads": [answer per photo prompt], "damage": answer}},
              "claims": {claim_id: {"story": answer}}, "sam": {concept: output_text} (optional)}
    A photo is found by its bytes, a story prompt by the claim's statement inside it.
    A missing answer raises, so MetaClient logs a failed call as it would for the API."""

    def __init__(self, labels: dict, replay: dict):
        self.by_sha: dict[str, dict] = {}
        for photo in labels["photos"]:
            sha = hashlib.sha256(Path(photo["file"]).read_bytes()).hexdigest()
            self.by_sha[sha] = (replay.get("photos") or {}).get(photo["id"]) or {}
        self.stories = []
        for claim in labels.get("claims") or []:
            text = _answers(labels, claim.get("answers")).get("description")
            answer = ((replay.get("claims") or {}).get(claim["id"]) or {}).get("story")
            if isinstance(text, str) and text.strip() and answer is not None:
                self.stories.append((text.strip(), answer))
        self.prompt_read = {p: i for prompts in photo_prompts.READS.values() for i, p in enumerate(prompts)}
        self.sam = replay.get("sam")

    def __call__(self, model: str, prompt: str, image: bytes | None, mime: str | None, timeout: float) -> dict:
        if model == segment.MODEL:  # SAM's raw text by concept (replay["sam"]); unset -> found nothing
            return {"text": (self.sam or {}).get(prompt, ""), "input_tokens": 0, "output_tokens": 0}
        if image is None:
            if not prompt.startswith(consistency_prompts.STORY):
                raise RuntimeError("replay: unknown text prompt")
            answer = next((a for text, a in self.stories if text in prompt), None)
        else:
            recorded = self.by_sha.get(hashlib.sha256(image).hexdigest(), {})
            if prompt == damage_prompts.DAMAGE:
                answer = recorded.get("damage")
            elif prompt in self.prompt_read:
                reads = recorded.get("reads") or []
                n = self.prompt_read[prompt]
                answer = reads[n] if n < len(reads) else None
            else:
                answer = None
        if answer is None:
            raise RuntimeError("replay: no recorded answer")
        return {"text": answer if isinstance(answer, str) else json.dumps(answer),
                "input_tokens": 1000, "output_tokens": 200}


# --- running ---


def run(labels: dict, meta: MetaClient, *, run_id: str, now: datetime, today: date | None = None,
        workers: int = 4, sam: bool = False, lane_rule: str = "cost") -> dict:
    """Send every labelled photo and claim through the production code paths; -> metrics (JSON-able).
    sam=True also outlines every boxed damage with SAM 3.1 (track 6, as store._outline_damages, no cap).
    `lane_rule` drives each claim's `lane`; the other rule's lane is kept for comparison."""
    today = today or _date(labels.get("today")) or now.date()
    policy, verified = policy_of(labels)
    photos = {p["id"]: p for p in labels["photos"]}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        done = list(pool.map(lambda p: _photo(p, labels, meta, run_id, policy, today, sam), labels["photos"]))
    by_id = {d["id"]: d for d in done}
    claims = [_claim(c, labels, photos, by_id, meta, run_id, policy, verified, today, now, lane_rule)
              for c in labels.get("claims") or []]
    calls = [c for d in done for c in d.pop("_calls")] + [c for k in claims for c in k.pop("_calls")]
    return {
        "version": VERSION,
        "run_id": run_id,
        "prompts": {"photo": photo_prompts.VERSION, "damage": damage_prompts.VERSION,
                    "consistency": consistency_prompts.VERSION},
        "code": {"photo_check": photo_check.VERSION, "damage": damage.VERSION,
                 "consistency": consistency.VERSION, "proposal": proposal.VERSION},
        "model": photo_prompts.MODEL,
        "lane_rule": lane_rule,
        "counts": {"photos": len(done), "claims": len(claims)},
        "reads": _read_metrics(done),
        "retake": _retake_metrics(done),
        "damage": _damage_metrics(done),
        "photo_check_parts": _parts_metrics(done),
        "e2": _flag_metrics(claims),
        "sam": _sam_metrics(done) if sam else None,
        "cost": _cost(meta, run_id, calls),
        "photos": done,
        "claims": claims,
    }


def _answers(labels: dict, extra: dict | None) -> dict:
    return {**(labels.get("answers") or {}), **(extra or {})}


def _date(value) -> date | None:
    try:
        return date.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _call(meta: MetaClient, calls: list, **kw) -> dict:
    out = meta.read_json(**kw)
    calls.append({"purpose": kw["purpose"], "ok": out["ok"], "cached": out["cached"],
                  "call_id": out["call_id"], "error": out["error"]})
    return out


def _photo(photo: dict, labels: dict, meta: MetaClient, run_id: str, policy, today: date,
           sam: bool = False) -> dict:
    """One photo: the photo-check reads + decide (as store.run_photo_check), and the damage reading
    (as store._read_damage) on damage slots."""
    slot, calls = photo["slot"], []
    data = Path(photo["file"]).read_bytes()
    mime = MIME.get(Path(photo["file"]).suffix.lower(), "image/jpeg")
    claim_id = f"EVAL-{run_id}-{photo['id']}"
    reads = [
        _call(meta, calls, purpose=f"photo.{slot}#{n + 1}", model=photo_prompts.MODEL, prompt=prompt,
              image=data, mime=mime, claim_id=claim_id, item_id=photo["id"])
        for n, prompt in enumerate(photo_prompts.READS[slot])
    ]
    readings = [r["json"] for r in reads] if all(r["ok"] for r in reads) else None
    ctx = {"policy": policy, "answers": _answers(labels, photo.get("answers")), "today": today}
    decision = photo_check.decide(slot, readings, ctx, 1)
    out = {
        "id": photo["id"], "slot": slot, "expected": photo.get("expected", {}),
        "check": {"status": "done" if readings is not None else "failed",
                  "error": next((r["error"] for r in reads if not r["ok"]), None),
                  "readings": readings, "result": decision["result"], "reason_code": decision["reason_code"],
                  "value": decision["value"]},
        "damage": None, "_calls": calls,
    }
    if slot in damage.DAMAGE_SLOTS:
        call = _call(meta, calls, purpose=f"damage.{slot}", model=damage_prompts.MODEL, prompt=damage_prompts.DAMAGE,
                     image=data, mime=mime, claim_id=claim_id, item_id=photo["id"])
        shaped = damage.shape(call["json"]) if call["ok"] else None
        if shaped is None:
            out["damage"] = {"status": "failed", "error": call["error"] or "bad_shape", "result": None}
        else:
            out["damage"] = {"status": "done", "error": None, "result": shaped}
            if sam:
                out["outlines"] = _outlines(shaped, data, meta, claim_id, photo["id"], calls)
    return out


def _outlines(shaped: dict, data: bytes, meta: MetaClient, claim_id: str, item_id: str, calls: list) -> list[dict]:
    """Each boxed damage of one photo: crop -> SAM 3.1 -> did SAM find a region overlapping Muse's box?"""
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        image = image.convert("RGB")
    except Exception:
        return [{"number": n, "sam_agrees": "not_run", "error": "image_unreadable"}
                for n, d in enumerate(shaped["damages"], 1) if d.get("box")]
    out = []
    for number, d in enumerate(shaped["damages"], 1):
        if not d.get("box"):
            continue
        crop = outline.crop_box(d["box"], *image.size)
        answer = outline.ask(meta, image.crop(crop), outline.concept(d), purpose="outline.damage",
                             claim_id=claim_id, item_id=item_id)
        calls.append({"purpose": "outline.damage", "ok": answer["ok"], "cached": answer["cached"],
                      "call_id": answer["call_id"], "error": answer["error"]})
        if not answer["ok"]:
            out.append({"number": number, "sam_agrees": "not_run", "error": answer["error"]})
            continue
        derived = outline.derive(d["box"], outline.regions(segment.parse(answer["text"]), crop), None, *image.size)
        out.append({"number": number, "sam_agrees": derived["sam_agrees"], "error": None,
                    "area": derived["area"]})
    return out


def _claim(claim: dict, labels: dict, photos: dict, done: dict, meta: MetaClient, run_id: str,
           policy, verified: bool, today: date, now: datetime, lane_rule: str = "cost") -> dict:
    """One claim after submit, as store.submit + run_damage + run_consistency + _proposal_view."""
    calls: list = []
    answers = _answers(labels, claim.get("answers"))
    started = now.isoformat()
    refs = claim["photos"]
    check_rows = [
        {"item_id": pid, "slot_id": done[pid]["slot"], "received_at": f"{n:04d}", "superseded": False,
         "status": done[pid]["check"]["status"], "readings": done[pid]["check"]["readings"],
         "started_at": started}
        for n, pid in enumerate(refs)
    ]
    entries = photo_check.view(check_rows, {"policy": policy, "answers": answers, "today": today}, now)
    damage_rows = [
        {"item_id": pid, "slot_id": done[pid]["slot"], "status": done[pid]["damage"]["status"],
         "result": done[pid]["damage"]["result"], "error": done[pid]["damage"]["error"], "started_at": started}
        for pid in refs if done[pid]["damage"] is not None
    ]
    reading = damage.view(damage_rows, now)
    story, story_error = None, None
    if consistency.usable(reading):
        call = _call(meta, calls, purpose="consistency.story", model=consistency_prompts.MODEL,
                     prompt=consistency_prompts.story_prompt(consistency.story_block(answers, reading)),
                     claim_id=f"EVAL-{run_id}-{claim['id']}")
        if call["ok"]:
            story = consistency.shape_story(call["json"], len(reading["damages"]))
            story_error = None if story is not None else "bad_shape"
        else:
            story_error = call["error"]
    covered = coverage.check(policy, verified, answers)
    checks = covered + photo_check.rows(entries) + consistency.check(answers, reading, story)
    deductible = next((r["value"] for r in covered
                       if r["check_id"] == "coverage.deductible" and r["result"] == "value"), None)
    built = proposal.build(checks, list(claim.get("submit_codes") or []), reading,
                           estimate.rough_range(reading), deductible if type(deductible) is int else None, lane_rule)
    predicted = sorted({f["reason_code"] for f in built["flags"]})
    expected = sorted(set(claim["expected_flags"]))
    return {
        "id": claim["id"], "note": claim.get("note"),
        "expected_flags": expected, "predicted_flags": predicted,
        "missed": sorted(set(expected) - set(predicted)), "extra": sorted(set(predicted) - set(expected)),
        "expected_lane": claim.get("expected_lane"), "lane": built["lane"],
        "lanes": {"cost": built["lane_cost"]["lane"], "count": built["lane_v1"]["lane"]},
        "claim_value": built["lane_cost"]["cost"]["claim_value"],
        "break_even_value": built["lane_cost"]["cost"]["break_even_value"],
        "evidence": {k: built["evidence"][k] for k in ("score_log10", "weight_log10", "band")}
        | {"causes": [c["cause"] for c in built["evidence"]["contributions"]],
           "missing": [m["cause"] for m in built["evidence"]["missing"]]},
        "story": story, "story_error": story_error, "damage_status": reading["status"],
        "_calls": calls,
    }


# --- metrics ---


def _rate(num: int, den: int) -> float | None:
    return round(num / den, 4) if den else None


Z95 = 1.959964  # two-sided 95% quantile of the normal distribution
SMALL_N = 30


def wilson(hits: int, n: int, z: float = Z95) -> list[float] | None:
    """95% Wilson score interval for hits / n -> [low, high] rounded to 4 places; None when n is 0.

    Unlike p +/- z * se it stays inside [0, 1] and is not zero-width at 0/n or n/n, which is
    where small evaluation sets live (Wilson 1927; Brown, Cai and DasGupta 2001)."""
    if not n:
        return None
    p = hits / n
    z2 = z * z
    centre = (p + z2 / (2 * n)) / (1 + z2 / n)
    half = z / (1 + z2 / n) * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def _share(hits: int, of: int) -> dict:
    return {"hits": hits, "of": of, "rate": _rate(hits, of), "ci": wilson(hits, of)}


def mcnemar_exact(only_a: int, only_b: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pairs (right with A only, right with B only).

    Under "A and B are equally good" each discordant pair is a fair coin, so the p-value is
    the binomial tail of the smaller count, doubled and capped at 1."""
    n = only_a + only_b
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(min(only_a, only_b) + 1)) / 2 ** n
    return round(min(1.0, 2 * tail), 6)


def paired(a: dict, b: dict) -> dict:
    """Two versions scored on the same items: {item: right?} each -> the 2x2 counts and McNemar's p.

    Only items present in both are paired; "prompt B is better" needs only_b well above only_a
    and a small p, not a higher rate on a different set of photos."""
    keys = sorted(set(a) & set(b))
    both = sum(1 for k in keys if a[k] and b[k])
    only_a = sum(1 for k in keys if a[k] and not b[k])
    only_b = sum(1 for k in keys if b[k] and not a[k])
    return {"pairs": len(keys), "both_right": both, "only_a": only_a, "only_b": only_b,
            "both_wrong": len(keys) - both - only_a - only_b, "p_value": mcnemar_exact(only_a, only_b),
            "a": _share(both + only_a, len(keys)), "b": _share(both + only_b, len(keys))}


def compare_reads(a: dict, b: dict) -> dict:
    """Two eval results (result.json) on the same labelled photos -> per field, paired exact-match."""
    out = {}
    for field in FIELDS:
        right = []
        for result in (a, b):
            misses = {m["id"] for m in result["reads"][field]["misses"]}
            right.append({p["id"]: p["id"] not in misses for p in result["photos"]
                          if p["slot"] == FIELDS[field] and field in p["expected"]})
        out[field] = paired(*right)
    return out


def _norm(field: str, value):
    if value is None:
        return None
    if field == "plate":
        return "".join(c for c in str(value).upper() if c.isascii() and c.isalnum())
    if field == "vin":
        return str(value).upper().replace(" ", "").replace("-", "")
    return int(value)


def _read_metrics(done: list[dict]) -> dict:
    out = {}
    for field, slot in FIELDS.items():
        items = [d for d in done if d["slot"] == slot and field in d["expected"]]
        results: dict[str, int] = {}
        read = exact = 0
        misses = []
        for d in items:
            want, got = _norm(field, d["expected"][field]), _norm(field, d["check"]["value"])
            results[d["check"]["result"]] = results.get(d["check"]["result"], 0) + 1
            read += got is not None
            if got == want:
                exact += 1
            else:
                misses.append({"id": d["id"], "expected": want, "got": got, "result": d["check"]["result"],
                               "reason_code": d["check"]["reason_code"], "error": d["check"]["error"]})
        out[field] = {"labelled": len(items), "read": read, "exact": exact,
                      "exact_rate": _rate(exact, len(items)), "exact_ci": wilson(exact, len(items)),
                      "accuracy_when_read": _rate(exact, read), "accuracy_when_read_ci": wilson(exact, read),
                      "results": dict(sorted(results.items())), "misses": misses}
    return out


def _retake_metrics(done: list[dict]) -> dict:
    """Photos labelled `retake` (T57): did the check before signing ask for another shot exactly when it
    should (blurred, dark, wrong subject, unreadable) and not on a good photo?"""
    items = [d for d in done if "retake" in d["expected"]]
    want = [d for d in items if d["expected"]["retake"]]
    good = [d for d in items if not d["expected"]["retake"]]
    caught = sum(d["check"]["result"] == "retake" for d in want)
    false = sum(d["check"]["result"] == "retake" for d in good)
    misses = [{"id": d["id"], "slot": d["slot"], "expected_retake": d["expected"]["retake"],
               "result": d["check"]["result"], "reason_code": d["check"]["reason_code"]}
              for d in items if (d["check"]["result"] == "retake") != d["expected"]["retake"]]
    return {"should_retake": len(want), "caught": caught, "caught_rate": _rate(caught, len(want)),
            "caught_ci": wilson(caught, len(want)), "fine": len(good) - false, "false_retakes": false,
            "false_retake_rate": _rate(false, len(good)), "false_retake_ci": wilson(false, len(good)),
            "misses": misses}


def _retake_lines(m: dict | None) -> list[str]:
    if not m or not (m["should_retake"] or m["fine"] + m["false_retakes"]):
        return []
    lines = ["", "## E1: asks for a retake when it should (photo check before signing)", "",
             "| | Photos | Asks for a retake |", "|---|---|---|",
             f"| Should ask (blurred, dark, wrong subject, unreadable) | {m['should_retake']} | "
             f"{_ratio(m['caught'], m['should_retake'])} |",
             f"| Good photos (false retakes) | {m['fine'] + m['false_retakes']} | "
             f"{_ratio(m['false_retakes'], m['fine'] + m['false_retakes'])} |"]
    lines += _small(m["should_retake"], m["fine"] + m["false_retakes"])
    if m["misses"]:
        lines += ["", "Wrong calls: " + "; ".join(
            f"{x['id']} ({x['slot']}: expected {'retake' if x['expected_retake'] else 'no retake'}, got "
            f"{x['result']}{', ' + x['reason_code'] if x['reason_code'] else ''})" for x in m["misses"])]
    return lines


def _pr(tp: int, fp: int, fn: int) -> dict:
    return {"tp": tp, "fp": fp, "fn": fn, "precision": _rate(tp, tp + fp), "precision_ci": wilson(tp, tp + fp),
            "recall": _rate(tp, tp + fn), "recall_ci": wilson(tp, tp + fn)}


def _area_counts(pairs: list[tuple[set, set]], lenient: bool = False) -> tuple[dict, dict]:
    """pairs = (expected areas, predicted areas) per photo -> (per-area counts, overall counts)."""
    per = {a: [0, 0, 0] for a in AREAS}
    for want, got in pairs:
        for a in got:
            hit = a in want or (lenient and bool(ADJACENT.get(a, set()) & want))
            per[a][0 if hit else 1] += 1
        for a in want:
            covered = a in got or (lenient and bool(ADJACENT.get(a, set()) & got))
            if not covered:
                per[a][2] += 1
    totals = [sum(v[i] for v in per.values()) for i in range(3)]
    return ({a: _pr(*v) for a, v in per.items() if any(v)}, _pr(*totals))


def _damage_metrics(done: list[dict]) -> dict:
    labelled = [d for d in done if "damages" in d["expected"] and d["damage"] is not None]
    read = [d for d in labelled if d["damage"]["status"] == "done"]
    pairs = []
    type_hits = type_total = sev_hits = sev_total = age_hits = age_total = 0
    for d in read:
        want = d["expected"]["damages"]
        got = d["damage"]["result"]["damages"]
        pairs.append(({w["area"] for w in want}, {g["area"] for g in got}))
        for w in want:
            same_area = [g for g in got if g["area"] == w["area"]]
            if not same_area:
                continue
            type_total += 1
            same_type = [g for g in same_area if g["damage_type"] == w["type"]]
            if not same_type:
                continue
            type_hits += 1
            sev_total += 1
            sev_hits += any(g["severity"] == w["severity"] for g in same_type)
            if w.get("age", "unsure") != "unsure":
                age_total += 1
                age_hits += any(g["age"] == w["age"] for g in same_type)
    per_area, strict = _area_counts(pairs)
    _, lenient = _area_counts(pairs, lenient=True)
    undamaged = [d for d in read if not d["expected"]["damages"]]
    false_damage = [d["id"] for d in undamaged if d["damage"]["result"]["damages"]]
    return {
        "labelled": len(labelled), "read": len(read),
        "not_read": [{"id": d["id"], "error": d["damage"]["error"]} for d in labelled if d["damage"]["status"] != "done"],
        "per_area": per_area, "overall_strict": strict, "overall_adjacent": lenient,
        "type_agreement": _share(type_hits, type_total),
        "severity_agreement": _share(sev_hits, sev_total),
        "age_agreement": _share(age_hits, age_total),
        "undamaged": {"photos": len(undamaged), "with_false_damage": len(false_damage),
                      "rate": _rate(len(false_damage), len(undamaged)),
                      "ci": wilson(len(false_damage), len(undamaged)), "ids": false_damage},
    }


def _parts_metrics(done: list[dict]) -> dict:
    """The quick photo check before signing (damage_closeup / damage_wide "parts")."""
    pairs = []
    for d in done:
        if "damages" not in d["expected"] or d["check"]["readings"] is None:
            continue
        parts = d["check"]["readings"][0].get("parts") or []
        pairs.append(({w["area"] for w in d["expected"]["damages"]}, {p for p in parts if p in AREAS}))
    _, strict = _area_counts(pairs)
    _, lenient = _area_counts(pairs, lenient=True)
    return {"photos": len(pairs), "overall_strict": strict, "overall_adjacent": lenient}


def _flag_metrics(claims: list[dict]) -> dict:
    codes = sorted({c for k in claims for c in k["expected_flags"] + k["predicted_flags"]})
    per = {}
    for code in codes:
        tp = sum(1 for k in claims if code in k["expected_flags"] and code in k["predicted_flags"])
        fp = sum(1 for k in claims if code not in k["expected_flags"] and code in k["predicted_flags"])
        fn = sum(1 for k in claims if code in k["expected_flags"] and code not in k["predicted_flags"])
        per[code] = _pr(tp, fp, fn)
    totals = [sum(v[key] for v in per.values()) for key in ("tp", "fp", "fn")]
    with_lane = [k for k in claims if k["expected_lane"]]
    clean = [k for k in claims if not k["expected_flags"]]
    planted = [k for k in claims if k["expected_flags"]]
    return {
        "claims": len(claims), "per_code": per, "overall": _pr(*totals),
        "exact_set": _share(sum(1 for k in claims if not k["missed"] and not k["extra"]), len(claims)),
        "lane": _share(sum(1 for k in with_lane if k["lane"] == k["expected_lane"]), len(with_lane)),
        "lane_by_rule": {rule: _share(sum(1 for k in with_lane if k["lanes"][rule] == k["expected_lane"]), len(with_lane))
                         for rule in proposal.LANE_RULES},
        "lane_changes": [{"id": k["id"], "cost": k["lanes"]["cost"], "count": k["lanes"]["count"],
                          "claim_value": k["claim_value"], "break_even_value": k["break_even_value"],
                          "band": k["evidence"]["band"]}
                         for k in claims if k["lanes"]["cost"] != k["lanes"]["count"]],
        "operating_point": {rule: {"planted_reaching_a_person": _share(sum(1 for k in planted if k["lanes"][rule] in PERSON_LANES), len(planted)),
                                   "clean_reaching_a_person": _share(sum(1 for k in clean if k["lanes"][rule] in PERSON_LANES), len(clean))}
                            for rule in proposal.LANE_RULES},
        "detection": detection_at_false_alarm([k["evidence"]["score_log10"] for k in clean],
                                              [k["evidence"]["score_log10"] for k in planted]),
    }


def _sam_metrics(done: list[dict]) -> dict:
    """Agreement between Muse Spark's boxes and SAM 3.1's regions; a damage SAM did not answer for is
    left out of the rate (missing, not a disagreement)."""
    rows = [(d["id"], o) for d in done for o in d.get("outlines") or []]
    yes = sum(1 for _, o in rows if o["sam_agrees"] == "yes")
    no = [f"{pid}#{o['number']}" for pid, o in rows if o["sam_agrees"] == "no"]
    return {"boxed": len(rows), "agrees": yes, "disagrees": len(no),
            "not_run": sum(1 for _, o in rows if o["sam_agrees"] == "not_run"),
            "agreement_rate": _rate(yes, yes + len(no)), "disagreements": no}
PERSON_LANES = ("siu_referral", "adjuster_review")
FALSE_ALARM_RATES = (0.01, 0.05, 0.10)


def detection_at_false_alarm(clean: list[float], planted: list[float], rates=FALSE_ALARM_RATES) -> dict:
    """Evidence scores of clean and planted claims -> the curve and, per target false-alarm rate,
    the lowest threshold (flag when score >= threshold; None flags nothing) whose false-alarm rate on clean claims
    is at most the target, with the detection rate it gives on planted claims.

    With n clean claims the false-alarm rate moves in steps of 1/n: below 1/n the only
    threshold that fits is one above every clean score."""
    def above(scores: list[float], t: float | None) -> int:
        return 0 if t is None else sum(1 for s in scores if s >= t)

    thresholds = sorted(set(clean) | set(planted)) + [None]  # None: above every score, nothing flagged
    curve = [{"threshold": t, "false_alarm": _share(above(clean, t), len(clean)),
              "detection": _share(above(planted, t), len(planted))} for t in thresholds]
    at = {}
    for rate in rates:
        fits = [c for c in curve if clean and c["false_alarm"]["hits"] <= rate * len(clean)]
        best = fits[0] if fits else None  # lowest threshold that fits: most detection
        at[f"{rate:.2f}"] = None if best is None else {
            "threshold": best["threshold"], "false_alarm": best["false_alarm"], "detection": best["detection"]}
    return {"clean": len(clean), "planted": len(planted), "at_false_alarm": at, "curve": curve}


def _pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, math.ceil(q * len(ordered)) - 1))]


def _cost(meta: MetaClient, run_id: str, calls: list[dict]) -> dict:
    """Calls sent in this run (rows with this run's claim ids) plus the original rows of cache hits."""
    with meta.db.tx() as conn:
        sent = conn.execute("SELECT * FROM model_calls WHERE claim_id LIKE %s", (f"EVAL-{run_id}-%",)).fetchall()
        cached_ids = [c["call_id"] for c in calls if c["cached"] and c["call_id"]]
        cached = (conn.execute("SELECT * FROM model_calls WHERE id = ANY(%s)", (cached_ids,)).fetchall()
                  if cached_ids else [])
    by_id = {r["id"]: r for r in cached}
    rows = [dict(r, cached=False) for r in sent if r["status"] != "budget"]
    rows += [dict(by_id[i], cached=True) for i in cached_ids if i in by_id]
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["purpose"].split("#")[0], []).append(r)

    def summary(rs: list[dict]) -> dict:
        ok_seconds = [r["latency_ms"] / 1000 for r in rs if r["status"] == "ok"]
        return {
            "calls": len(rs), "sent": sum(1 for r in rs if not r["cached"]), "cached": sum(1 for r in rs if r["cached"]),
            "failed": sum(1 for r in rs if r["status"] != "ok"),
            "cost_usd_sent": round(sum(r["cost_usd"] or 0 for r in rs if not r["cached"]), 4),
            "cost_usd_uncached": round(sum(r["cost_usd"] or 0 for r in rs), 4),
            "cost_unknown": sum(1 for r in rs if r["cost_usd"] is None and r["status"] == "ok"),
            "p50_s": _pct(ok_seconds, 0.5), "p95_s": _pct(ok_seconds, 0.95),
            "max_s": max(ok_seconds) if ok_seconds else None,
        }

    errors: dict[str, int] = {}
    for c in calls:
        if not c["ok"]:
            errors[c["error"] or "unknown"] = errors.get(c["error"] or "unknown", 0) + 1
    return {"total": summary(rows), "by_purpose": {k: summary(v) for k, v in sorted(groups.items())},
            "budget_blocked": sum(1 for r in sent if r["status"] == "budget"), "errors": errors}


# --- markdown ---


def _ratio(hits: int, of: int) -> str:
    """ "36/40 = 0.90 [0.77, 0.96]": the count, the rate and its 95% Wilson interval."""
    if not of:
        return f"n/a ({hits}/{of})"
    lo, hi = wilson(hits, of)
    return f"{hits}/{of} = {hits / of:.2f} [{lo:.2f}, {hi:.2f}]"


def _small(*ns: int) -> list[str]:
    """A line under a table when any of its sample sizes is below SMALL_N."""
    if any(0 < n < SMALL_N for n in ns) or not any(ns):
        return ["", f"Small sample (n < {SMALL_N} in at least one row): the interval is wide."]
    return []


def _money(v) -> str:
    return "n/a" if v is None else f"${v:.4f}"


def _secs(v) -> str:
    return "n/a" if v is None else f"{v:.1f}"


def markdown(result: dict) -> str:
    """The run as a markdown report: one table per metric family, every rate with its 95% interval."""
    r = result
    lines = [
        f"# Evaluation run {r['run_id']}",
        "",
        f"Model `{r['model']}`; prompts {r['prompts']['photo']}, {r['prompts']['damage']}, "
        f"{r['prompts']['consistency']}; {r['counts']['photos']} photos, {r['counts']['claims']} claims; "
        f"lane rule `{r.get('lane_rule', 'cost')}`.",
        "",
        "Rates are `hits/n = rate [95% Wilson interval]`.",
        "",
        "## E1: reads (photo check before signing)",
        "",
        "| Field | Labelled | Read | Exact | Exact-match rate | Accuracy when read |",
        "|---|---|---|---|---|---|",
    ]
    for field, m in r["reads"].items():
        lines.append(f"| {field} | {m['labelled']} | {m['read']} | {m['exact']} | "
                     f"{_ratio(m['exact'], m['labelled'])} | {_ratio(m['exact'], m['read'])} |")
    lines += _small(*(m["labelled"] for m in r["reads"].values()))
    misses = [(f, x) for f, m in r["reads"].items() for x in m["misses"]]
    if misses:
        lines += ["", "Misses: " + "; ".join(
            f"{x['id']} {f} expected {x['expected']} got {x['got']} ({x['result']}"
            + (f", {x['reason_code']}" if x["reason_code"] else "") + ")" for f, x in misses)]
    lines += _retake_lines(r.get("retake"))
    d = r["damage"]
    lines += [
        "",
        "## E1: damage reading (after submit)",
        "",
        f"{d['read']} of {d['labelled']} labelled damage photos read"
        + (f"; not read (left out, not counted as 0): {', '.join(x['id'] + ' ' + str(x['error']) for x in d['not_read'])}"
           if d["not_read"] else "") + ".",
        "",
        "| Area | TP | FP | FN | Precision | Recall |",
        "|---|---|---|---|---|---|",
    ]
    rows = list(d["per_area"].items()) + [("**All areas (exact area)**", d["overall_strict"]),
                                          ("**All areas (adjacent counts)**", d["overall_adjacent"])]
    for area, m in rows:
        lines.append(f"| {area} | {m['tp']} | {m['fp']} | {m['fn']} | {_ratio(m['tp'], m['tp'] + m['fp'])} | "
                     f"{_ratio(m['tp'], m['tp'] + m['fn'])} |")
    lines += _small(d["overall_strict"]["tp"] + d["overall_strict"]["fp"], d["overall_strict"]["tp"] + d["overall_strict"]["fn"])
    u = d["undamaged"]
    lines += [
        "",
        "| Measure | Value |",
        "|---|---|",
        f"| Type right, when the area is found | {_ratio(d['type_agreement']['hits'], d['type_agreement']['of'])} |",
        f"| Severity right, when area and type are found | {_ratio(d['severity_agreement']['hits'], d['severity_agreement']['of'])} |",
        f"| Age (new / old) right, when area and type are found | {_ratio(d['age_agreement']['hits'], d['age_agreement']['of'])} |",
        f"| False damage on undamaged cars | {_ratio(u['with_false_damage'], u['photos'])}"
        + (f" ({', '.join(u['ids'])})" if u["ids"] else "") + " |",
    ]
    lines += _small(d["type_agreement"]["of"], d["severity_agreement"]["of"], d["age_agreement"]["of"], u["photos"])
    p = r["photo_check_parts"]
    ps, pa = p["overall_strict"], p["overall_adjacent"]
    lines += [
        "",
        f"Photo check before signing, damaged parts ({p['photos']} photos): exact area precision "
        f"{_ratio(ps['tp'], ps['tp'] + ps['fp'])}, recall {_ratio(ps['tp'], ps['tp'] + ps['fn'])}; "
        f"adjacent counts precision {_ratio(pa['tp'], pa['tp'] + pa['fp'])}, recall {_ratio(pa['tp'], pa['tp'] + pa['fn'])}.",
    ]
    e = r["e2"]
    lines += [
        "",
        "## E2: flags on claims with planted mismatches",
        "",
        "| Flag | TP | FP | FN | Precision | Recall |",
        "|---|---|---|---|---|---|",
    ]
    for code, m in list(e["per_code"].items()) + [("**All flags**", e["overall"])]:
        lines.append(f"| {code} | {m['tp']} | {m['fp']} | {m['fn']} | {_ratio(m['tp'], m['tp'] + m['fp'])} | "
                     f"{_ratio(m['tp'], m['tp'] + m['fn'])} |")
    lines += _small(e["claims"])
    lines += [
        "",
        f"Claims with exactly the expected flags: {_ratio(e['exact_set']['hits'], e['exact_set']['of'])}. "
        f"Lane as expected: {_ratio(e['lane']['hits'], e['lane']['of'])} "
        f"(cost rule {_ratio(e['lane_by_rule']['cost']['hits'], e['lane_by_rule']['cost']['of'])}; "
        f"count rule {_ratio(e['lane_by_rule']['count']['hits'], e['lane_by_rule']['count']['of'])}).",
        "",
        "| Claim | Expected | Predicted | Missed | Extra | Lane | Evidence |",
        "|---|---|---|---|---|---|---|",
    ]
    for k in r["claims"]:
        lane = k["lane"] + (f" (expected {k['expected_lane']})" if k["expected_lane"] and k["expected_lane"] != k["lane"] else "")
        ev = k["evidence"]
        lines.append(f"| {k['id']} | {', '.join(k['expected_flags']) or '-'} | {', '.join(k['predicted_flags']) or '-'} | "
                     f"{', '.join(k['missed']) or '-'} | {', '.join(k['extra']) or '-'} | {lane} | "
                     f"{ev['band']} ({ev['weight_log10']:+.2f}) |")
    lines += ["", "### Lane rules: cost vs count", ""]
    if e["lane_changes"]:
        lines += ["| Claim | Cost rule | Count rule | Claim value | Break-even value | Evidence |", "|---|---|---|---|---|---|"]
        for c in e["lane_changes"]:
            lines.append(f"| {c['id']} | {c['cost']} | {c['count']} | {_dollars(c['claim_value'])} | "
                         f"{_dollars(c['break_even_value'])} | {c['band']} |")
    else:
        lines.append("Both rules give every claim the same lane.")
    lines += ["", "| Rule | Planted claims reaching a person (SIU or review) | Clean claims reaching a person |", "|---|---|---|"]
    for rule, m in e["operating_point"].items():
        lines.append(f"| {rule} | {_ratio(m['planted_reaching_a_person']['hits'], m['planted_reaching_a_person']['of'])} | "
                     f"{_ratio(m['clean_reaching_a_person']['hits'], m['clean_reaching_a_person']['of'])} |")
    det = e["detection"]
    lines += [
        "",
        "### Detection at a fixed false-alarm rate (evidence score)",
        "",
        f"{det['planted']} planted and {det['clean']} clean claims. A claim is flagged when its evidence score "
        "(log10 odds) is at or above the threshold. The weights are assumed, not fitted, so this is not calibrated: "
        "fitting them needs labelled outcomes of SIU referrals.",
        "",
        "| Target false-alarm rate | Threshold | False alarms on clean claims | Detection on planted claims |",
        "|---|---|---|---|",
    ]
    for rate, m in det["at_false_alarm"].items():
        if m is None:
            lines.append(f"| {rate} | n/a (no clean claims) | - | - |")
            continue
        lines.append(f"| {rate} | {_threshold(m['threshold'])} | {_ratio(m['false_alarm']['hits'], m['false_alarm']['of'])} | "
                     f"{_ratio(m['detection']['hits'], m['detection']['of'])} |")
    if det["clean"] and det["clean"] < 100:
        lines += ["", f"With {det['clean']} clean claim{'s' if det['clean'] != 1 else ''} the false-alarm rate moves in steps "
                  f"of {1 / det['clean']:.2f}: a lower target is met only by a threshold above every clean score."]
    if r.get("transport") == "replay":
        lines += ["", "Recorded answers (replay): these numbers measure the harness, not the product."]
    lines += ["", "| Threshold | False alarms | Detection |", "|---|---|---|"]
    for c in det["curve"]:
        lines.append(f"| {_threshold(c['threshold'])} | {_ratio(c['false_alarm']['hits'], c['false_alarm']['of'])} | "
                     f"{_ratio(c['detection']['hits'], c['detection']['of'])} |")
    lines += _small(det["clean"], det["planted"])
    sam = r.get("sam")
    if sam:
        latency = r["cost"]["by_purpose"].get("outline.damage") or {}
        lines += [
            "",
            "## E1: SAM 3.1 outlines (Muse Spark box -> crop -> SAM)",
            "",
            "| Measure | Value |",
            "|---|---|",
            f"| Boxed damages | {sam['boxed']} |",
            f"| SAM region overlaps Muse's box | {_ratio(sam['agrees'], sam['agrees'] + sam['disagrees'])} |",
            f"| SAM not run or failed (left out) | {sam['not_run']} |",
            f"| SAM latency p50 / p95 s | {_secs(latency.get('p50_s'))} / {_secs(latency.get('p95_s'))} |",
        ]
        if sam["disagreements"]:
            lines += ["", "SAM found nothing in Muse's box: " + ", ".join(sam["disagreements"])]
    c = r["cost"]
    lines += [
        "",
        "## Cost and latency (from model_calls)",
        "",
        "| Purpose | Calls | Sent | Cached | Failed | Cost sent | Cost uncached | p50 s | p95 s | max s |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name, m in list(c["by_purpose"].items()) + [("**All**", c["total"])]:
        lines.append(f"| {name} | {m['calls']} | {m['sent']} | {m['cached']} | {m['failed']} | {_money(m['cost_usd_sent'])} | "
                     f"{_money(m['cost_usd_uncached'])} | {_secs(m['p50_s'])} | {_secs(m['p95_s'])} | {_secs(m['max_s'])} |")
    if c["errors"] or c["budget_blocked"]:
        lines += ["", "Failed calls: " + ", ".join(f"{k} x{v}" for k, v in sorted(c["errors"].items()))
                  + (f"; refused by the budget cap: {c['budget_blocked']}" if c["budget_blocked"] else "")]
    lines += ["", "Latency is model time per call (cache hits report the original call). "
              "\"Cost uncached\" is what the run would cost with an empty cache."]
    return "\n".join(lines) + "\n"


def _dollars(v) -> str:
    return "not priced" if v is None else f"${v:,}"


def _threshold(t) -> str:
    return "above every score" if t is None else f"{t:+.3f}"
