"""Store domain mixin: reports (split from store.py, behaviour unchanged)."""

import hashlib
import io
import json
import logging
import re
import secrets
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg
import psycopg.errors
from PIL import Image

from . import ai_summary, capture, claim_type, consistency, consistency_prompts, coverage, damage, damage_prompts, decision, desk, diary, estimate, evaluation, integration, lifecycle, loss_classify, loss_notice, meta_client, outline, photo_check, photo_prompts, policies, proposal, report, report_pdf, review, review_steps, seal, segment, signals, users, vocab
from .blobstore import BlobNotFound, BlobStore
from .db import Database
from .errors import CaptureError

from .store_common import *
from .store_common import _liveness_assessment, _mean_abs_diff, _new_claim_id, _open_photo, _report_dir, _sniff_mime, _validate_upload


class ReportsMixin:
    def run_photo_check(self, item_id: str) -> None:
        """Check one uploaded photo against the claim in the background; never raises."""
        try:
            with self.db.tx() as conn:
                check = conn.execute("SELECT * FROM photo_checks WHERE item_id=%s", (item_id,)).fetchone()
                if not check or check["status"] != "pending":
                    return
                claim_id, slot = check["claim_id"], check["slot_id"]
                item = conn.execute("SELECT path, mime FROM items WHERE id=%s", (item_id,)).fetchone()
            if item is None:
                raise CaptureError("Item not found", 404)
            if slot == "driver_license" and not self.check_license:
                self._finish_check(item_id, claim_id, slot, "failed", None, "license_check_off")
                return
            data = self.blobs.get(item["path"])
            prompts = photo_prompts.READS[slot]
            if len(prompts) == 1:
                calls = [self._read_one(slot, 1, prompts[0], data, item["mime"], claim_id, item_id)]
            else:  # the two VIN reads run in parallel
                with ThreadPoolExecutor(max_workers=len(prompts)) as pool:
                    futures = [
                        pool.submit(self._read_one, slot, n + 1, prompt, data, item["mime"], claim_id, item_id)
                        for n, prompt in enumerate(prompts)
                    ]
                    calls = [f.result() for f in futures]
            bad = next((c for c in calls if not c["ok"]), None)
            if bad is not None:
                self._finish_check(item_id, claim_id, slot, "failed", None, bad["error"])
                return
            readings = [c["json"] for c in calls]
            if slot in photo_prompts.RECHECKS:
                readings = self._maybe_recheck(slot, item_id, claim_id, data, item["mime"], readings)
            self._finish_check(item_id, claim_id, slot, "done", readings, None)
        except Exception as e:
            try:
                with self.db.tx() as conn:
                    check = conn.execute(
                        "SELECT claim_id, slot_id FROM photo_checks WHERE item_id=%s", (item_id,)).fetchone()
                if check is not None:
                    logger.exception("photo check failed for claim %s", check["claim_id"])
                    self._finish_check(item_id, check["claim_id"], check["slot_id"], "failed", None, type(e).__name__)
                else:
                    logger.exception("photo check failed for item %s", item_id)
            except Exception:
                logger.exception("photo check failed for item %s", item_id)

    def _read_one(self, slot: str, n: int, prompt: str, data: bytes, mime: str, claim_id: str, item_id: str) -> dict:
        return self.meta.read_json(
            purpose=f"photo.{slot}#{n}", model=photo_prompts.MODEL, prompt=prompt,
            image=data, mime=mime, claim_id=claim_id, item_id=item_id,
        )

    def _maybe_recheck(self, slot: str, item_id: str, claim_id: str, data: bytes, mime: str,
                       readings: list) -> list:
        """A bounded second read (T49): after the first damage read succeeds, decide
        with the same context _photo_rows builds; only when it is retake +
        DAMAGE_AREA_MISMATCH and the claim has made fewer than 2 calls whose
        purpose ends in #recheck, read once more and store [first, second].
        Past the cap, or when the second call fails, the first decides alone."""
        try:
            with self.db.tx() as conn:
                claim = conn.execute("SELECT * FROM claims WHERE id=%s", (claim_id,)).fetchone()
                if claim is None:
                    return readings
                snapshot = conn.execute(
                    "SELECT * FROM policy_snapshots WHERE id=%s", (claim["policy_snapshot_id"],)).fetchone()
                view_rows, ctx = self._photo_rows(conn, claim, snapshot)
                entry = next((r for r in view_rows if r["item_id"] == item_id), None)
                attempt = photo_check._attempt(entry, view_rows, ctx) if entry is not None else 1
                rechecks = conn.execute(
                    "SELECT COUNT(*) AS n FROM model_calls WHERE claim_id=%s AND purpose LIKE %s",
                    (claim_id, "%#recheck")).fetchone()["n"]
            decided = photo_check.decide(slot, [readings[0]], ctx, attempt)
            if decided["result"] != "retake" or decided["reason_code"] != "DAMAGE_AREA_MISMATCH":
                return readings
            if (rechecks or 0) >= 2:
                return readings
            second = self.meta.read_json(
                purpose=f"photo.{slot}#recheck", model=photo_prompts.MODEL, prompt=photo_prompts.RECHECKS[slot],
                image=data, mime=mime, claim_id=claim_id, item_id=item_id,
            )
            if not second["ok"]:
                return readings
            return [readings[0], second["json"]]
        except Exception:
            logger.exception("second photo read failed for claim %s", claim_id)
            return readings

    def _finish_check(
        self, item_id: str, claim_id: str, slot: str, status: str, readings: list | None, error: str | None
    ) -> None:
        """Mark the check row (only while still pending), log the event, and refresh the
        stored rows when the claim is already submitted."""
        now = self.clock.now().isoformat()
        with self.db.claim_tx(claim_id=claim_id) as (conn, claim):
            updated = conn.execute(
                "UPDATE photo_checks SET status=%s, readings_json=%s, error=%s, finished_at=%s"
                " WHERE item_id=%s AND status='pending'",
                (status, policies.canonical(readings) if readings is not None else None, error, now, item_id),
            )
            if updated.rowcount == 0:
                return
            lifecycle.append_event(
                conn, claim_id, now, "system", "photo.checked",
                data={"item_id": item_id, "slot": slot, "status": status,
                      "version": f"{photo_check.VERSION}+{photo_prompts.VERSION}"},
            )
            if claim["status"] not in CAPTURE_STATUSES:
                snapshot = conn.execute(
                    "SELECT * FROM policy_snapshots WHERE id=%s", (claim["policy_snapshot_id"],)).fetchone()
                photo_check.save(conn, claim_id, self._photo_view(conn, claim, snapshot), now)

    def run_damage(self, claim_id: str) -> None:
        """Read each current damage photo once in the background, then report; never raises."""
        try:
            self._run_damage(claim_id)
        except Exception:
            logger.exception("damage analysis failed for claim %s", claim_id)
        try:
            self.make_report(claim_id)
        except Exception:
            logger.exception("make_report failed for claim %s", claim_id)

    def _run_damage(self, claim_id: str) -> None:
        """The damage reading itself; the wrapper above still reports after a failure."""
        try:
            with self.db.tx() as conn:
                rows = conn.execute(
                    "SELECT a.item_id, i.step_id AS slot_id, i.path, i.mime FROM analyses a"
                    " JOIN items i ON i.id = a.item_id"
                    " WHERE a.claim_id=%s AND a.kind='damage' AND a.status='pending'",
                    (claim_id,),
                ).fetchall()
            if not rows:
                return
            images: dict[str, bytes | Exception] = {}
            for row in rows:
                try:
                    images[row["item_id"]] = self.blobs.get(row["path"])
                except Exception as e:
                    images[row["item_id"]] = e
            with ThreadPoolExecutor(max_workers=4) as pool:
                calls = {
                    row["item_id"]: pool.submit(self._read_damage, claim_id, row, images[row["item_id"]])
                    for row in rows
                }
                outcomes = {item_id: future.result() for item_id, future in calls.items()}
            # Track 6: SAM outlines before the reading is stored, so the reading, the outlines and
            # the report built from them appear together. A failure here only loses the outlines.
            outlines = self._outline_damages(claim_id, rows, images, outcomes)
            now = self.clock.now().isoformat()
            done = failed = kept = 0
            with self.db.tx() as conn:
                for row in rows:
                    status, shaped, error = outcomes[row["item_id"]]
                    updated = conn.execute(
                        "UPDATE analyses SET status=%s, result_json=%s, error=%s, finished_at=%s"
                        " WHERE claim_id=%s AND kind='damage' AND item_id=%s AND status='pending'",
                        (
                            status,
                            policies.canonical(shaped) if shaped is not None else None,
                            error, now, claim_id, row["item_id"],
                        ),
                    )
                    if updated.rowcount == 0:
                        continue
                    conn.execute("DELETE FROM damage_outlines WHERE claim_id=%s AND item_id=%s",
                                 (claim_id, row["item_id"]))
                    for number, o_status, o_result, o_error in outlines.get(row["item_id"], []):
                        conn.execute(
                            "INSERT INTO damage_outlines (claim_id, item_id, number, status, result_json,"
                            " error, version, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                            (claim_id, row["item_id"], number, o_status, policies.canonical(o_result),
                             o_error, outline.VERSION, now),
                        )
                    if status == "done":
                        done += 1
                        kept += len(shaped["damages"])
                    else:
                        failed += 1
            if done + failed == 0:
                return
            with self.db.claim_tx(claim_id=claim_id) as (conn, _claim):
                lifecycle.append_event(
                    conn, claim_id, now, "system", "damage.read",
                    data={"version": f"{damage.VERSION}+{damage_prompts.VERSION}",
                          "photos": done + failed, "done": done, "failed": failed, "damages": kept},
                )
                self._save_claim_type(conn, claim_id, _claim, now)
        except Exception:
            logger.exception("damage analysis failed for claim %s", claim_id)

    @staticmethod
    def _has_damage_slots(answers: dict) -> bool:
        """Whether the claim type shows any damage photo slots at all."""
        overlay = loss_notice.template()["overlays"].get((answers or {}).get("loss_type"))
        if overlay is None:
            return True
        return any(loss_notice._section_id(s) == "damage" for s in overlay.get("sections", []))

    def _save_claim_type(self, conn: psycopg.Connection, claim_id: str, claim: dict, now: str) -> None:
        """Run the claim type fit over the damage reading and save (or clear) its row."""
        try:
            reading = self._damage_view(conn, claim_id)
            row = claim_type.fit(self._answers(claim), reading)
        except Exception:
            logger.exception("claim type fit failed for claim %s", claim_id)
            return
        if row is None:
            try:
                claim_type.clear(conn, claim_id)
            except Exception:
                logger.exception("claim type fit failed for claim %s", claim_id)
            return
        try:
            claim_type.save(conn, claim_id, row, now)
        except Exception:
            logger.exception("claim type fit failed for claim %s", claim_id)

    def _outline_damages(self, claim_id: str, rows: list[dict], images: dict, outcomes: dict) -> dict:
        """SAM 3.1 outlines for the damages Muse Spark just read (track 6, outline.py).
        -> {item_id: [(number, status, result, error), ...]}; never raises (a failure -> {}).

        Each boxed damage is cropped with a margin and segmented with a concept code picks; then,
        with the calls left under the per-claim cap, the panels of damages SAM agreed on are
        segmented on the whole photo for the share-of-panel figure. Capped, failed or skipped
        damages keep Muse's box and show "outline not available"."""
        try:
            damages = []
            for row in rows:
                status, shaped, _error = outcomes[row["item_id"]]
                if status != "done":
                    continue
                for number, d in enumerate(shaped["damages"], 1):
                    damages.append({**d, "item_id": row["item_id"], "slot_id": row["slot_id"],
                                    "number": number, "key": f"{row['item_id']}:{number}"})
            if not damages:
                return {}
            photos: dict[str, Image.Image | None] = {}
            for d in damages:
                if d["item_id"] not in photos:
                    photos[d["item_id"]] = _open_photo(images.get(d["item_id"]))
            # Only damages on a photo that opens compete for the cap; the order follows every photo.
            sendable = [d for d in damages if photos[d["item_id"]] is not None]
            planned = outline.plan(sendable, damage.merge(damages), self.sam_max_calls)
            by_key = {d["key"]: d for d in damages}
            results: dict[str, tuple] = {}

            def call(image: Image.Image, concept: str, d: dict, what: str) -> dict:
                return outline.ask(self.meta, image, concept, purpose=f"outline.{what}",
                                   claim_id=claim_id, item_id=d["item_id"])

            runnable = planned["run"]
            crops = {}
            for key in runnable:
                d = by_key[key]
                photo = photos[d["item_id"]]
                crops[key] = outline.crop_box(d["box"], *photo.size)
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = {
                    key: pool.submit(call, photos[by_key[key]["item_id"]].crop(crops[key]),
                                     outline.concept(by_key[key]), by_key[key], "damage")
                    for key in runnable
                }
                answers = {key: f.result() for key, f in futures.items()}
            found: dict[str, list] = {}
            for key, answer in answers.items():
                d = by_key[key]
                if not answer["ok"]:
                    results[key] = ("failed", {**outline.not_run(d, photos[d["item_id"]].size),
                                               "call_id": answer.get("call_id")}, answer["error"])
                    continue
                found[key] = outline.regions(segment.parse(answer["text"]), crops[key])
            # Panels, with what is left of the cap: one call per photo and panel concept.
            panel_jobs: dict[tuple, list[str]] = {}
            for key, regions in found.items():
                d = by_key[key]
                size = photos[d["item_id"]].size
                if outline.panel(d) and outline.derive(d["box"], regions, None, *size)["sam_agrees"] == "yes":
                    panel_jobs.setdefault((d["item_id"], outline.panel(d)), []).append(key)
            left = max(0, self.sam_max_calls - len(runnable))
            jobs = list(panel_jobs)[:left]
            with ThreadPoolExecutor(max_workers=4) as pool:
                pfutures = {job: pool.submit(call, photos[job[0]], job[1], by_key[panel_jobs[job][0]], "panel")
                            for job in jobs}
                panel_answers = {job: f.result() for job, f in pfutures.items()}
            panel_regions: dict[str, tuple] = {}
            for job, answer in panel_answers.items():
                if not answer["ok"]:
                    continue
                size = photos[job[0]].size
                regions = outline.regions(segment.parse(answer["text"]), [0, 0, *size])
                for key in panel_jobs[job]:
                    panel_regions[key] = (job[1], regions, answer.get("call_id"))
            for key, regions in found.items():
                d = by_key[key]
                width, height = photos[d["item_id"]].size
                panel_concept, p_regions, p_call = panel_regions.get(key, (None, None, None))
                derived = outline.derive(d["box"], regions, p_regions, width, height)
                if derived["sam_agrees"] == "yes" and derived["area"]["panel_pct"] is not None:
                    derived["area"]["panel"] = panel_concept
                results[key] = ("done", {
                    "concept": outline.concept(d), "sam_agrees": derived["sam_agrees"],
                    "regions": outline.public_regions(derived["regions"], width, height),
                    "area": derived["area"], "found": len(regions), "crop": crops[key],
                    "photo_size": [width, height], "call_id": answers[key].get("call_id"),
                    "panel_call_id": p_call,
                }, None)
            out: dict[str, list] = {}
            for d in damages:
                key = d["key"]
                if key not in results:
                    photo = photos[d["item_id"]]
                    if not d.get("box"):
                        reason = "no_box"
                    elif photo is None:
                        reason = "image_unreadable"
                    elif self.sam_max_calls <= 0:
                        reason = "disabled"
                    else:
                        reason = "capped"
                    results[key] = ("not_run", outline.not_run(d, photo.size if photo else None), reason)
                status, result, error = results[key]
                out.setdefault(d["item_id"], []).append((d["number"], status, result, error))
            return out
        except Exception:
            logger.exception("outline step failed for claim %s", claim_id)
            return {}

    def _read_damage(self, claim_id: str, row: dict, image: bytes | Exception) -> tuple:
        """One photo -> (status, shaped result or None, error or None); never raises."""
        try:
            if isinstance(image, Exception):
                raise image
            call = self.meta.read_json(
                purpose=f"damage.{row['slot_id']}", model=damage_prompts.MODEL,
                prompt=damage_prompts.DAMAGE, image=image, mime=row["mime"],
                claim_id=claim_id, item_id=row["item_id"],
            )
            if not call["ok"]:
                return ("failed", None, call["error"])
            shaped = damage.shape(call["json"])
            if shaped is None:
                return ("failed", None, "bad_shape")
            return ("done", shaped, None)
        except Exception as e:
            return ("failed", None, type(e).__name__)

    def run_consistency(self, claim_id: str) -> None:
        """Compare the statement with the damage reading in the background, then report; never raises."""
        try:
            self._run_consistency(claim_id)
        except Exception:
            logger.exception("consistency failed for claim %s", claim_id)
        try:
            self.make_report(claim_id)
        except Exception:
            logger.exception("make_report failed for claim %s", claim_id)

    def _run_consistency(self, claim_id: str) -> None:
        """The consistency check itself; the wrapper above still reports after a failure."""
        try:
            with self.db.tx() as conn:
                row = conn.execute(
                    "SELECT status FROM analyses WHERE claim_id=%s AND kind='consistency'",
                    (claim_id,),
                ).fetchone()
                if not row or row["status"] != "pending":
                    return
                claim = conn.execute("SELECT * FROM claims WHERE id=%s", (claim_id,)).fetchone()
                if not claim:
                    return
                reading = self._damage_view(conn, claim_id)
                answers = self._answers(claim)
                skip_story = not self._has_damage_slots(answers)
            if skip_story:
                now_skip = self.clock.now().isoformat()
                with self.db.claim_tx(claim_id=claim_id) as (c2, _c):
                    c2.execute(
                        "UPDATE analyses SET status='done', result_json=NULL, error=NULL,"
                        " finished_at=%s WHERE claim_id=%s AND kind='consistency' AND status='pending'",
                        (now_skip, claim_id),
                    )
                    c2.execute(
                        "DELETE FROM checks WHERE claim_id=%s AND check_id LIKE 'consistency.%%'",
                        (claim_id,),
                    )
                return
            if consistency.usable(reading):
                call = self.meta.read_json(
                    purpose="consistency.story", model=consistency_prompts.MODEL,
                    prompt=consistency_prompts.story_prompt(consistency.story_block(answers, reading)),
                    claim_id=claim_id,
                )
                if not call["ok"]:
                    story, error = None, call["error"]
                else:
                    story = consistency.shape_story(call["json"], len(consistency.story_damages(reading)))
                    error = None if story is not None else "bad_shape"
            else:
                story, error = None, None
            results = consistency.check(answers, reading, story)
            now = self.clock.now().isoformat()
            with self.db.claim_tx(claim_id=claim_id) as (conn, _claim):
                updated = conn.execute(
                    "UPDATE analyses SET status='done', result_json=%s, error=%s, finished_at=%s"
                    " WHERE claim_id=%s AND kind='consistency' AND status='pending'",
                    (
                        policies.canonical(story) if story is not None else None,
                        error, now, claim_id,
                    ),
                )
                if updated.rowcount == 0:
                    return
                consistency.save(conn, claim_id, results, now)
                lifecycle.append_event(
                    conn, claim_id, now, "system", "consistency.checked",
                    data={"version": f"{consistency.VERSION}+{consistency_prompts.VERSION}",
                          "results": {r["check_id"]: r["result"] for r in results}},
                )
        except Exception as e:
            logger.exception("consistency failed for claim %s", claim_id)
            try:
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE analyses SET status='failed', error=%s, finished_at=%s"
                        " WHERE claim_id=%s AND kind='consistency' AND status='pending'",
                        (type(e).__name__, self.clock.now().isoformat(), claim_id),
                    )
            except Exception:
                logger.exception("consistency failed for claim %s", claim_id)

    def _summarise(self, data: dict, claim_id: str) -> dict:
        """The AI summary for a report draft: verified, rejected or failed. Never raises."""
        facts = ai_summary.facts(data)
        try:
            call = self.meta.read_json(
                purpose="report.ai_summary", model=ai_summary.MODEL,
                prompt=ai_summary.prompt(facts), claim_id=claim_id,
            )
        except Exception as e:
            return {"status": "failed", "sentences": [], "reasons": [f"{type(e).__name__}: {e}"[:300]],
                    "attempts": 1}
        if not call.get("ok"):
            return {"status": "failed", "sentences": [], "reasons": [str(call.get("error") or "model_error")],
                    "attempts": 1}
        try:
            first = ai_summary.verify(call.get("json"), facts)
        except Exception as e:
            return {"status": "failed", "sentences": [], "reasons": [f"{type(e).__name__}: {e}"[:300]],
                    "attempts": 1}
        if first.get("status") != "rejected":
            return {**first, "attempts": 1}
        try:
            second_call = self.meta.read_json(
                purpose="report.ai_summary", model=ai_summary.MODEL,
                prompt=ai_summary.retry_prompt(facts, call.get("json"), first.get("reasons") or []),
                claim_id=claim_id,
            )
        except Exception as e:
            return {"status": "failed", "sentences": [], "reasons": [f"{type(e).__name__}: {e}"[:300]],
                    "attempts": 2}
        if not second_call.get("ok"):
            return {"status": "failed", "sentences": [],
                    "reasons": [str(second_call.get("error") or "model_error")], "attempts": 2}
        try:
            second = ai_summary.verify(second_call.get("json"), facts)
        except Exception as e:
            return {"status": "failed", "sentences": [], "reasons": [f"{type(e).__name__}: {e}"[:300]],
                    "attempts": 2}
        if second.get("status") == "rejected":
            return {"status": "rejected", "sentences": [],
                    "reasons": list(first.get("reasons") or []) + list(second.get("reasons") or []),
                    "attempts": 2}
        return {**second, "attempts": 2}

    def make_report(self, claim_id: str) -> None:
        """Build the verified evidence report and its outbox message once, after the
        analysis is done. Called at the end of run_damage and run_consistency; a
        missing or duplicate report is a quiet no-op, unexpected failures raise
        so the caller can log and swallow them."""
        detail = self.claim_detail(claim_id)
        proposal = detail.get("proposal")
        if not proposal or proposal.get("status") != "ready":
            return
        with self.db.tx() as conn:
            rnd = conn.execute("SELECT round FROM claims WHERE id=%s", (claim_id,)).fetchone()["round"]
            if self._has_report(conn, claim_id, rnd):
                return
            org = conn.execute("SELECT name FROM orgs WHERE id=%s", (self.org_id,)).fetchone()
            snapshot_id = conn.execute(
                "SELECT policy_snapshot_id FROM claims WHERE id=%s", (claim_id,)
            ).fetchone()["policy_snapshot_id"]
            snapshot = conn.execute(
                "SELECT * FROM policy_snapshots WHERE id=%s", (snapshot_id,)
            ).fetchone()
            tz = self._claim_tz(conn, snapshot)
            paths = {r["id"]: r["path"] for r in conn.execute(
                "SELECT id, path FROM items WHERE claim_id=%s", (claim_id,))}
        now = self.clock.now()
        data = report.build(detail, org["name"], now, tz)
        data["test_mode"] = bool(detail.get("test_mode"))
        data["review"] = detail.get("review") or {"flags": {}, "damages": {}, "notes": []}
        data["ai_summary"] = self._summarise(data, claim_id)
        images: dict[str, bytes] = {}
        for photo in data["photos"]:
            key = paths.get(photo["item_id"])
            if not key:
                continue
            try:
                images[photo["item_id"]] = self.blobs.get(key)
            except Exception:
                continue  # a missing photo is left out of the PDF, not a failure
        pdf = report_pdf.render(data, images, data["ai_summary"])
        digest = hashlib.sha256(pdf).hexdigest()
        expires = now + timedelta(days=report.LINK_DAYS)
        outbox_id = uuid.uuid4().hex
        blob_key = f"{_report_dir(claim_id, rnd)}/{report.PDF_NAME}"
        mode_row = self.delivery_mode()
        with self.db.claim_tx(claim_id=claim_id) as (conn, claim):
            if claim["round"] != rnd or self._has_report(conn, claim_id, rnd):
                return  # the other background job won, or the claim moved on
            earlier = conn.execute("SELECT token FROM reports WHERE claim_id=%s", (claim_id,)).fetchone()
            # A later round keeps the secret link the insurer already holds; it serves the latest report.
            token = earlier["token"] if earlier else secrets.token_urlsafe(32)  # 256 bits, unguessable
            body = report.package(
                data, outbox_id, digest, f"/api/r/{token}", f"/api/r/{token}/package")
            if rnd > 1:
                body["round"] = rnd
                body["complete_activities"] = ["Follow up on requested photos"]  # from the Send back
            self.blobs.put(blob_key, pdf, "application/pdf")
            conn.execute(
                "INSERT INTO reports (claim_id, token, path, sha256, version, created_at, expires_at,"
                " ai_summary_json, round) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (claim_id) DO UPDATE SET path=EXCLUDED.path, sha256=EXCLUDED.sha256,"
                " version=EXCLUDED.version, created_at=EXCLUDED.created_at, expires_at=EXCLUDED.expires_at,"
                " ai_summary_json=EXCLUDED.ai_summary_json, round=EXCLUDED.round,"
                " reviewed_path=NULL, reviewed_sha256=NULL, reviewed_at=NULL",
                (claim_id, token, blob_key, digest, report.VERSION,
                 now.isoformat(), expires.isoformat(), policies.canonical(data["ai_summary"]), rnd),
            )
            if mode_row != "on_decision":
                conn.execute(
                    "INSERT INTO outbox (id, claim_id, event, body_json, created_at, attempts, round)"
                    " VALUES (%s, %s, 'evidence.ready', %s, %s, 0, %s)",
                    (outbox_id, claim_id, policies.canonical(body), now.isoformat(), rnd),
                )
            else:
                # The report waits for the review; the claims system still gets an
                # activity so the adjuster knows there is something to review (track 3).
                pending = report.pending_review(data, outbox_id, f"/claims/{claim_id}")
                if rnd > 1:
                    pending["round"] = rnd
                    pending["complete_activities"] = ["Follow up on requested photos"]
                conn.execute(
                    "INSERT INTO outbox (id, claim_id, event, body_json, created_at, attempts, round)"
                    " VALUES (%s, %s, 'evidence.pending_review', %s, %s, 0, %s)",
                    (outbox_id, claim_id, policies.canonical(pending), now.isoformat(), rnd),
                )
            lifecycle.append_event(
                conn, claim_id, now.isoformat(), "system", "report.ready",
                data={"sha256": digest, "version": report.VERSION, "outbox_id": outbox_id,
                      **({"round": rnd} if rnd > 1 else {})},
            )
        # After the transaction commits, and only when this call created the
        # report (the early return above skips delivery when it already existed).
        try:
            self.deliver(claim_id)
        except Exception:
            logger.exception("deliver failed for claim %s", claim_id)

    @staticmethod
    def _has_report(conn: psycopg.Connection, claim_id: str, rnd: int) -> bool:
        """A report exists for this round of the claim (Send back starts a new round)."""
        return conn.execute(
            "SELECT 1 FROM reports WHERE claim_id=%s AND round >= %s", (claim_id, rnd)
        ).fetchone() is not None

    def report_by_token(self, token: str, version: str | None = None) -> tuple[bytes, str]:
        """The stored PDF and claim id behind a secret link; unknown is 404, expired is 410.

        Serves the reviewed PDF once the decision made it, else the draft;
        `?version=draft` always serves the draft. After media removal the PDF
        answers 410; the package link keeps working.
        """
        row = self._report_row(token)
        if row.get("removed_at"):
            raise CaptureError(f"Removed on {row['removed_at']}", 410)
        with self.db.tx() as conn:
            claim = conn.execute(
                "SELECT removal_json FROM claims WHERE id=%s", (row["claim_id"],)
            ).fetchone()
        if claim and claim.get("removal_json"):
            try:
                receipt = json.loads(claim["removal_json"])
                when = receipt.get("removed_at") or row["removed_at"]
            except ValueError:
                when = row["removed_at"]
            raise CaptureError(f"Removed on {when}", 410)
        if version != "draft" and row.get("reviewed_path"):
            try:
                return self.blobs.get(row["reviewed_path"]), row["claim_id"]
            except BlobNotFound:
                pass
        try:
            return self.blobs.get(row["path"]), row["claim_id"]
        except BlobNotFound:
            raise CaptureError("Report file is missing", 500) from None

    def package_by_token(self, token: str) -> tuple[bytes, str]:
        """The sealed zip behind a secret link; same unknown/expired checks as the PDF."""
        row = self._report_row(token)
        return self.package(row["claim_id"]), row["claim_id"]

    def report_pdf_by_claim(self, claim_id: str) -> bytes:
        """The stored PDF for the adjuster screen; 404 before the report exists, 410 after removal."""
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM reports WHERE claim_id=%s", (claim_id,)).fetchone()
            claim = conn.execute("SELECT removal_json FROM claims WHERE id=%s", (claim_id,)).fetchone()
        if not row:
            raise CaptureError("Report not found", 404)
        if row.get("removed_at") or (claim and claim.get("removal_json")):
            when = row.get("removed_at")
            if claim and claim.get("removal_json"):
                try:
                    when = json.loads(claim["removal_json"]).get("removed_at") or when
                except ValueError:
                    pass
            raise CaptureError(f"Removed on {when}", 410)
        try:
            return self.blobs.get(row["path"])
        except BlobNotFound:
            raise CaptureError("Report file is missing", 500) from None

    def _report_row(self, token: str) -> dict:
        """The reports row for a secret link token; unknown is 404, expired is 410."""
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM reports WHERE token=%s", (token,)).fetchone()
        if not row:
            raise CaptureError("Report not found", 404)
        if datetime.fromisoformat(row["expires_at"]) < self.clock.now():
            raise CaptureError("This report link has expired", 410)
        return row

    def _report_view(self, conn: psycopg.Connection, claim_id: str) -> dict | None:
        """The report card for claim_detail: None before the report exists."""
        row = conn.execute("SELECT * FROM reports WHERE claim_id=%s", (claim_id,)).fetchone()
        if not row:
            return None
        outbox = conn.execute(
            "SELECT * FROM outbox WHERE claim_id=%s ORDER BY created_at, id", (claim_id,)).fetchall()
        return {
            "url": f"/api/r/{row['token']}",
            "sha256": row["sha256"],
            "version": row["version"],
            "round": row["round"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "outbox": [
                {"id": m["id"], "event": m["event"], "round": m["round"], "created_at": m["created_at"],
                 "delivered_at": m["delivered_at"], "attempts": m["attempts"],
                 "last_error": m["last_error"], "next_attempt_at": m["next_attempt_at"],
                 "dead_at": m["dead_at"], "body": json.loads(m["body_json"])}
                for m in outbox
            ],
        }

    def _consistency_status(self, conn: psycopg.Connection, claim_id: str) -> dict:
        row = conn.execute(
            "SELECT status, started_at FROM analyses WHERE claim_id=%s AND kind='consistency'",
            (claim_id,),
        ).fetchone()
        return self._consistency_from_row(row)

    def _consistency_from_row(self, row: dict | None) -> dict:
        if not row:
            return {"status": "none"}
        if row["status"] == "pending":
            try:
                stale = self.clock.now() - datetime.fromisoformat(row["started_at"]) > damage.STALE_AFTER
            except (TypeError, ValueError):
                stale = False
            return {"status": "failed" if stale else "pending"}
        return {"status": row["status"]}

    def _proposal_view(self, claim: dict, checks: list[dict], reading: dict, consistency_status: dict) -> dict | None:
        """The adjuster's proposal, computed on read from stored rows; the claimant never sees it."""
        if claim["submitted_at"] is None:
            return None
        if reading["status"] == "pending" or consistency_status["status"] == "pending":
            return {"status": "pending"}
        facts = json.loads(claim["facts_json"])
        raw = facts.get("deductible_amount") if isinstance(facts, dict) else None
        deductible = raw.get("value") if isinstance(raw, dict) else None
        if type(deductible) is not int:
            deductible = None
        return {
            "status": "ready",
            **proposal.build(
                checks, json.loads(claim["reason_codes_json"]), reading,
                estimate.rough_range(reading), deductible, self.lane_rule,
            ),
        }

    def _damage_view(self, conn: psycopg.Connection, claim_id: str) -> dict:
        rows = conn.execute(
            "SELECT a.item_id, i.step_id AS slot_id, a.status, a.result_json, a.error, a.started_at"
            " FROM analyses a JOIN items i ON i.id = a.item_id"
            " WHERE a.claim_id=%s AND a.kind='damage' AND i.superseded=0"
            " ORDER BY i.received_at, i.id",
            (claim_id,),
        ).fetchall()
        return self._damage_from_rows(rows)

    def _damage_from_rows(self, rows: list[dict]) -> dict:
        return damage.view(
            [
                {
                    "item_id": r["item_id"],
                    "slot_id": r["slot_id"],
                    "status": r["status"],
                    "result": json.loads(r["result_json"]) if r["result_json"] else None,
                    "error": r["error"],
                    "started_at": r["started_at"],
                }
                for r in rows
            ],
            self.clock.now(),
        )

    def _photo_view(self, conn: psycopg.Connection, claim: dict, snapshot: dict) -> list[dict]:
        view_rows, ctx = self._photo_rows(conn, claim, snapshot)
        return photo_check.view(view_rows, ctx, self.clock.now())

    def _photo_rows(self, conn: psycopg.Connection, claim: dict, snapshot: dict) -> tuple[list[dict], dict]:
        """Every checked-slot item (superseded too) with its readings, and the decide() context."""
        rows = conn.execute(
            "SELECT i.id AS item_id, i.step_id AS slot_id, i.received_at, i.superseded,"
            " p.status, p.readings_json, p.started_at, p.error FROM items i"
            " LEFT JOIN photo_checks p ON p.item_id = i.id"
            " WHERE i.claim_id=%s AND i.step_id = ANY(%s) ORDER BY i.received_at, i.id",
            (claim["id"], list(photo_check.CHECKED_SLOTS)),
        ).fetchall()
        view_rows = [
            {
                "item_id": r["item_id"],
                "slot_id": r["slot_id"],
                "received_at": r["received_at"],
                "superseded": bool(r["superseded"]),
                "status": r["status"],
                "readings": json.loads(r["readings_json"]) if r["readings_json"] else None,
                "started_at": r["started_at"],
                "error": r["error"],
            }
            for r in rows
        ]
        data = json.loads(snapshot["data_json"]) if snapshot["data_json"] else None
        ctx = {
            "policy": data if snapshot["verified"] else None,
            "answers": self._answers(claim),
            "today": self.clock.now().date(),
        }
        return view_rows, ctx

    def _log_hint_edits(self, conn: psycopg.Connection, claim: dict, snapshot: dict, answers: dict) -> None:
        """Form edits made while a photo hint invites them, so an adjuster can tell a correction from coaching."""
        now = self.clock.now().isoformat()
        for edit in photo_check.hint_edits(self._photo_view(conn, claim, snapshot), self._answers(claim), answers):
            lifecycle.append_event(conn, claim["id"], now, "customer", "answers.changed_after_photo_hint", data=edit)

