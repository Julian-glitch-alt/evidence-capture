"""Store domain mixin: retention (split from store.py, behaviour unchanged)."""

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


class RetentionMixin:
    def delete_claim_media(self, claim_id: str, reason: str, user: dict) -> dict:
        """POST /api/admin/claims/{id}/delete-media (supervisors only): remove the
        claim's photos, video, signature image and our report PDFs now, whatever
        their age. Only for a claim that was sent; already removed is 409."""
        note = reason if isinstance(reason, str) else ""
        if not isinstance(note, str) or not 5 <= len(note.strip()) <= 500:
            raise CaptureError("reason must be 5-500 characters", 400)
        note = note.strip()
        with self.db.tx() as conn:
            claim = conn.execute("SELECT * FROM claims WHERE id=%s", (claim_id,)).fetchone()
        if not claim:
            raise CaptureError("Claim not found", 404)
        if claim.get("removal_json"):
            raise CaptureError("This claim's media was already deleted", 409)
        if claim.get("submitted_at") is None:
            raise CaptureError("Only a sent claim's media can be deleted", 409)
        self._remove_media(
            claim_id, reason="request", by=user["name"], actor="user",
            user_id=user["id"], user_name=user["name"], note=note,
        )
        return {"ok": True}

    def _remove_media(self, claim_id: str, reason: str, by: str, actor: str,
                      user_id: str | None, user_name: str | None, note: str | None) -> dict:
        """Record the removal receipt on the claim, then delete the blobs.

        Rows first, blobs after: a crash in between leaves rows saying removed
        and some blobs still there, which the next sweep cleans; never the
        other way round.
        """
        now = self.clock.now().isoformat()
        with self.db.claim_tx(claim_id=claim_id) as (conn, claim):
            if claim.get("removal_json"):
                raise CaptureError("This claim's media was already deleted", 409)
            seals = conn.execute(
                "SELECT manifest_json FROM seals WHERE claim_id=%s ORDER BY round", (claim_id,)
            ).fetchall()
            seen: dict[str, str | None] = {}
            for s in seals:
                try:
                    manifest = json.loads(s["manifest_json"])
                except ValueError:
                    continue
                for item in manifest.get("items", []) or []:
                    if isinstance(item, dict) and isinstance(item.get("file"), str):
                        seen.setdefault(item["file"], item.get("sha256"))
            removed = [{"file": name, "sha256": seen[name]} for name in sorted(seen)]
            receipt = {"claim_id": claim_id, "removed": removed, "removed_at": now,
                       "reason": reason, "by": by}
            body, signature = self.sealer.sign(receipt)
            item_paths = [r["path"] for r in conn.execute(
                "SELECT path FROM items WHERE claim_id=%s", (claim_id,)).fetchall()]
            report_paths = []
            rep = conn.execute("SELECT path, reviewed_path FROM reports WHERE claim_id=%s",
                               (claim_id,)).fetchone()
            if rep:
                if rep.get("path"):
                    report_paths.append(rep["path"])
                if rep.get("reviewed_path"):
                    report_paths.append(rep["reviewed_path"])
            conn.execute(
                "UPDATE claims SET removal_json=%s, removal_signature=%s WHERE id=%s",
                (body.decode(), signature, claim_id),
            )
            conn.execute(
                "UPDATE items SET removed_at=%s, removed_reason=%s WHERE claim_id=%s",
                (now, reason, claim_id),
            )
            conn.execute("UPDATE reports SET removed_at=%s WHERE claim_id=%s", (now, claim_id))
            lifecycle.append_event(
                conn, claim_id, now, actor, "media.removed",
                data={"reason": reason, "note": note, "files": len(removed),
                      "user_id": user_id, "user_name": user_name},
            )
        failed = False
        for key in item_paths + report_paths:
            try:
                self.blobs.delete(key)
            except BlobNotFound:
                pass
            except Exception:
                logger.exception("removal delete failed for claim %s key %s", claim_id, key)
                failed = True
        if not failed:
            try:
                if all(not self.blobs.exists(key) for key in item_paths + report_paths):
                    with self.db.tx() as conn:
                        conn.execute(
                            "UPDATE claims SET removal_done_at=%s"
                            " WHERE id=%s AND removal_done_at IS NULL",
                            (now, claim_id),
                        )
            except Exception:
                logger.exception("removal done stamp failed for claim %s", claim_id)
        return receipt

    def _clean_removed_blobs(self, claim_id: str) -> bool:
        """Delete any blobs left behind by a crashed removal; rows already say removed.

        Returns True once every blob is confirmed gone, and stamps
        `claims.removal_done_at` then, so later sweeps skip this claim.
        A storage error leaves `removal_done_at` empty for the next sweep.
        """
        with self.db.tx() as conn:
            paths = [r["path"] for r in conn.execute(
                "SELECT path FROM items WHERE claim_id=%s", (claim_id,)).fetchall()]
            rep = conn.execute("SELECT path, reviewed_path FROM reports WHERE claim_id=%s",
                               (claim_id,)).fetchone()
        keys = list(paths)
        if rep:
            if rep.get("path"):
                keys.append(rep["path"])
            if rep.get("reviewed_path"):
                keys.append(rep["reviewed_path"])
        failed = False
        for key in keys:
            try:
                if self.blobs.exists(key):
                    self.blobs.delete(key)
            except Exception:
                logger.exception("removal cleanup failed for claim %s key %s", claim_id, key)
                failed = True
        if failed:
            return False
        try:
            if all(not self.blobs.exists(key) for key in keys):
                with self.db.tx() as conn:
                    conn.execute(
                        "UPDATE claims SET removal_done_at=%s"
                        " WHERE id=%s AND removal_done_at IS NULL",
                        (self.clock.now().isoformat(), claim_id),
                    )
                return True
            return False
        except Exception:
            logger.exception("removal cleanup check failed for claim %s", claim_id)
            return False

    def _sweep_retention(self, out: dict) -> None:
        """Claims whose latest decision is older than retention_days, that still
        have media, and whose outbox is fully delivered or dead: remove the media.

        The candidates come from one query: this org's claims with media left and
        no removal, whose latest decision (`MAX(decided_at)` as `timestamptz`) is
        older than the cutoff, ordered by that latest decision, at most
        `RETENTION_SWEEP_LIMIT` rows. Old claims without a decision never hold the
        others back. A claim with no removal work left behind is never selected.

        Outbox gate: a pending message blocks only a claim that has an insurer
        callback (or whose message was actually attempted: attempts >= 1). A claim
        opened in the console has no callback and never attempts delivery, so its
        queued messages never block it.
        """
        # Resolved through the facade so patching evidence_server.store works.
        from . import store as _facade

        sweep_limit = _facade.RETENTION_SWEEP_LIMIT
        days = self.retention_days()
        cutoff = (self.clock.now() - timedelta(days=days)).isoformat()
        with self.db.tx() as conn:
            unfinished = [r["id"] for r in conn.execute(
                "SELECT id FROM claims WHERE org_id=%s AND removal_json IS NOT NULL"
                " AND removal_done_at IS NULL ORDER BY created_at LIMIT %s",
                (self.org_id, sweep_limit)).fetchall()]
            candidate_ids = [r["id"] for r in conn.execute(
                "SELECT c.id FROM claims c JOIN decisions d ON d.claim_id=c.id"
                " WHERE c.org_id=%s AND c.removal_json IS NULL"
                " AND EXISTS (SELECT 1 FROM items i WHERE i.claim_id=c.id AND i.removed_at IS NULL)"
                " AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.claim_id=c.id"
                " AND o.delivered_at IS NULL AND o.dead_at IS NULL"
                " AND (c.callback_url IS NOT NULL OR o.attempts >= 1))"
                " GROUP BY c.id HAVING MAX(d.decided_at::timestamptz) < %s::timestamptz"
                " ORDER BY MAX(d.decided_at::timestamptz) LIMIT %s",
                (self.org_id, cutoff, sweep_limit)).fetchall()]
        for claim_id in unfinished:
            try:
                self._clean_removed_blobs(claim_id)
            except Exception:
                logger.exception("retention cleanup failed for claim %s", claim_id)
        done = 0
        for claim_id in candidate_ids:
            try:
                self._remove_media(
                    claim_id, reason="retention", by="retention policy", actor="system",
                    user_id=None, user_name=None, note=None,
                )
                done += 1
            except CaptureError:
                continue
            except Exception:
                logger.exception("retention removal failed for claim %s", claim_id)
        out["retention"] = out.get("retention", 0) + done

    def sweep(self) -> dict:
        """Finish background work a lost container left behind. Safe to run often and
        from several places at once (each row is claimed before it is worked on).

        1. Photo checks and analyses still pending after SWEEP_STALE_AFTER are run
           again, at most ANALYSIS_MAX_ATTEMPTS times in all; after that they are
           marked failed ("stale"), so the proposal shows them as missing, never as 0.
        2. Submitted claims whose proposal is ready but have no report get one.
        3. Outbox messages that are due are posted again (backoff in _delivery_failed).
        4. Old per-IP request counters are deleted.
        5. Media older than retention_days after the decision is removed (T50).
        Returns counts for the log.
        """
        out = {"photo_checks_rerun": 0, "photo_checks_given_up": 0, "analyses_rerun": 0,
               "analyses_given_up": 0, "reports_built": 0, "delivered": 0, "delivery_failed": 0,
               "rate_limit_rows_pruned": 0, "retention": 0}
        started_at = self.clock.now().isoformat()
        errors: list[str] = []
        steps = (self._sweep_photo_checks, self._sweep_analyses, self._sweep_reports, self._sweep_outbox,
                 self._sweep_rate_limits, self._sweep_retention)
        for step in steps:
            try:
                step(out)
            except Exception as e:
                logger.exception("sweep step %s failed", step.__name__)
                errors.append(f"{step.__name__}: {type(e).__name__}: {e}")
        finished_at = self.clock.now().isoformat()
        try:
            with self.db.tx() as conn:
                conn.execute(
                    "INSERT INTO ops_runs (id, kind, started_at, finished_at, result_json, error)"
                    " VALUES (%s, 'sweep', %s, %s, %s, %s)",
                    (uuid.uuid4().hex, started_at, finished_at, policies.canonical(out),
                     "; ".join(errors) if errors else None),
                )
                conn.execute(
                    "DELETE FROM ops_runs WHERE id NOT IN (SELECT id FROM ops_runs"
                    " ORDER BY finished_at DESC, started_at DESC, id DESC LIMIT 500)")
        except Exception:
            logger.exception("sweep record failed")
        return out

    def health(self) -> dict:
        """System health for the console page: failures and deliveries over the last 24 hours.

        Counts the analyses and photo checks that failed and finished in the window,
        the outbox messages dead, retrying or delivered, the model calls made in the
        window, the latest sweep run, and up to 20 claim ids involved in any of those
        failures, most recent first. Never raises for the shape above: an empty system
        reports zeros, a null rate and a null sweep.
        """
        now = self.clock.now()
        cutoff = (now - timedelta(hours=24)).isoformat()
        live_only = ("(claim_id IS NULL OR claim_id NOT IN"
                       " (SELECT id FROM claims WHERE test_mode))")
        with self.db.tx() as conn:
            analyses_failed = conn.execute(
                "SELECT COUNT(*) AS n FROM analyses WHERE status='failed' AND finished_at IS NOT NULL"
                " AND finished_at::timestamptz >= %s::timestamptz AND " + live_only,
                (cutoff,)).fetchone()["n"]
            photo_checks_failed = conn.execute(
                "SELECT COUNT(*) AS n FROM photo_checks WHERE status='failed' AND finished_at IS NOT NULL"
                " AND finished_at::timestamptz >= %s::timestamptz"
                " AND (error IS NULL OR error != 'license_check_off') AND " + live_only,
                (cutoff,)).fetchone()["n"]
            outbox = conn.execute(
                "SELECT COUNT(*) FILTER (WHERE dead_at IS NOT NULL AND delivered_at IS NULL) AS dead,"
                " COUNT(*) FILTER (WHERE delivered_at IS NULL AND dead_at IS NULL AND attempts >= 1) AS retrying,"
                " COUNT(*) FILTER (WHERE delivered_at IS NOT NULL"
                " AND delivered_at::timestamptz >= %s::timestamptz) AS delivered"
                " FROM outbox WHERE " + live_only, (cutoff,)).fetchone()
            calls = conn.execute(
                "SELECT COUNT(*) AS total,"
                " COUNT(*) FILTER (WHERE status IN ('error', 'timeout', 'bad_json')) AS errors,"
                " COUNT(*) FILTER (WHERE status='budget') AS budget,"
                " COALESCE(SUM(cost_usd), 0) AS cost FROM model_calls"
                " WHERE created_at::timestamptz >= %s::timestamptz AND " + live_only,
                (cutoff,)).fetchone()
            sweep_row = conn.execute(
                "SELECT finished_at, result_json, error FROM ops_runs WHERE kind='sweep'"
                " ORDER BY finished_at DESC LIMIT 1").fetchone()
            failed: list[dict] = []
            failed += conn.execute(
                "SELECT claim_id, finished_at AS ts FROM analyses WHERE status='failed'"
                " AND finished_at IS NOT NULL AND finished_at::timestamptz >= %s::timestamptz"
                " AND " + live_only,
                (cutoff,)).fetchall()
            failed += conn.execute(
                "SELECT claim_id, finished_at AS ts FROM photo_checks WHERE status='failed'"
                " AND finished_at IS NOT NULL AND finished_at::timestamptz >= %s::timestamptz"
                " AND (error IS NULL OR error != 'license_check_off') AND " + live_only,
                (cutoff,)).fetchall()
            failed += conn.execute(
                "SELECT claim_id, dead_at AS ts FROM outbox"
                " WHERE dead_at IS NOT NULL AND delivered_at IS NULL AND " + live_only).fetchall()
            failed += conn.execute(
                "SELECT claim_id, created_at AS ts FROM outbox"
                " WHERE delivered_at IS NULL AND dead_at IS NULL AND attempts >= 1"
                " AND " + live_only).fetchall()
            failed += conn.execute(
                "SELECT claim_id, created_at AS ts FROM model_calls"
                " WHERE status IN ('error', 'timeout', 'bad_json') AND claim_id IS NOT NULL"
                " AND created_at::timestamptz >= %s::timestamptz AND " + live_only,
                (cutoff,)).fetchall()
        latest: dict[str, str] = {}
        for row in failed:
            if row["claim_id"] is None or row["ts"] is None:
                continue
            if row["claim_id"] not in latest or row["ts"] > latest[row["claim_id"]]:
                latest[row["claim_id"]] = row["ts"]
        total = calls["total"]
        return {
            "window_hours": 24,
            "generated_at": now.isoformat(),
            "model": meta_client.PROVIDERS[self.meta.provider],
            "accuracy": json.loads(ACCURACY_FILE.read_text()) if ACCURACY_FILE.exists() else None,
            "analyses_failed": analyses_failed,
            "photo_checks_failed": photo_checks_failed,
            "outbox": {"dead": outbox["dead"], "retrying": outbox["retrying"],
                       "delivered": outbox["delivered"]},
            "model_calls": {"total": total, "errors": calls["errors"],
                            "error_rate": round(calls["errors"] / total, 3) if total else None,
                            "budget_refused": calls["budget"], "cost_usd": float(calls["cost"])},
            "last_sweep": ({"at": sweep_row["finished_at"], "result": json.loads(sweep_row["result_json"]),
                             "error": sweep_row["error"]} if sweep_row else None),
            "claims": sorted(latest, key=lambda c: latest[c], reverse=True)[:20],
        }

    def feedback(self) -> dict:
        """Adjuster feedback on flags (T49): one row per reason code found in
        review rows of kind flag. The latest verdict per claim and flag key
        counts once. -> {"rows": [...], "reviews": n}."""
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT r.claim_id, r.target, r.verdict, r.at FROM reviews r"
                " JOIN claims c ON c.id = r.claim_id"
                " WHERE r.kind='flag' AND c.org_id=%s ORDER BY r.at",
                (self.org_id,),
            ).fetchall()
        latest: dict[tuple, str] = {}
        for row in rows:
            target = row["target"]
            if not isinstance(target, str) or not target:
                continue
            verdict = row["verdict"]
            if verdict not in ("confirmed", "false_alarm"):
                continue
            latest[(row["claim_id"], target)] = verdict
        counts: dict[str, list[int]] = {}
        for (_claim_id, target), verdict in latest.items():
            code = target.rsplit(":", 1)[-1] if ":" in target else target
            entry = counts.setdefault(code, [0, 0])
            entry[0 if verdict == "confirmed" else 1] += 1
        out = []
        for code, (confirmed, false_alarm) in counts.items():
            total = confirmed + false_alarm
            out.append({
                "reason_code": code,
                "confirmed": confirmed,
                "false_alarm": false_alarm,
                "total": total,
                "confirm_rate": round(confirmed / total, 3) if total else 0,
                "ci": evaluation.wilson(confirmed, total),
            })
        out.sort(key=lambda r: (-r["total"], r["reason_code"]))
        return {"rows": out, "reviews": sum(r["total"] for r in out)}

    def _stale_cutoff(self) -> str:
        return (self.clock.now() - SWEEP_STALE_AFTER).isoformat()

    def _sweep_photo_checks(self, out: dict) -> None:
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT item_id, claim_id, slot_id, attempts, started_at FROM photo_checks"
                " WHERE status='pending' AND started_at::timestamptz < %s::timestamptz",
                (self._stale_cutoff(),),
            ).fetchall()
        for row in rows:
            if row["attempts"] >= ANALYSIS_MAX_ATTEMPTS:
                self._finish_check(row["item_id"], row["claim_id"], row["slot_id"], "failed", None, "stale")
                out["photo_checks_given_up"] += 1
                continue
            with self.db.tx() as conn:
                claimed = conn.execute(
                    "UPDATE photo_checks SET started_at=%s, attempts=attempts+1"
                    " WHERE item_id=%s AND status='pending' AND started_at=%s",
                    (self.clock.now().isoformat(), row["item_id"], row["started_at"]),
                ).rowcount
            if claimed:
                self.run_photo_check(row["item_id"])
                out["photo_checks_rerun"] += 1

    def _sweep_analyses(self, out: dict) -> None:
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT claim_id, kind, item_id, attempts, started_at FROM analyses"
                " WHERE status='pending' AND started_at::timestamptz < %s::timestamptz"
                " ORDER BY claim_id, kind",
                (self._stale_cutoff(),),
            ).fetchall()
        rerun: dict[str, set[str]] = {}
        for row in rows:
            now = self.clock.now().isoformat()
            with self.db.tx() as conn:
                if row["attempts"] >= ANALYSIS_MAX_ATTEMPTS:
                    gave_up = conn.execute(
                        "UPDATE analyses SET status='failed', error='stale', finished_at=%s"
                        " WHERE claim_id=%s AND kind=%s AND item_id=%s AND status='pending'",
                        (now, row["claim_id"], row["kind"], row["item_id"]),
                    ).rowcount
                    out["analyses_given_up"] += gave_up
                    if gave_up:
                        rerun.setdefault(row["claim_id"], set())  # still needs its report
                    continue
                claimed = conn.execute(
                    "UPDATE analyses SET started_at=%s, attempts=attempts+1"
                    " WHERE claim_id=%s AND kind=%s AND item_id=%s AND status='pending' AND started_at=%s",
                    (now, row["claim_id"], row["kind"], row["item_id"], row["started_at"]),
                ).rowcount
            if claimed:
                rerun.setdefault(row["claim_id"], set()).add(row["kind"])
                out["analyses_rerun"] += 1
        for claim_id, kinds in rerun.items():
            # Damage first: the consistency check reads the damage reading.
            if "damage" in kinds:
                self._run_damage(claim_id)
            if "consistency" in kinds:
                self._run_consistency(claim_id)
            try:
                self.make_report(claim_id)
            except Exception:
                logger.exception("make_report failed for claim %s", claim_id)

    def _sweep_reports(self, out: dict) -> None:
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT c.id FROM claims c LEFT JOIN reports r ON r.claim_id = c.id"
                " WHERE c.org_id=%s AND c.submitted_at IS NOT NULL AND (r.claim_id IS NULL OR r.round < c.round)"
                " AND NOT EXISTS (SELECT 1 FROM analyses a WHERE a.claim_id = c.id AND a.status='pending'"
                " AND a.started_at::timestamptz >= %s::timestamptz)",
                (self.org_id, self._stale_cutoff()),
            ).fetchall()
        for row in rows:
            try:
                self.make_report(row["id"])
            except Exception:
                logger.exception("make_report failed for claim %s", row["id"])
                continue
            with self.db.tx() as conn:
                if conn.execute(
                    "SELECT 1 FROM reports r JOIN claims c ON c.id = r.claim_id AND r.round = c.round"
                    " WHERE r.claim_id=%s", (row["id"],)
                ).fetchone():
                    out["reports_built"] += 1

    def _sweep_rate_limits(self, out: dict) -> None:
        if self.limiter is not None:
            out["rate_limit_rows_pruned"] += self.limiter.prune()

    def _sweep_outbox(self, out: dict) -> None:
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT DISTINCT o.claim_id FROM outbox o JOIN claims c ON c.id = o.claim_id"
                " WHERE o.delivered_at IS NULL AND o.dead_at IS NULL AND c.callback_url IS NOT NULL"
                " AND (o.next_attempt_at IS NULL OR o.next_attempt_at::timestamptz <= %s::timestamptz)",
                (self.clock.now().isoformat(),),
            ).fetchall()
        for row in rows:
            counts = self.deliver(row["claim_id"])
            out["delivered"] += counts["delivered"]
            out["delivery_failed"] += counts["failed"]

