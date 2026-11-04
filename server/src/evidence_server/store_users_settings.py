"""Store domain mixin: users settings (split from store.py, behaviour unchanged)."""

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


class UsersSettingsMixin:
    def _seed_supervisor(self, conn, admin_email: str | None, admin_password: str | None) -> None:
        """The first supervisor: today's admin email + password, name "Administrator".

        Only when the org has no user and a password is configured; afterwards
        startup changes nothing (changing ADMIN_PASSWORD later changes nobody's
        password). The seeded email must be an email address; there is no
        "admin" default.
        """
        if not admin_password:
            return
        if conn.execute("SELECT 1 FROM users WHERE org_id=%s LIMIT 1", (self.org_id,)).fetchone():
            return
        if not admin_email:
            return
        email = users.check_email(admin_email)
        conn.execute(
            "INSERT INTO users (id, org_id, email, name, role, password_hash,"
            " must_change_password, active, session_version, created_at, created_by)"
            " VALUES (%s, %s, %s, 'Administrator', 'supervisor', %s, FALSE, TRUE, 1, %s, NULL)"
            " ON CONFLICT (org_id, email) DO NOTHING",
            (uuid.uuid4().hex, self.org_id, email,
             users.hash_password(admin_password), self.clock.now().isoformat()),
        )

    def delivery_mode(self) -> str:
        """How this org sends results: `on_ready` (today) or `on_decision` (after the review)."""
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT delivery_mode FROM org_settings WHERE org_id=%s", (self.org_id,)
            ).fetchone()
        if not row or row["delivery_mode"] not in ("on_ready", "on_decision"):
            return "on_ready"
        return row["delivery_mode"]

    def retention_days(self) -> int:
        """How long media is kept after the decision (T50); 90 until a supervisor changes it."""
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT retention_days FROM org_settings WHERE org_id=%s", (self.org_id,)
            ).fetchone()
        if not row or not isinstance(row.get("retention_days"), int):
            return RETENTION_DEFAULT_DAYS
        return row["retention_days"]

    def settings(self) -> dict:
        """GET /api/admin/settings. Always carries brand_color: the org's color,
        else DEFAULT_BRAND_COLOR (the claimant falls back to it meanwhile).
        updated_by/at name who last changed the settings (NULL until the first change)."""
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT delivery_mode, updated_by, updated_at, retention_days"
                " FROM org_settings WHERE org_id=%s",
                (self.org_id,),
            ).fetchone()
            org = conn.execute("SELECT brand_color FROM orgs WHERE id=%s", (self.org_id,)).fetchone()
        days = row.get("retention_days") if row else None
        return {
            "delivery_mode": row["delivery_mode"] if row else "on_ready",
            "brand_color": (org["brand_color"] if org else None) or DEFAULT_BRAND_COLOR,
            "retention_days": days if isinstance(days, int) else RETENTION_DEFAULT_DAYS,
            "updated_by": row["updated_by"] if row else None,
            "updated_at": row["updated_at"] if row else None,
        }

    def update_settings(self, fields: dict, user: dict) -> dict:
        """PUT /api/admin/settings (supervisors only): delivery_mode, brand_color,
        retention_days, or any combination. Every field sent is validated before
        anything is written, and the change records who made it and when."""
        fields = fields if isinstance(fields, dict) else {}
        mode, color, days = fields.get("delivery_mode"), fields.get("brand_color"), fields.get("retention_days")
        if "delivery_mode" in fields and mode not in ("on_ready", "on_decision"):
            raise CaptureError("delivery_mode must be on_ready or on_decision", 400)
        if "brand_color" in fields and (
            not isinstance(color, str) or not BRAND_COLOR_RE.fullmatch(color)
        ):
            raise CaptureError("brand_color must be a #RRGGBB hex color like #1673e6", 400)
        if "retention_days" in fields and (
            not isinstance(days, int) or isinstance(days, bool)
            or not RETENTION_MIN_DAYS <= days <= RETENTION_MAX_DAYS
        ):
            raise CaptureError(
                f"retention_days must be an integer between {RETENTION_MIN_DAYS} and {RETENTION_MAX_DAYS}", 400)
        now = self.clock.now().isoformat()
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT delivery_mode, retention_days FROM org_settings WHERE org_id=%s", (self.org_id,)
            ).fetchone()
            next_mode = mode if "delivery_mode" in fields else (row["delivery_mode"] if row else "on_ready")
            next_days = (days if "retention_days" in fields
                         else (row["retention_days"] if row and isinstance(row.get("retention_days"), int)
                               else RETENTION_DEFAULT_DAYS))
            conn.execute(
                "INSERT INTO org_settings (org_id, delivery_mode, retention_days, updated_by, updated_at)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (org_id) DO UPDATE SET delivery_mode=EXCLUDED.delivery_mode,"
                " retention_days=EXCLUDED.retention_days,"
                " updated_by=EXCLUDED.updated_by, updated_at=EXCLUDED.updated_at",
                (self.org_id, next_mode, next_days, user["name"], now),
            )
            if "brand_color" in fields:
                conn.execute("UPDATE orgs SET brand_color=%s WHERE id=%s", (color, self.org_id))
        return self.settings()

    @staticmethod
    def _user_public(row: dict) -> dict:
        """The user shape the console shows: never the hash, never the one-time password."""
        return {
            "id": row["id"],
            "email": row["email"],
            "name": row["name"],
            "role": row["role"],
            "active": bool(row["active"]),
            "must_change_password": bool(row["must_change_password"]),
            "created_at": row["created_at"],
        }

    def list_users(self) -> list[dict]:
        """Every user of the org, sorted by name."""
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT * FROM users WHERE org_id=%s ORDER BY name", (self.org_id,)
            ).fetchall()
        return [self._user_public(r) for r in rows]

    def create_user(self, email: str, name: str, role: str, created_by: str | None) -> dict:
        """Add an adjuster or supervisor: they sign in with a one-time password
        and must choose their own at first login. Answers 201 with the user and
        the one-time password (shown only in this reply)."""
        try:
            clean_email = users.check_email(email)
        except ValueError as e:
            raise CaptureError(str(e), 400) from e
        try:
            clean_name = users.check_name(name)
        except ValueError as e:
            raise CaptureError(str(e), 400) from e
        if role not in ("adjuster", "supervisor"):
            raise CaptureError("role must be adjuster or supervisor", 400)
        with self.db.tx() as conn:
            if conn.execute(
                "SELECT 1 FROM users WHERE org_id=%s AND email=%s", (self.org_id, clean_email)
            ).fetchone():
                raise CaptureError("A user with this email already exists", 409)
            one_time = users.one_time_password()
            row = {
                "id": uuid.uuid4().hex,
                "email": clean_email,
                "name": clean_name,
                "role": role,
                "password_hash": users.hash_password(one_time),
                "created_at": self.clock.now().isoformat(),
                "created_by": created_by,
            }
            conn.execute(
                "INSERT INTO users (id, org_id, email, name, role, password_hash,"
                " must_change_password, active, session_version, created_at, created_by)"
                " VALUES (%s, %s, %s, %s, %s, %s, TRUE, TRUE, 1, %s, %s)",
                (row["id"], self.org_id, row["email"], row["name"], row["role"],
                 row["password_hash"], row["created_at"], row["created_by"]),
            )
        return {**self._user_public({**row, "active": True, "must_change_password": True}),
                "one_time_password": one_time}

    def update_user(self, user_id: str, fields: dict, me: dict) -> dict:
        """Change a user's name, role, or status. Deactivating, or changing the
        role, bumps session_version (signs that user out at once). A supervisor
        cannot change their own role or deactivate themselves."""
        fields = fields if isinstance(fields, dict) else {}
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id=%s AND org_id=%s", (user_id, self.org_id)
            ).fetchone()
            if row is None:
                raise CaptureError("User not found", 404)
            name, role, active = row["name"], row["role"], bool(row["active"])
            if "name" in fields:
                try:
                    name = users.check_name(fields["name"])
                except ValueError as e:
                    raise CaptureError(str(e), 400) from e
            if "role" in fields:
                if fields["role"] not in ("adjuster", "supervisor"):
                    raise CaptureError("role must be adjuster or supervisor", 400)
                role = fields["role"]
            if "active" in fields:
                active = bool(fields["active"])
            if user_id == me["id"] and (role != "supervisor" or not active):
                raise CaptureError("You cannot remove your own supervisor access", 409)
            bump = ", session_version=session_version+1" if (
                role != row["role"] or active != bool(row["active"])) else ""
            conn.execute(
                f"UPDATE users SET name=%s, role=%s, active=%s{bump} WHERE id=%s",
                (name, role, active, user_id),
            )
            return self._user_public(conn.execute(
                "SELECT * FROM users WHERE id=%s", (user_id,)
            ).fetchone())

    def reset_user_password(self, user_id: str) -> dict:
        """Give a user a new one-time password: they must choose their own at
        the next sign-in, and their sessions stop at once."""
        with self.db.tx() as conn:
            if not conn.execute(
                "SELECT 1 FROM users WHERE id=%s AND org_id=%s", (user_id, self.org_id)
            ).fetchone():
                raise CaptureError("User not found", 404)
            one_time = users.one_time_password()
            conn.execute(
                "UPDATE users SET password_hash=%s, must_change_password=TRUE,"
                " session_version=session_version+1 WHERE id=%s",
                (users.hash_password(one_time), user_id),
            )
        return {"one_time_password": one_time}

