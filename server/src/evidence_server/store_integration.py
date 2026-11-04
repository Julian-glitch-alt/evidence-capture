"""Store domain mixin: integration (split from store.py, behaviour unchanged)."""

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


class IntegrationMixin:
    def list_api_keys(self) -> list[dict]:
        """GET /api/admin/api-keys: newest first, never the hash."""
        now_iso = self.clock.now().isoformat()
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM api_keys WHERE org_id=%s ORDER BY seq DESC",
                (self.org_id,),
            ).fetchall()
        return [integration.public_key(r, now_iso) for r in rows]

    def create_api_key(self, name: str, mode: str, user: dict) -> dict:
        """POST /api/admin/api-keys: the row plus the full key, shown once."""
        if not isinstance(name, str) or not 1 <= len(name) <= 60:
            raise CaptureError("name must be 1-60 characters", 400)
        if mode not in ("live", "test"):
            raise CaptureError("mode must be live or test", 400)
        now_iso = self.clock.now().isoformat()
        with self.db.tx() as conn:
            row, key = integration.mint_key(
                conn, self.org_id, name, mode, user["name"], now_iso)
        return {**integration.public_key(row, now_iso), "key": key}

    def revoke_api_key(self, key_id: str) -> dict:
        """POST /api/admin/api-keys/{id}/revoke: revoked now. 404 unknown."""
        now_iso = self.clock.now().isoformat()
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE id=%s AND org_id=%s", (key_id, self.org_id)
            ).fetchone()
            if row is None:
                raise CaptureError("API key not found", 404)
            if row["revoked_at"] is not None:
                raise CaptureError("This key is already revoked", 409)
            conn.execute(
                "UPDATE api_keys SET revoked_at=%s WHERE id=%s", (now_iso, key_id))
            row = conn.execute(
                "SELECT * FROM api_keys WHERE id=%s", (key_id,)).fetchone()
        return integration.public_key(row, now_iso)

    def rotate_api_key(self, key_id: str, expires_in: str, user: dict) -> dict:
        """POST /api/admin/api-keys/{id}/rotate: a new key of the same name
        and mode, the old one expiring now, in 1 hour, or in 24 hours."""
        if expires_in not in ("now", "1h", "24h"):
            raise CaptureError("old_key_expires_in must be now, 1h or 24h", 400)
        now = self.clock.now()
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM api_keys WHERE id=%s AND org_id=%s", (key_id, self.org_id)
            ).fetchone()
            if row is None:
                raise CaptureError("API key not found", 404)
            status = integration.key_status(row, now.isoformat())
            if status == "revoked":
                raise CaptureError("This key is already revoked", 409)
            if status == "expired":
                raise CaptureError("This key has already expired", 409)
            delta = {"now": timedelta(0), "1h": timedelta(hours=1), "24h": timedelta(hours=24)}[expires_in]
            conn.execute(
                "UPDATE api_keys SET expires_at=%s WHERE id=%s",
                ((now + delta).isoformat(), key_id))
            new_row, key = integration.mint_key(
                conn, self.org_id, row["name"], row["mode"], user["name"], now.isoformat())
        return {**integration.public_key(new_row, now.isoformat()), "key": key}

    def get_webhook_secret(self) -> dict:
        """GET /api/admin/webhook-secret: the last 4 and when it was rotated."""
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT webhook_secret, rotated_at FROM integration_credentials WHERE org_id=%s",
                (self.org_id,),
            ).fetchone()
        if row is None:
            raise CaptureError("No webhook secret", 404)
        return {"last4": row["webhook_secret"][-4:], "rotated_at": row["rotated_at"]}

    def rotate_webhook_secret(self) -> dict:
        """POST /api/admin/webhook-secret/rotate: a new secret, shown once.

        The mock claims system's copy is updated in the same transaction (it
        stands for the insurer's IT); the outbox keeps retrying as today while
        an insurer switches over.
        """
        secret = secrets.token_urlsafe(32)
        now_iso = self.clock.now().isoformat()
        with self.db.tx() as conn:
            updated = conn.execute(
                "UPDATE integration_credentials SET webhook_secret=%s, rotated_at=%s"
                " WHERE org_id=%s",
                (secret, now_iso, self.org_id),
            ).rowcount
            if not updated:
                raise CaptureError("No webhook secret", 404)
            conn.execute(
                "UPDATE mock_config SET webhook_secret=%s WHERE id=1", (secret,))
        return {"secret": secret}

    def deliver(self, claim_id: str, force: bool = False) -> dict:
        """POST the claim's undelivered outbox messages to its callback URL, oldest
        first. Never raises; returns {"delivered": n, "failed": n}.

        Order is kept per claim: a message waiting for its retry (or given up) holds
        back the later ones. force=True is the adjuster's manual Resend: it ignores
        the backoff, the lease and the dead letter.
        """
        counts = {"delivered": 0, "failed": 0}
        try:
            with self.db.tx() as conn:
                claim = conn.execute(
                    "SELECT callback_url FROM claims WHERE id=%s", (claim_id,)
                ).fetchone()
                if not claim:
                    return counts
                rows = conn.execute(
                    "SELECT id FROM outbox WHERE claim_id=%s AND delivered_at IS NULL"
                    " ORDER BY created_at, id",
                    (claim_id,),
                ).fetchall()
                secret_row = conn.execute(
                    "SELECT webhook_secret FROM integration_credentials WHERE org_id=%s",
                    (self.org_id,),
                ).fetchone()
            if not rows or not claim["callback_url"] or not secret_row:
                return counts
            secret = secret_row["webhook_secret"].encode()
            for row in rows:
                outcome = self._deliver_one(row["id"], claim["callback_url"], secret, force)
                if outcome != "delivered":
                    if outcome == "failed":
                        counts["failed"] += 1
                    break  # keep the order: later messages wait for this one
                counts["delivered"] += 1
        except Exception:
            logger.exception("deliver failed for claim %s", claim_id)
        return counts

    def _deliver_one(self, outbox_id: str, url: str, secret: bytes, force: bool) -> str:
        """-> "delivered" | "failed" | "skipped" (not due, leased by another worker, or dead)."""
        now = self.clock.now()
        with self.db.tx() as conn:
            row = conn.execute(
                "UPDATE outbox SET next_attempt_at=%s WHERE id=%s AND delivered_at IS NULL"
                " AND (%s OR (dead_at IS NULL AND (next_attempt_at IS NULL"
                " OR next_attempt_at::timestamptz <= %s::timestamptz)))"
                " RETURNING event, body_json",
                ((now + OUTBOX_LEASE).isoformat(), outbox_id, force, now.isoformat()),
            ).fetchone()
        if row is None:
            return "skipped"
        body = row["body_json"].encode()
        headers = {
            "Content-Type": "application/json",
            "X-Evidence-Event": row["event"],
            "X-Evidence-Delivery": outbox_id,
            "X-Evidence-Signature": report.sign(body, secret, int(now.timestamp())),
        }
        try:
            status, _ = self.http("POST", url, body, headers)
        except Exception as e:
            self._delivery_failed(outbox_id, f"{type(e).__name__}: {e}")
            return "failed"
        if not 200 <= status < 300:
            self._delivery_failed(outbox_id, f"HTTP {status}")
            return "failed"
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE outbox SET delivered_at=%s, next_attempt_at=NULL, dead_at=NULL WHERE id=%s",
                (self.clock.now().isoformat(), outbox_id),
            )
        return "delivered"

    def _delivery_failed(self, outbox_id: str, error: str) -> None:
        """Record one undelivered attempt and schedule the next one; 2xx never lands here.

        Retry n waits OUTBOX_BACKOFF_BASE * 2**(n-1), capped at OUTBOX_BACKOFF_CAP; after
        OUTBOX_MAX_ATTEMPTS the message is dead until an adjuster presses Resend.
        """
        now = self.clock.now()
        with self.db.tx() as conn:
            row = conn.execute(
                "UPDATE outbox SET attempts = attempts + 1, last_error = %s WHERE id = %s RETURNING attempts",
                (error[:300], outbox_id),
            ).fetchone()
            if row is None:
                return
            if row["attempts"] >= OUTBOX_MAX_ATTEMPTS:
                conn.execute(
                    "UPDATE outbox SET dead_at=%s, next_attempt_at=NULL WHERE id=%s",
                    (now.isoformat(), outbox_id),
                )
            else:
                conn.execute(
                    "UPDATE outbox SET next_attempt_at=%s WHERE id=%s",
                    ((now + outbox_backoff(row["attempts"])).isoformat(), outbox_id),
                )

