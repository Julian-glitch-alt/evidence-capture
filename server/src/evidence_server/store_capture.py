"""Store domain mixin: capture (split from store.py, behaviour unchanged)."""

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


class CaptureMixin:
    def item_file(self, item_id: str) -> ItemFile:
        with self.db.tx() as conn:
            row = conn.execute("SELECT path, mime, removed_at FROM items WHERE id=%s", (item_id,)).fetchone()
        if not row:
            raise CaptureError("Item not found", 404)
        if row.get("removed_at"):
            raise CaptureError(f"Removed on {row['removed_at']}", 410)
        return self._file_target(row["path"], row["mime"])

    def session_state(self, token: str) -> dict:
        self._reopen_if_sent_back(token)
        with self.db.tx() as conn:
            claim = conn.execute("SELECT * FROM claims WHERE token=%s", (token,)).fetchone()
            if not claim:
                raise CaptureError("This link is not valid", 404)
            self._check_open(claim, allow_submitted=True)
            items = conn.execute(
                "SELECT * FROM items WHERE claim_id=%s AND superseded=0 ORDER BY received_at",
                (claim["id"],),
            ).fetchall()
            snapshot = conn.execute(
                "SELECT * FROM policy_snapshots WHERE id=%s", (claim["policy_snapshot_id"],)
            ).fetchone()
            org = conn.execute("SELECT name, brand_color FROM orgs WHERE id=%s", (self.org_id,)).fetchone()
            return {
                "claim_id": claim["id"],
                "status": claim["status"],
                "insurer_claim_number": claim["insurer_claim_number"],
                "brand": {"name": org["name"], "color": org["brand_color"] or DEFAULT_BRAND_COLOR},
                "answers": self._answers(claim),
                "layout": self._layout(claim, snapshot, items),
                "items": [self._item_public(i) for i in items],
                "photo_checks": photo_check.public(self._photo_view(conn, claim, snapshot)),
                # After a Send back: the only slots the claimant may (and must) retake,
                # and those still without a shot taken since the decision.
                "requested_slots": json.loads(claim["reopen_slots_json"]) if claim["reopen_slots_json"] else [],
                "retakes_left": self._retakes_left(conn, claim, items),
            }

    def _reopen_if_sent_back(self, token: str) -> None:
        """The claimant opens a link the adjuster sent back: more_evidence -> capturing.
        Answers stay as sealed; only the requested photos can be taken (track 3)."""
        with self.db.tx() as conn:
            row = conn.execute("SELECT status FROM claims WHERE token=%s", (token,)).fetchone()
        if not row or row["status"] != "more_evidence":
            return
        with self.db.claim_tx(token=token) as (conn, claim):
            if claim["status"] != "more_evidence":
                return  # another request reopened it first
            if datetime.fromisoformat(claim["expires_at"]) < self.clock.now():
                raise CaptureError("This link has expired", 410)
            lifecycle.transition(
                conn, claim["id"], "capturing", "customer", self.clock.now().isoformat(),
                {"reopened_for": json.loads(claim["reopen_slots_json"] or "[]"), "round": claim["round"] + 1},
            )

    def classify(self, token: str, text: str) -> dict:
        """Suggest a loss type from the claimant's own words; saves nothing.

        Never raises for a model problem: anything unusable is no suggestion."""
        self.check_link(token)
        with self.db.tx() as conn:
            claim = conn.execute("SELECT id FROM claims WHERE token=%s", (token,)).fetchone()
        claim_id = claim["id"]
        types = loss_classify.visible_types()
        try:
            call = self.meta.read_json(
                purpose="classify", model=loss_classify.MODEL,
                prompt=loss_classify.prompt_for(types, text), claim_id=claim_id,
                timeout_s=loss_classify.TIMEOUT_S, max_attempts=1,
            )
        except Exception:
            logger.exception("classify failed for claim %s", claim_id)
            return loss_classify.no_suggestion()
        if not call.get("ok"):
            return loss_classify.no_suggestion()
        return loss_classify.shape(call.get("json"), types) or loss_classify.no_suggestion()

    def save_draft(self, token: str, answers: dict) -> dict:
        """Answers save as the claimant goes; only photo-hint trails are logged before submit."""
        with self.db.claim_tx(token=token) as (conn, claim):
            self._check_open(claim)
            snapshot = conn.execute(
                "SELECT * FROM policy_snapshots WHERE id=%s", (claim["policy_snapshot_id"],)
            ).fetchone()
            clean = loss_notice.effective(loss_notice.clean_answers(answers), self._policy_view(snapshot))
            self._log_hint_edits(conn, claim, snapshot, clean)
            conn.execute("UPDATE claims SET draft_json=%s WHERE id=%s", (policies.canonical(clean), claim["id"]))
        return self.session_state(token)

    def claimant_item_file(self, token: str, item_id: str) -> ItemFile:
        """A claimant may view their own shots, found through their own link only."""
        with self.db.tx() as conn:
            claim = conn.execute("SELECT * FROM claims WHERE token=%s", (token,)).fetchone()
            if not claim:
                raise CaptureError("This link is not valid", 404)
            self._check_open(claim, allow_submitted=True)
            row = conn.execute(
                "SELECT path, mime, removed_at FROM items WHERE id=%s AND claim_id=%s", (item_id, claim["id"])
            ).fetchone()
            if not row:
                raise CaptureError("Item not found", 404)
            if row.get("removed_at"):
                raise CaptureError(f"Removed on {row['removed_at']}", 410)
            key, mime = row["path"], row["mime"]
        return self._file_target(key, mime)

    def _file_target(self, key: str, mime: str) -> ItemFile:
        """Auth and link checks happen before this; a missing blob is still a 404."""
        if not self.blobs.exists(key):
            raise CaptureError("Item not found", 404)
        url = self.blobs.url(key)
        if url is not None:
            return ItemFile(mime=mime, url=url)
        path = self.blobs.path(key)  # type: ignore[attr-defined]
        return ItemFile(mime=mime, path=path)

    def check_link(self, token: str) -> None:
        """Refuse an unknown, expired or submitted link before any upload is read."""
        with self.db.tx() as conn:
            claim = conn.execute("SELECT * FROM claims WHERE token=%s", (token,)).fetchone()
            if not claim:
                raise CaptureError("This link is not valid", 404)
            self._check_open(claim)

    def start_session(self, token: str) -> dict:
        self._reopen_if_sent_back(token)
        with self.db.claim_tx(token=token) as (conn, claim):
            self._check_open(claim)
            now = self.clock.now()
            if claim["status"] == "opened":
                lifecycle.transition(conn, claim["id"], "capturing", "customer", now.isoformat())
            slots = json.loads(claim["reopen_slots_json"]) if claim["reopen_slots_json"] else None
            session = capture.start_session(conn, claim["id"], now, slots)
            lifecycle.append_event(
                conn, claim["id"], now.isoformat(), "customer", "capture.session_started",
                data={"code": session["code"]},
            )
            return session

    def issue_nonce(self, token: str, session_id: str, slot_id: str) -> dict:
        with self.db.claim_tx(token=token) as (conn, claim):
            self._check_open(claim)
            self._step(claim, slot_id)
            capture.require_slot(conn, claim["id"], session_id, slot_id)
            return capture.issue_nonce(conn, claim["id"], session_id, slot_id, self.clock.now())

    def add_item(
        self,
        token: str,
        step_id: str,
        data: bytes,
        mime: str,
        client_meta: dict,
        session_id: str = "",
        nonce: str = "",
        frame_before: bytes | None = None,
        frame_after: bytes | None = None,
    ) -> dict:
        # (b) Read-only pre-check in a short tx: no claim lock and no pool
        # connection is held while the bytes are uploaded in (c).
        now_pre = self.clock.now()
        with self.db.tx() as conn:
            claim = conn.execute("SELECT * FROM claims WHERE token=%s", (token,)).fetchone()
            if not claim:
                raise CaptureError("This link is not valid", 404)
            self._check_open(claim)
            step = self._step(claim, step_id)
            kind = step["kind"]
            mime = mime.split(";")[0].strip().lower()
            if mime not in ALLOWED_MIME[kind]:
                raise CaptureError(f"A {kind} is required for this step (got {mime})")
            if not data:
                raise CaptureError("Empty file")
            if len(data) > MAX_BYTES[kind]:
                raise CaptureError(f"File too large for a {kind}", 413)
            _validate_upload(data, mime, kind)
            claim_id = claim["id"]
            ext = ALLOWED_MIME[kind][mime]
            sha256 = hashlib.sha256(data).hexdigest()
            srow = conn.execute(
                "SELECT expires_at, superseded FROM capture_sessions WHERE id=%s AND claim_id=%s",
                (session_id, claim_id),
            ).fetchone()
            if not srow or srow["superseded"]:
                raise CaptureError("This capture session is not active. Please reopen the link.", 403)
            if datetime.fromisoformat(srow["expires_at"]) < now_pre:
                raise CaptureError("This capture session has expired. Please reopen the link.", 403)
            capture.require_slot(conn, claim_id, session_id, step_id)
            nrow = conn.execute(
                "SELECT * FROM slot_nonces WHERE nonce=%s AND claim_id=%s AND session_id=%s",
                (nonce, claim_id, session_id),
            ).fetchone()
            if not nrow or nrow["slot_id"] != step_id:
                raise CaptureError("This shot was not taken in the capture screen. Please retake it.", 403)
            if nrow["used_at"]:
                same = nrow["item_id"] and conn.execute(
                    "SELECT 1 FROM items WHERE id=%s AND sha256=%s", (nrow["item_id"], sha256)
                ).fetchone()
                if same:
                    irow = conn.execute("SELECT * FROM items WHERE id=%s", (nrow["item_id"],)).fetchone()
                    return self._item_public(irow)
                raise CaptureError("This shot was already uploaded. Please retake it.", 403)
            if datetime.fromisoformat(nrow["expires_at"]) < now_pre:
                raise CaptureError("This shot was not taken in the capture screen. Please retake it.", 403)
            nonce_issued_at = nrow["issued_at"]
        # Liveness is assessed outside any transaction: flag only, never a block.
        liveness = _liveness_assessment(kind, data, frame_before, frame_after)
        item_id = uuid.uuid4().hex
        key = f"claims/{claim_id}/{step_id}-{item_id}.{ext}"
        # (c) The file lands in the blob store before its row, holding neither
        # the claim lock nor a pool connection.
        self.blobs.put(key, data, mime)
        # (d) Authoritative write under the claim lock; consume_nonce decides.
        try:
            with self.db.claim_tx(token=token) as (conn, claim):
                self._check_open(claim)
                step = self._step(claim, step_id)
                kind = step["kind"]
                now = self.clock.now()
                capture.require_slot(conn, claim["id"], session_id, step_id)
                earlier = capture.consume_nonce(conn, claim["id"], session_id, step_id, nonce, sha256, now)
                if earlier:
                    row = conn.execute("SELECT * FROM items WHERE id=%s", (earlier,)).fetchone()
                    try:
                        self.blobs.delete(key)
                    except Exception:
                        pass  # the new blob is unneeded; keep the earlier item
                    return self._item_public(row)
                active = [
                    r["id"]
                    for r in conn.execute(
                        "SELECT id FROM items WHERE claim_id=%s AND step_id=%s AND superseded=0",
                        (claim["id"], step_id),
                    )
                ]
                if step.get("multiple") and len(active) >= step.get("max_items", 1):
                    raise CaptureError(f"At most {step['max_items']} photos for this step")
                if not step.get("multiple") and active:
                    # A retake replaces the earlier shot; the old file is kept, only marked.
                    conn.execute(
                        "UPDATE items SET superseded=1 WHERE claim_id=%s AND step_id=%s",
                        (claim["id"], step_id),
                    )
                # Server-side facts (fix 8): what the server itself recorded.
                # The client capture time stays labelled client-reported and
                # untrusted; photos carry no camera EXIF by design (canvas).
                try:
                    latency_s = (now - datetime.fromisoformat(nonce_issued_at)).total_seconds()
                except (TypeError, ValueError):
                    latency_s = None
                slow_upload = latency_s is not None and latency_s > SLOW_UPLOAD_SECONDS
                flags: list[str] = []
                if liveness is not None and liveness.get("status") == "flagged":
                    flags.append("static_source")
                if slow_upload:
                    flags.append("slow_upload")
                client_captured_at = None
                if isinstance(client_meta, dict):
                    raw_captured = client_meta.get("captured_at")
                    client_captured_at = raw_captured if isinstance(raw_captured, str) else None
                server_facts = {
                    "received_at": now.isoformat(),
                    "client_captured_at": client_captured_at,
                    "client_captured_at_source": (
                        "client-reported, untrusted" if client_captured_at else None),
                    "nonce_issued_at": nonce_issued_at,
                    "nonce_to_upload_latency_seconds": (
                        round(latency_s, 3) if latency_s is not None else None),
                    "slow_upload": bool(slow_upload),
                    "liveness": liveness,
                    "static_source": (
                        "flagged" if liveness is not None and liveness.get("status") == "flagged"
                        else ("ok" if liveness is not None else None)),
                    "flags": flags,
                }
                conn.execute(
                    "INSERT INTO items (id, claim_id, step_id, kind, mime, path, size, sha256,"
                    " received_at, client_meta_json, server_facts_json)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        item_id,
                        claim["id"],
                        step_id,
                        kind,
                        mime,
                        key,
                        len(data),
                        sha256,
                        now.isoformat(),
                        json.dumps(client_meta),  # size capped at the route
                        json.dumps(server_facts),
                    ),
                )
                if step_id in photo_check.CHECKED_SLOTS:
                    conn.execute(
                        "INSERT INTO photo_checks (item_id, claim_id, slot_id, status, version, started_at)"
                        " VALUES (%s, %s, %s, 'pending', %s, %s)",
                        (
                            item_id, claim["id"], step_id,
                            f"{photo_check.VERSION}+{photo_prompts.VERSION}", now.isoformat(),
                        ),
                    )
                capture.link_item(conn, nonce, item_id)
                lifecycle.append_event(
                    conn, claim["id"], now.isoformat(), "customer", "item.received",
                    data={"item_id": item_id, "slot": step_id, "sha256": sha256, "size": len(data),
                          "flags": flags,
                          "nonce_to_upload_latency_seconds": server_facts["nonce_to_upload_latency_seconds"]},
                )
                if not step.get("multiple") and active:
                    lifecycle.append_event(
                        conn, claim["id"], now.isoformat(), "customer", "item.retaken",
                        data={"slot": step_id, "replaced": active, "by": item_id},
                    )
                row = conn.execute("SELECT * FROM items WHERE id=%s", (item_id,)).fetchone()
                return self._item_public(row)
        except Exception:
            try:
                self.blobs.delete(key)
            except Exception:
                pass  # cleanup must not hide the original failure
            raise

    def remove_item(self, token: str, item_id: str, session_id: str = "") -> None:
        """Claimant discards a shot from a multi-photo slot. The file is kept, only marked."""
        with self.db.claim_tx(token=token) as (conn, claim):
            self._check_open(claim)
            now = self.clock.now()
            capture.require_session(conn, claim["id"], session_id, now)
            item = conn.execute(
                "SELECT step_id FROM items WHERE id=%s AND claim_id=%s AND superseded=0", (item_id, claim["id"])
            ).fetchone()
            if not item:
                raise CaptureError("Item not found", 404)
            if not self._step(claim, item["step_id"]).get("multiple"):
                raise CaptureError("Retake this shot instead of removing it")
            capture.require_slot(conn, claim["id"], session_id, item["step_id"])
            conn.execute("UPDATE items SET superseded=1 WHERE id=%s", (item_id,))
            lifecycle.append_event(
                conn, claim["id"], now.isoformat(), "customer", "item.removed",
                data={"item_id": item_id, "slot": item["step_id"]},
            )

