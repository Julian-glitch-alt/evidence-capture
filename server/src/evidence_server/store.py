"""Claims, capture links and evidence storage (Postgres + blob store).

Trust boundary: anything the phone sends (client_meta) is stored as a claim,
never as fact. Facts are what the server records itself: received_at, size, sha256.
"""

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

# T45: the claimant header and button color, one per insurer (orgs.brand_color,
# migration 0016). NULL until a supervisor sets it; the claimant falls back to this.
DEFAULT_BRAND_COLOR = "#1673e6"
BRAND_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{6}")

# T50: photos, video, the signature image and our report PDFs go 90 days after the decision.
RETENTION_DEFAULT_DAYS = 90
RETENTION_MIN_DAYS = 30
RETENTION_MAX_DAYS = 3650
RETENTION_SWEEP_LIMIT = 20

LINK_TTL = timedelta(days=7)
CAPTURE_STATUSES = {"opened", "capturing"}  # the claimant can still add evidence
REOPENABLE_STATUSES = CAPTURE_STATUSES | {"more_evidence"}  # waiting for the claimant (Send back included)

# Durable background work (track 3). The sweeper runs from a cron (deploy/README).
SWEEP_STALE_AFTER = timedelta(minutes=5)  # well past a model call's worst case (30 s queue + 30 s call)
ANALYSIS_MAX_ATTEMPTS = 3                 # the first run plus two sweeper re-runs
OUTBOX_MAX_ATTEMPTS = 8                   # about an hour of retries in all
OUTBOX_BACKOFF_BASE = timedelta(seconds=30)
OUTBOX_BACKOFF_CAP = timedelta(hours=1)
OUTBOX_LEASE = timedelta(seconds=60)      # longer than one POST (15 s timeout)


def _report_dir(claim_id: str, rnd: int) -> str:
    """Blob folder of one round's reports; round 1 keeps the original location."""
    return f"reports/{claim_id}" if rnd <= 1 else f"reports/{claim_id}/round-{rnd}"


def outbox_backoff(attempts: int) -> timedelta:
    """Wait before retry number `attempts` + 1: 30 s, 1 min, 2 min, 4 min, ... capped."""
    return min(OUTBOX_BACKOFF_BASE * (2 ** max(attempts - 1, 0)), OUTBOX_BACKOFF_CAP)

# The accuracy measured by the E1 set (scripts/e1_summary.py), shown on System health (T57).
ACCURACY_FILE = Path(__file__).parent / "reference" / "model_accuracy.json"
logger = logging.getLogger(__name__)

ALLOWED_MIME = {
    "photo": {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"},
    "video": {"video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov"},
    "signature": {"image/png": "png"},
}
MAX_BYTES = {"photo": 15 * 1024 * 1024, "video": 80 * 1024 * 1024, "signature": 1024 * 1024}


@dataclass
class ItemFile:
    """Where a stored file lives: a presigned URL (S3/R2) or a local path."""

    mime: str
    url: str | None = None
    path: Path | None = None


def _new_claim_id(local_day: datetime) -> str:
    return f"CLM-{local_day:%Y%m%d}-{secrets.token_hex(3).upper()}"


@dataclass
class Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


def _open_photo(data) -> "Image.Image | None":
    """Photo bytes -> an RGB Pillow image in its stored pixel orientation (as Muse Spark saw it), or None."""
    if not isinstance(data, bytes):
        return None
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
        return image.convert("RGB")
    except Exception:
        return None


class Store:
    def __init__(
        self,
        data_dir: Path,
        db: Database,
        blobs: BlobStore,
        clock: Clock | None = None,
        sealer: "seal.Sealer | None" = None,
        meta: "meta_client.MetaClient | None" = None,
        check_license: bool = False,
        sam_max_calls: int = meta_client.DEFAULT_SAM_MAX_CALLS,
        lane_rule: str = proposal.DEFAULT_LANE_RULE,
        admin_email: str | None = None,
        admin_password: str | None = None,
    ):
        self.data_dir = Path(data_dir)
        self.db = db
        self.blobs = blobs
        self.clock = clock or Clock()
        self.sealer = sealer or seal.Sealer.from_env_or_dev_file(self.data_dir)
        self.meta = meta or meta_client.MetaClient(db, None)
        self.check_license = check_license
        self.sam_max_calls = sam_max_calls  # track 6: SAM outline calls per claim; 0 skips the step
        self.lane_rule = lane_rule  # which lane rule drives the proposal (track 5): "cost" or "count"
        self.http: integration.Http = integration.httpx_transport()
        self.limiter = None  # ratelimit.RateLimiter, set by create_app; the sweeper prunes it
        self.db.migrate()
        with self.db.tx() as conn:
            self.org_id = policies.seed(conn)
            integration.ensure_credentials(conn, self.org_id, self.clock.now())
            self._seed_supervisor(conn, admin_email, admin_password)

    def _seed_supervisor(self, conn, admin_email: str | None, admin_password: str | None) -> None:
        """The first supervisor: today's admin email + password, name "Administrator".

        Only when the org has no user and a password is configured; afterwards
        startup changes nothing (changing ADMIN_PASSWORD later changes nobody's
        password). The seeded email skips the email format check (it may be `admin`).
        """
        if not admin_password:
            return
        if conn.execute("SELECT 1 FROM users WHERE org_id=%s LIMIT 1", (self.org_id,)).fetchone():
            return
        email = (admin_email or "admin").strip().lower()
        conn.execute(
            "INSERT INTO users (id, org_id, email, name, role, password_hash,"
            " must_change_password, active, session_version, created_at, created_by)"
            " VALUES (%s, %s, %s, 'Administrator', 'supervisor', %s, FALSE, TRUE, 1, %s, NULL)"
            " ON CONFLICT (org_id, email) DO NOTHING",
            (uuid.uuid4().hex, self.org_id, email,
             users.hash_password(admin_password), self.clock.now().isoformat()),
        )

    # --- adjuster side ---

    def open_claim(
        self,
        policy_no: str | None,
        actor: str = "user",
        insurer_claim_number: str | None = None,
        callback_url: str | None = None,
        opened_data: dict | None = None,
        test_mode: bool = False,
        policy_data: dict | None = None,
    ) -> dict:
        template_id = loss_notice.TEMPLATE_ID
        policy_no = (policy_no or "").strip().upper()
        if not policy_no:
            raise CaptureError("Policy number is required")
        now = self.clock.now()
        for _ in range(5):
            token = secrets.token_urlsafe(16)  # 128 bits
            try:
                with self.db.tx() as conn:
                    snapshot = policies.take_snapshot(conn, self.org_id, policy_no, policy_data)
                    local_day = now.astimezone(self._claim_tz(conn, snapshot))
                    claim_id = _new_claim_id(local_day)
                    conn.execute(
                        "INSERT INTO claims (id, org_id, token, template_id, policy_snapshot_id, status,"
                        " created_at, expires_at, insurer_claim_number, callback_url, test_mode)"
                        " VALUES (%s, %s, %s, %s, %s, 'opened', %s, %s, %s, %s, %s)",
                        (
                            claim_id,
                            self.org_id,
                            token,
                            template_id,
                            snapshot["id"],
                            now.isoformat(),
                            (now + LINK_TTL).isoformat(),
                            insurer_claim_number,
                            callback_url,
                            bool(test_mode),
                        ),
                    )
                    lifecycle.append_event(
                        conn,
                        claim_id,
                        now.isoformat(),
                        actor,
                        "claim.opened",
                        None,
                        "opened",
                        {
                            "policy_no": policy_no,
                            "policy_verified": bool(snapshot["verified"]),
                            "insurer_claim_number": insurer_claim_number,
                            **(opened_data or {}),
                            "policy_source": policies.snapshot_public(snapshot)["source"],
                        },
                    )
            except psycopg.errors.UniqueViolation:
                continue
            return {"id": claim_id, "token": token}
        raise CaptureError("Could not create a unique claim id", 500)

    def transition(self, claim_id: str, to_status: str, actor: str, data: dict | None = None) -> dict:
        with self.db.claim_tx(claim_id=claim_id) as (conn, _claim):
            return lifecycle.transition(
                conn, claim_id, to_status, actor, self.clock.now().isoformat(), data
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

    # --- insurer integration kit (supervisors manage; T48) ---

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

    # --- users (supervisors manage; T47) ---

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

    def _review_state(self, conn: psycopg.Connection, claim_id: str) -> dict:
        """The adjuster review state of the claim's current round: empty before any review."""
        rows = conn.execute(
            "SELECT r.* FROM reviews r JOIN claims c ON c.id = r.claim_id AND r.round = c.round"
            " WHERE r.claim_id=%s ORDER BY r.at", (claim_id,)
        ).fetchall()
        return review.state(rows)

    @staticmethod
    def _keyed_flags(entries: list[dict] | None, answers: dict | None = None) -> list[dict]:
        """Every proposal flag and note carries its key (f"{source}:{reason_code}"),
        its review step and its answer fields (T52, at read time, never stored)."""
        return [
            {**e, "key": f"{e.get('source')}:{e.get('reason_code')}",
             "step": review_steps.step_of(e), "fields": review_steps.fields_of(e, answers)}
            for e in entries or []
        ]

    @staticmethod
    def _keyed_damages(damages: list[dict] | None) -> list[dict]:
        """Every damage.reading entry carries its key: f"{item_id}:{number}"."""
        counts: dict = {}
        out = []
        for d in damages or []:
            counts[d.get("item_id")] = counts.get(d.get("item_id"), 0) + 1
            out.append({**d, "number": counts[d.get("item_id")],
                        "key": f"{d.get('item_id')}:{counts[d.get('item_id')]}"})
        return out

    @staticmethod
    def _with_outlines(conn: psycopg.Connection, claim_id: str, damages: list[dict]) -> list[dict]:
        """Keyed damages with their SAM outline (track 6) as "outline"; a damage without a row gets
        None (the reading predates the outline step, or the step failed): "outline not available"."""
        rows = conn.execute(
            "SELECT item_id, number, status, result_json, error FROM damage_outlines WHERE claim_id=%s",
            (claim_id,),
        ).fetchall()
        found = {f"{r['item_id']}:{r['number']}": {**json.loads(r["result_json"]), "status": r["status"],
                                                   "error": r["error"]} for r in rows}
        return [{**d, "outline": found.get(d["key"])} for d in damages]

    @staticmethod
    def _without_excluded(reading: dict, dropped: set[str]) -> dict:
        """A copy of the damage reading without the excluded damages. Excluding one photo's
        listing excludes the whole physical damage (damage.merge), or the other photo
        of the same dent would keep it priced."""
        if not dropped:
            return reading
        dropped = set(dropped)
        for group in damage.merge(reading.get("damages") or []):
            if dropped & set(group["keys"]):
                dropped |= set(group["keys"])
        counts: dict = {}
        kept = []
        for d in reading.get("damages") or []:
            counts[d.get("item_id")] = counts.get(d.get("item_id"), 0) + 1
            if f"{d.get('item_id')}:{counts[d.get('item_id')]}" not in dropped:
                kept.append(d)
        return {**reading, "damages": kept}

    def add_review(self, claim_id: str, body: dict, user: dict) -> dict:
        """Record one adjuster review row and return the claim detail. The name
        always comes from the session: a `by` sent in the body is ignored."""
        detail = self.claim_detail(claim_id)
        proposal = detail.get("proposal")
        if not proposal or proposal.get("status") != "ready":
            raise CaptureError("The proposal is not ready yet", 409)
        if detail.get("decision") is not None:
            raise CaptureError("This claim already has a decision", 409)
        flag_keys = {f.get("key") for f in proposal.get("flags") or []}
        damage_keys = {d.get("key") for d in (detail.get("damage") or {}).get("damages") or []}
        try:
            clean = review.validate({**(body or {}), "by": user["name"]}, flag_keys, damage_keys)
        except ValueError as e:
            raise CaptureError(str(e), 400) from e
        with self.db.claim_tx(claim_id=claim_id) as (conn, claim):
            if conn.execute("SELECT 1 FROM decisions WHERE claim_id=%s AND round=%s",
                            (claim_id, claim["round"])).fetchone():
                raise CaptureError("This claim already has a decision", 409)
            now = self.clock.now().isoformat()
            conn.execute(
                "INSERT INTO reviews (id, claim_id, kind, target, verdict, reason, text, decided_by, at, round, user_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (uuid.uuid4().hex, claim_id, clean["kind"], clean["target"], clean["verdict"],
                 clean["reason"], clean["text"], clean["by"], now, claim["round"], user["id"]),
            )
            lifecycle.append_event(
                conn, claim_id, now, "user", f"review.{clean['kind']}",
                data={"kind": clean["kind"], "target": clean["target"], "verdict": clean["verdict"],
                      "reason": clean["reason"], "text": clean["text"],
                      "by": clean["by"], "at": now,
                      "user_id": user["id"], "user_name": user["name"]},
            )
        return self.claim_detail(claim_id)

    def decide(self, claim_id: str, body: dict, user: dict) -> dict:
        """Record the adjuster's decision on a ready proposal and lock the claim.

        The name always comes from the session: a `by` sent in the body is ignored.
        The status steps, the decisions row, the event and the outbox row all
        land in one claim_tx transaction; delivery runs after commit.
        """
        detail = self.claim_detail(claim_id)
        proposal = detail.get("proposal")
        if not proposal or proposal.get("status") != "ready":
            raise CaptureError("The proposal is not ready yet", 409)
        photo_slots = [
            s["id"]
            for section in (detail.get("layout") or {}).get("sections", [])
            for s in section.get("slots", [])
            if s.get("kind") == "photo"
        ]
        try:
            clean = decision.validate(
                (body or {}).get("action"), {**(body or {}), "by": user["name"]}, proposal, photo_slots)
        except ValueError as e:
            raise CaptureError(str(e), 400) from e
        mode = self.delivery_mode()
        with self.db.tx() as conn:
            rep = conn.execute(
                "SELECT r.* FROM reports r JOIN claims c ON c.id = r.claim_id AND r.round = c.round"
                " WHERE r.claim_id=%s", (claim_id,)).fetchone()
            if not rep:
                raise CaptureError("The report is not ready yet", 409)
            try:
                stored_ai = json.loads(rep["ai_summary_json"]) if rep["ai_summary_json"] else None
            except ValueError:
                stored_ai = None
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
            token = rep["token"]
        stamped = self.clock.now()
        reviewed = report.build(detail, org["name"], stamped, tz)
        reviewed["test_mode"] = bool(detail.get("test_mode"))
        reviewed["review"] = detail.get("review") or {"flags": {}, "damages": {}, "notes": []}
        reviewed["decision"] = {**clean, "at": stamped.isoformat(), "by": clean["by"]}
        reviewed["ai_summary"] = stored_ai
        images: dict[str, bytes] = {}
        for photo in reviewed["photos"]:
            key = paths.get(photo["item_id"])
            if not key:
                continue
            try:
                images[photo["item_id"]] = self.blobs.get(key)
            except Exception:
                continue
        reviewed_pdf = report_pdf.render(reviewed, images, stored_ai)
        reviewed_sha = hashlib.sha256(reviewed_pdf).hexdigest()
        with self.db.claim_tx(claim_id=claim_id) as (conn, claim):
            rnd = claim["round"]
            reviewed_key = f"{_report_dir(claim_id, rnd)}/reviewed-evidence-report.pdf"
            if conn.execute("SELECT 1 FROM decisions WHERE claim_id=%s AND round=%s", (claim_id, rnd)).fetchone():
                raise CaptureError("This claim already has a decision", 409)
            if rep["round"] != rnd:
                raise CaptureError("The claim changed while deciding; reload it", 409)
            now = self.clock.now()
            at = now.isoformat()
            order = ["submitted", "verifying", "assessed", "in_review"]
            if claim["status"] in order:
                for nxt in order[order.index(claim["status"]) + 1:]:
                    lifecycle.transition(conn, claim_id, nxt, "system", at)
            out = decision.outcome(clean)
            lifecycle.transition(conn, claim_id, out["status"], "user", at, {
                "action": clean["action"], "user_id": user["id"], "user_name": user["name"]})
            conn.execute(
                "INSERT INTO decisions (claim_id, action, lane_before, lane_after, reserve_before, reserve_after,"
                " reason, slots_json, decided_by, decided_at, version, round, user_id)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    claim_id, clean["action"], clean["lane_before"], clean["lane_after"],
                    clean["reserve_before"], clean["reserve_after"], clean["reason"],
                    policies.canonical(clean["slots"]), clean["by"], at, decision.VERSION, rnd,
                    user["id"],
                ),
            )
            if clean["action"] == "send_back":
                # Reopen the claimant link for the requested photos only (track 3). The
                # link gets a fresh lifetime; the claimant opening it moves the claim
                # from more_evidence back to capturing.
                expires = max(datetime.fromisoformat(claim["expires_at"]), now + LINK_TTL)
                conn.execute(
                    "UPDATE claims SET reopen_slots_json=%s, expires_at=%s WHERE id=%s",
                    (policies.canonical(clean["slots"]), expires.isoformat(), claim_id),
                )
            lifecycle.append_event(conn, claim_id, at, "user", "decision.recorded", data={
                **clean, "user_id": user["id"], "user_name": user["name"]})
            note = decision.note(clean, proposal)
            activity = out["activity"]
            snapshot = conn.execute(
                "SELECT * FROM policy_snapshots WHERE id=%s", (claim["policy_snapshot_id"],)
            ).fetchone()
            vehicles = (((detail.get("policy") or {}).get("data") or {}).get("vehicles")) or []
            activity_entry = {
                "subject": activity["subject"],
                "description": proposal.get("next_step")
                if clean["action"] in ("approve", "edit_approve")
                else note["body"].split(".")[0],
                "priority": activity["priority"],
                "due_date": report.business_days_after(
                    now.astimezone(self._claim_tz(conn, snapshot)).date(),
                    activity["business_days"],
                ).isoformat(),
            }
            self.blobs.put(reviewed_key, reviewed_pdf, "application/pdf")
            conn.execute(
                "UPDATE reports SET reviewed_path=%s, reviewed_sha256=%s, reviewed_at=%s WHERE claim_id=%s",
                (reviewed_key, reviewed_sha, at, claim_id),
            )
            lifecycle.append_event(
                conn, claim_id, at, "system", "report.reviewed", data={"sha256": reviewed_sha},
            )
            reviewed_doc = {
                "name": "reviewed-evidence-report.pdf",
                "document_type": "evidence_report",
                "mime": "application/pdf",
                "url": f"/api/r/{token}",
                "sha256": reviewed_sha,
            }
            if mode == "on_decision":
                outbox_id = uuid.uuid4().hex
                body = report.package(
                    reviewed, outbox_id, reviewed_sha,
                    f"/api/r/{token}", f"/api/r/{token}/package")
                body["documents"][0] = reviewed_doc
                body["notes"] = [report.note(reviewed), note]
                body["activities"] = [activity_entry]
                body["decision"] = {**clean, "at": at}
                body["complete_activities"] = ["Review verified evidence"]  # the pending_review one
                if rnd > 1:
                    body["round"] = rnd
                conn.execute(
                    "INSERT INTO outbox (id, claim_id, event, body_json, created_at, attempts, round)"
                    " VALUES (%s, %s, 'evidence.ready', %s, %s, 0, %s)"
                    " ON CONFLICT (claim_id, event, round) DO UPDATE SET body_json=EXCLUDED.body_json,"
                    " created_at=EXCLUDED.created_at, delivered_at=NULL, attempts=0, last_error=NULL,"
                    " next_attempt_at=NULL, dead_at=NULL",
                    (outbox_id, claim_id, policies.canonical(body), at, rnd),
                )
            else:
                outbox_id = uuid.uuid4().hex
                conn.execute(
                    "INSERT INTO outbox (id, claim_id, event, body_json, created_at, attempts, round)"
                    " VALUES (%s, %s, 'decision.recorded', %s, %s, 0, %s)",
                    (
                        outbox_id, claim_id,
                        policies.canonical(report.decision_recorded(
                            {
                                "case_id": detail["id"],
                                "insurer_claim_number": detail["insurer_claim_number"],
                                "policy_number": (detail.get("policy") or {}).get("policy_no"),
                                "vin": vehicles[0].get("vin") if vehicles else None,
                            },
                            outbox_id, at, clean, [reviewed_doc], [note], [activity_entry],
                            complete_activities=["Review verified evidence"],
                            test_mode=bool(detail.get("test_mode")), round=rnd,
                        )),
                        at, rnd,
                    ),
                )
        self.deliver(claim_id)
        return self.claim_detail(claim_id)

    def set_diary(self, claim_id: str, body: dict, user: dict) -> dict:
        """Set a follow-up diary on a claim. The name always comes from the
        session: a `created_by` sent in the body is ignored."""
        try:
            clean = diary.validate(body if isinstance(body, dict) else {}, self.clock.now().date())
        except ValueError as e:
            raise CaptureError(str(e), 400) from e
        with self.db.claim_tx(claim_id=claim_id) as (conn, claim):
            now = self.clock.now().isoformat()
            diary_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO diaries (id, claim_id, due_on, reason, created_by, created_at, done_at, done_by)"
                " VALUES (%s, %s, %s, %s, %s, %s, NULL, NULL)",
                (diary_id, claim_id, clean["due_on"], clean["reason"], user["name"], now),
            )
            lifecycle.append_event(
                conn, claim_id, now, "user", "diary.set",
                data={"diary_id": diary_id, "due_on": clean["due_on"], "reason": clean["reason"],
                      "user_id": user["id"], "user_name": user["name"]},
            )
            return {"id": diary_id, "due_on": clean["due_on"], "reason": clean["reason"],
                    "created_by": user["name"], "created_at": now, "done_at": None, "done_by": None}

    def complete_diary(self, diary_id: str, user: dict) -> dict:
        """Mark a diary done; doing it twice keeps the first values."""
        with self.db.tx() as conn:
            row = conn.execute("SELECT * FROM diaries WHERE id=%s", (diary_id,)).fetchone()
        if row is None:
            raise CaptureError("Diary not found", 404)
        if row["done_at"]:
            return self._diary_public(row)
        with self.db.claim_tx(claim_id=row["claim_id"]) as (conn, claim):
            now = self.clock.now().isoformat()
            conn.execute(
                "UPDATE diaries SET done_at=%s, done_by=%s WHERE id=%s",
                (now, user["name"], diary_id),
            )
            lifecycle.append_event(
                conn, row["claim_id"], now, "user", "diary.done",
                data={"diary_id": diary_id, "due_on": row["due_on"], "reason": row["reason"],
                      "user_id": user["id"], "user_name": user["name"]},
            )
            return self._diary_public(
                conn.execute("SELECT * FROM diaries WHERE id=%s", (diary_id,)).fetchone())

    def queue(self) -> dict:
        """The console queue: every claim grouped by lane, plus the cycle averages.

        A fixed number of SQL statements whatever the number of claims (track 3):
        one read per table for all claims at once, and claims not yet submitted
        skip the proposal reads entirely. Same result as building claim_detail
        for each claim (tests/test_queue_statements.py checks both).
        """
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT c.id, c.status, c.created_at, c.submitted_at, c.insurer_claim_number,"
                " c.facts_json, c.reason_codes_json, c.test_mode, c.form_json, c.draft_json,"
                " s.policy_no, s.verified, s.data_json AS policy_json,"
                " (SELECT COUNT(*) FROM items i WHERE i.claim_id=c.id AND i.superseded=0) AS item_count"
                " FROM claims c JOIN policy_snapshots s ON s.id = c.policy_snapshot_id"
                " WHERE c.org_id=%s ORDER BY c.created_at DESC",
                (self.org_id,),
            ).fetchall()
            all_ids = [r["id"] for r in rows]
            submitted = [r["id"] for r in rows
                         if r["submitted_at"] is not None and r["status"] not in REOPENABLE_STATUSES]
            first_events = conn.execute(
                "SELECT DISTINCT ON (claim_id, type) claim_id, type, at FROM claim_events"
                " WHERE claim_id = ANY(%s) AND type = ANY(%s) ORDER BY claim_id, type, seq",
                (all_ids, list(desk.FIRST_TYPES)),
            ).fetchall() if all_ids else []
            batch = self._proposal_batch(conn, submitted) if submitted else {}
            reports = {row["claim_id"]: row["created_at"] for row in conn.execute(
                "SELECT claim_id, created_at FROM reports WHERE claim_id = ANY(%s)",
                (submitted,),
            ).fetchall()} if submitted else {}
            diaries_by: dict[str, list[dict]] = {}
            if all_ids:
                for row in conn.execute(
                    "SELECT * FROM diaries WHERE claim_id = ANY(%s)", (all_ids,),
                ).fetchall():
                    diaries_by.setdefault(row["claim_id"], []).append(row)
            org_tz = ZoneInfo(conn.execute(
                "SELECT default_tz FROM orgs WHERE id=%s", (self.org_id,)).fetchone()["default_tz"])
            today = self.clock.now().date().isoformat()
        events_by: dict[str, list[dict]] = {}
        for e in first_events:
            events_by.setdefault(e["claim_id"], []).append({"type": e["type"], "at": e["at"]})
        entries = []
        for r in rows:
            policy = json.loads(r["policy_json"]) if r["policy_json"] else None
            summary = {
                "id": r["id"],
                "status": r["status"],
                "created_at": r["created_at"],
                "submitted_at": r["submitted_at"],
                "insurer_claim_number": r["insurer_claim_number"],
                "item_count": r["item_count"],
                "policy_number": r["policy_no"],
                "policy_verified": bool(r["verified"]),
                "holder": policy["holder"]["name"] if policy else None,
            }
            proposal, decision_view = None, None
            if r["id"] in batch:  # early return for the rest (not submitted, or sent back): nothing to read
                b = batch[r["id"]]
                decision_view = b["decision"]
                proposal = self._proposal_view(
                    r, b["checks"], self._without_excluded(b["reading"], review.excluded(b["review"])),
                    b["consistency"],
                )
            claim_type_view = self._claim_type_view(self._answers(r))
            tz = self._claim_tz_for_policy(policy, org_tz)
            review_due = self._review_due(reports.get(r["id"]), tz)
            opens = sorted(
                (d for d in diaries_by.get(r["id"], []) if not d.get("done_at")),
                key=lambda d: (d.get("due_on") or "", d.get("created_at") or ""))
            entries.append({
                "summary": summary,
                "test_mode": bool(r["test_mode"]),
                "proposal": proposal,
                "decision": decision_view,
                "cycle": desk.cycle(events_by.get(r["id"], [])),
                "vehicle": self._vehicle_label(policy),
                "key": desk.queue_key(r["submitted_at"], proposal, decision_view, r["status"]),
                "claim_type": claim_type_view["label"] if claim_type_view else None,
                "review_due": review_due,
                "overdue": bool(review_due) and review_due < today and decision_view is None,
                "diary_due": opens[0]["due_on"] if opens else None,
                "diary_reason": opens[0]["reason"] if opens else None,
                # From the snapshot data the query already read: no new statement.
                "trial": bool(policy and policy.get("source") == "trial"),
            })
        live = [e for e in entries if not e["test_mode"]]
        def to_claim(e: dict) -> dict:
            p = e["proposal"]
            ready = p is not None and p.get("status") == "ready"
            return {
                **e["summary"],
                "lane": p.get("lane") if ready else None,
                "headline": p.get("headline") if ready else None,
                "flags": len(p.get("flags") or []) if ready else 0,
                "vehicle": e["vehicle"],
                "claim_type": e["claim_type"],
                "review_due": e["review_due"],
                "overdue": e["overdue"],
                "diary_due": e["diary_due"],
                "diary_reason": e["diary_reason"],
                "trial": e["trial"],
            }

        groups = []
        for key, label in desk.QUEUES:
            members = [e for e in live if e["key"] == key]
            if key == "decided":
                members.sort(key=lambda e: ((e["decision"] or {}).get("decided_at") or ""), reverse=True)
            elif key == "capturing":
                members.sort(key=lambda e: e["summary"]["created_at"] or "")
            else:
                members.sort(key=lambda e: e["summary"]["submitted_at"] or e["summary"]["created_at"] or "")
            claims = [to_claim(e) for e in members]
            groups.append({"key": key, "label": label, "count": len(claims), "claims": claims})
        # Test claims sit in their own lane, last: never among the live lanes,
        # never in the averages, never in System health.
        test_members = sorted(
            [e for e in entries if e["test_mode"]],
            key=lambda e: e["summary"]["submitted_at"] or e["summary"]["created_at"] or "",
        )
        if test_members:
            test_claims = [to_claim(e) for e in test_members]
            groups.append({"key": "test", "label": "Test claims",
                           "count": len(test_claims), "claims": test_claims})
        return {"groups": groups, "averages": desk.averages([e["cycle"] for e in live])}

    @staticmethod
    def _vehicle_label(policy: dict | None) -> str | None:
        vehicles = (policy or {}).get("vehicles") or []
        if vehicles and isinstance(vehicles[0], dict):
            v = vehicles[0]
            if v.get("year") and v.get("make") and v.get("model"):
                return f"{v['year']} {v['make']} {v['model']}"
        return None

    def _proposal_batch(self, conn: psycopg.Connection, claim_ids: list[str]) -> dict[str, dict]:
        """What the proposal needs for many claims, one statement per table:
        {claim_id: {"checks", "reading", "consistency", "review", "decision"}}."""
        def grouped(sql: str) -> dict[str, list[dict]]:
            out: dict[str, list[dict]] = {}
            for row in conn.execute(sql, (claim_ids,)).fetchall():
                out.setdefault(row["claim_id"], []).append(row)
            return out

        checks = grouped("SELECT * FROM checks WHERE claim_id = ANY(%s)")
        damage_rows = grouped(
            "SELECT a.claim_id, a.item_id, i.step_id AS slot_id, a.status, a.result_json, a.error, a.started_at"
            " FROM analyses a JOIN items i ON i.id = a.item_id"
            " WHERE a.claim_id = ANY(%s) AND a.kind='damage' AND i.superseded=0"
            " ORDER BY i.received_at, i.id"
        )
        story = grouped("SELECT claim_id, status, started_at FROM analyses WHERE claim_id = ANY(%s) AND kind='consistency'")
        reviews = grouped("SELECT r.* FROM reviews r JOIN claims c ON c.id = r.claim_id AND r.round = c.round"
                          " WHERE r.claim_id = ANY(%s) ORDER BY r.at")
        decisions = grouped("SELECT d.* FROM decisions d JOIN claims c ON c.id = d.claim_id AND d.round = c.round"
                            " WHERE d.claim_id = ANY(%s)")
        out = {}
        for cid in claim_ids:
            rows = checks.get(cid, [])
            out[cid] = {
                "checks": (coverage.load(None, cid, rows) + photo_check.load(None, cid, rows)
                           + consistency.load(None, cid, rows) + claim_type.load(None, cid, rows)),
                "reading": self._damage_from_rows(damage_rows.get(cid, [])),
                "consistency": self._consistency_from_row((story.get(cid) or [None])[0]),
                "review": review.state(reviews.get(cid, [])),
                "decision": self._decision_from_row((decisions.get(cid) or [None])[0]),
            }
        return out

    @classmethod
    def _decision_view(cls, conn: psycopg.Connection, claim_id: str) -> dict | None:
        """The recorded decision for claim_detail: None before one is recorded."""
        row = conn.execute(
            "SELECT d.* FROM decisions d JOIN claims c ON c.id = d.claim_id AND d.round = c.round"
            " WHERE d.claim_id=%s", (claim_id,)).fetchone()
        return cls._decision_from_row(row)

    @classmethod
    def _earlier_decisions(cls, conn: psycopg.Connection, claim_id: str) -> list[dict]:
        """Decisions of earlier rounds (each a Send back), oldest first."""
        rows = conn.execute(
            "SELECT d.* FROM decisions d JOIN claims c ON c.id = d.claim_id AND d.round < c.round"
            " WHERE d.claim_id=%s ORDER BY d.round", (claim_id,)).fetchall()
        return [{**cls._decision_from_row(r), "round": r["round"]} for r in rows]

    @staticmethod
    def _decision_from_row(row: dict | None) -> dict | None:
        if not row:
            return None
        return {
            "action": row["action"],
            "label": decision.ACTIONS.get(row["action"], row["action"]),
            "lane_before": row["lane_before"],
            "lane_after": row["lane_after"],
            "reserve_before": row["reserve_before"],
            "reserve_after": row["reserve_after"],
            "reason": row["reason"],
            "slots": json.loads(row["slots_json"] or "[]"),
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
            "version": row["version"],
        }

    def set_fact(
        self, claim_id: str, name: str, value, source: str, evidence: list[str]
    ) -> dict:
        """Store one fact with where it came from. Coverage facts only from the policy."""
        if source not in vocab.SOURCES:
            raise CaptureError(f"Unknown source '{source}'")
        with self.db.claim_tx(claim_id=claim_id) as (conn, _claim):
            claim = conn.execute(
                "SELECT c.facts_json, c.policy_snapshot_id, s.verified FROM claims c"
                " JOIN policy_snapshots s ON s.id = c.policy_snapshot_id WHERE c.id=%s",
                (claim_id,),
            ).fetchone()
            if not claim:
                raise CaptureError("Claim not found", 404)
            if name in vocab.POLICY_ONLY_FACTS and (
                source != "VERIFIED"
                or not claim["verified"]
                or f"policy_snapshot:{claim['policy_snapshot_id']}" not in evidence
            ):
                raise CaptureError(f"'{name}' can only come from this claim's policy snapshot")
            return self._put_fact(conn, claim_id, name, value, source, evidence)

    @staticmethod
    def _put_fact(conn: psycopg.Connection, claim_id: str, name: str, value, source: str, evidence: list[str]) -> dict:
        row = conn.execute("SELECT facts_json FROM claims WHERE id=%s", (claim_id,)).fetchone()
        facts = json.loads(row["facts_json"])
        facts[name] = {"value": value, "source": source, "evidence": list(evidence)}
        conn.execute("UPDATE claims SET facts_json=%s WHERE id=%s", (policies.canonical(facts), claim_id))
        return facts[name]

    def events(self, claim_id: str) -> list[dict]:
        with self.db.tx() as conn:
            return lifecycle.events(conn, claim_id)

    def _seal_info(self, conn: psycopg.Connection, claim_id: str) -> dict | None:
        row = conn.execute(
            "SELECT manifest_json, key_id, sealed_at, round FROM seals WHERE claim_id=%s ORDER BY round DESC LIMIT 1",
            (claim_id,)).fetchone()
        if not row:
            return None
        return {
            "sealed_at": row["sealed_at"],
            "key_id": row["key_id"],
            "manifest_sha256": hashlib.sha256(row["manifest_json"].encode()).hexdigest(),
            **({"round": row["round"]} if row["round"] > 1 else {}),
        }

    def _sessions(self, conn: psycopg.Connection, claim_id: str) -> list[dict]:
        return capture.sessions(conn, claim_id)

    def verify_chain(self, claim_id: str) -> dict:
        with self.db.tx() as conn:
            return lifecycle.verify_chain(conn, claim_id)

    def list_claims(self) -> list[dict]:
        with self.db.tx() as conn:
            rows = conn.execute(
                "SELECT c.id, c.status, c.created_at, c.submitted_at,"
                " c.insurer_claim_number,"
                " s.policy_no, s.verified, s.data_json AS policy_json,"
                " (SELECT COUNT(*) FROM items i WHERE i.claim_id=c.id AND i.superseded=0) AS item_count"
                " FROM claims c JOIN policy_snapshots s ON s.id = c.policy_snapshot_id"
                " WHERE c.org_id=%s ORDER BY c.created_at DESC",
                (self.org_id,),
            ).fetchall()
        claims = []
        for r in rows:
            policy = json.loads(r["policy_json"]) if r["policy_json"] else None
            claims.append(
                {
                    "id": r["id"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "submitted_at": r["submitted_at"],
                    "insurer_claim_number": r["insurer_claim_number"],
                    "item_count": r["item_count"],
                    "policy_number": r["policy_no"],
                    "policy_verified": bool(r["verified"]),
                    "holder": policy["holder"]["name"] if policy else None,
                    "trial": bool(policy and policy.get("source") == "trial"),
                }
            )
        return claims

    def list_policies(self) -> list[dict]:
        with self.db.tx() as conn:
            return policies.list_policies(conn, self.org_id)

    def policy_data(self, policy_no: str) -> dict | None:
        """The full seeded policy without policy_no, as the mock sends it;
        None when the insurer's number names no policy."""
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT data_json FROM policies WHERE org_id=%s AND policy_no=%s",
                (self.org_id, (policy_no or "").strip().upper()),
            ).fetchone()
        if not row or not row["data_json"]:
            return None
        policy = json.loads(row["data_json"])
        policy.pop("policy_no", None)
        return policy

    def claim_detail(self, claim_id: str) -> dict:
        with self.db.tx() as conn:
            claim = conn.execute("SELECT * FROM claims WHERE id=%s", (claim_id,)).fetchone()
            if not claim:
                raise CaptureError("Claim not found", 404)
            items = conn.execute(
                "SELECT * FROM items WHERE claim_id=%s ORDER BY received_at", (claim_id,)
            ).fetchall()
            snapshot = conn.execute(
                "SELECT * FROM policy_snapshots WHERE id=%s", (claim["policy_snapshot_id"],)
            ).fetchone()
            photo_rows, photo_ctx = self._photo_rows(conn, claim, snapshot)
            photo_view = photo_check.view(photo_rows, photo_ctx, self.clock.now())
            evts = lifecycle.events(conn, claim_id)
            checks = (coverage.load(conn, claim["id"]) + photo_check.load(conn, claim["id"])
                      + consistency.load(conn, claim["id"]) + claim_type.load(conn, claim["id"]))
            if claim["submitted_at"] is not None:  # cheap signals, computed on read like the proposal
                checks += signals.rows(photo_rows, photo_ctx, evts, self._answers(claim))
            reading = self._damage_view(conn, claim["id"])
            consistency_status = self._consistency_status(conn, claim["id"])
            review_state = self._review_state(conn, claim_id)
            mode_row = conn.execute(
                "SELECT delivery_mode FROM org_settings WHERE org_id=%s", (self.org_id,)
            ).fetchone()
            delivery_mode = (mode_row["delivery_mode"]
                             if mode_row and mode_row["delivery_mode"] in ("on_ready", "on_decision")
                             else "on_ready")
            priced_reading = self._without_excluded(reading, review.excluded(review_state))
            proposal = self._proposal_view(claim, checks, priced_reading, consistency_status)
            form_answers = self._answers(claim)
            if proposal is not None and proposal.get("status") == "ready":
                proposal = {**proposal, "flags": self._keyed_flags(proposal.get("flags"), form_answers),
                            "notes": self._keyed_flags(proposal.get("notes"), form_answers)}
            keyed_damage = {**reading, "damages": self._with_outlines(
                conn, claim_id, self._keyed_damages(reading.get("damages")))}
            report_row = conn.execute(
                "SELECT ai_summary_json, created_at FROM reports WHERE claim_id=%s", (claim_id,)
            ).fetchone()
            review_due = self._review_due(
                report_row["created_at"] if report_row else None,
                self._claim_tz(conn, snapshot))
            diary_rows = conn.execute(
                "SELECT * FROM diaries WHERE claim_id=%s", (claim_id,)
            ).fetchall()
            stored_summary = None
            if report_row and report_row["ai_summary_json"]:
                try:
                    stored_summary = json.loads(report_row["ai_summary_json"])
                except ValueError:
                    stored_summary = None
            if isinstance(stored_summary, dict):
                stored_summary = {**stored_summary, "caption": ai_summary.CAPTION}
            org_row = conn.execute("SELECT name FROM orgs WHERE id=%s", (self.org_id,)).fetchone()
            summary_text = None
            if proposal is not None and proposal.get("status") == "ready":
                try:
                    built = report.build(
                        {"id": claim["id"], "insurer_claim_number": claim["insurer_claim_number"],
                         "policy": policies.snapshot_public(snapshot),
                         "answers": self._answers(claim), "checks": checks,
                         "damage": keyed_damage, "proposal": proposal,
                         "seal": self._seal_info(conn, claim_id), "events": evts},
                        (org_row["name"] if org_row else ""),
                        self.clock.now(), self._claim_tz(conn, snapshot))
                    built["review"] = review_state
                    summary_text = report.summary(built)
                except Exception:
                    logger.exception("report failed for claim %s", claim["id"])
                    summary_text = None
            removal = None
            if claim.get("removal_json"):
                try:
                    removal = json.loads(claim["removal_json"])
                except ValueError:
                    removal = {"claim_id": claim["id"], "removed": [],
                               "removed_at": None, "reason": None, "by": None}
            policy_public = policies.snapshot_public(snapshot)
            policy_data = policy_public.get("data")
            fact_map = json.loads(claim["facts_json"])
            return {
                "id": claim["id"],
                "token": claim["token"],
                "status": claim["status"],
                "insurer_claim_number": claim["insurer_claim_number"],
                "callback_url": claim["callback_url"],
                "test_mode": bool(claim["test_mode"]),
                "policy": policy_public,
                "facts": fact_map,
                "decisions": {f: claim[f] for f in vocab.DECISION_FIELDS},
                "reason_codes": json.loads(claim["reason_codes_json"]),
                "missing_information": json.loads(claim["missing_info_json"]),
                "events": evts,
                "decision": self._decision_view(conn, claim_id),
                "round": claim["round"],
                "requested_slots": json.loads(claim["reopen_slots_json"]) if claim["reopen_slots_json"] else [],
                "earlier_decisions": self._earlier_decisions(conn, claim_id),
                "cycle": desk.cycle(evts),
                "sessions": self._sessions(conn, claim_id),
                "created_at": claim["created_at"],
                "expires_at": claim["expires_at"],
                "submitted_at": claim["submitted_at"],
                "checks": checks,
                "photo_checks": photo_view,
                "damage": keyed_damage,
                "consistency": consistency_status,
                "proposal": proposal,
                "review": review_state,
                "delivery_mode": delivery_mode,
                "summary": summary_text,
                "ai_summary": stored_summary,
                "ai_usage": meta_client.usage(conn, claim_id),
                "human_review_required": bool(json.loads(claim["reason_codes_json"])),
                "seal": self._seal_info(conn, claim_id),
                "removal": removal,
                "report": self._report_view(conn, claim_id),
                "answers": self._answers(claim),
                "to_verify": loss_notice.to_verify(self._answers(claim)),
                "claim_type": self._claim_type_view(self._answers(claim)),
                "layout": self._layout(claim, snapshot, items),
                "items": [self._item_public(i) for i in items],
                "contact": self._contact_view(evts, policy_data),
                "coverage": self._coverage_view(fact_map, policy_data),
                "review_due": review_due,
                "diaries": self._diaries_view(diary_rows),
            }

    def item_file(self, item_id: str) -> ItemFile:
        with self.db.tx() as conn:
            row = conn.execute("SELECT path, mime, removed_at FROM items WHERE id=%s", (item_id,)).fetchone()
        if not row:
            raise CaptureError("Item not found", 404)
        if row.get("removed_at"):
            raise CaptureError(f"Removed on {row['removed_at']}", 410)
        return self._file_target(row["path"], row["mime"])

    # --- customer side (by link token) ---

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
                conn.execute(
                    "INSERT INTO items (id, claim_id, step_id, kind, mime, path, size, sha256,"
                    " received_at, client_meta_json) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
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
                    data={"item_id": item_id, "slot": step_id, "sha256": sha256, "size": len(data)},
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

    # --- retention and deletion on request (T50) ---

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
        days = self.retention_days()
        cutoff = (self.clock.now() - timedelta(days=days)).isoformat()
        with self.db.tx() as conn:
            unfinished = [r["id"] for r in conn.execute(
                "SELECT id FROM claims WHERE org_id=%s AND removal_json IS NOT NULL"
                " AND removal_done_at IS NULL ORDER BY created_at LIMIT %s",
                (self.org_id, RETENTION_SWEEP_LIMIT)).fetchall()]
            candidate_ids = [r["id"] for r in conn.execute(
                "SELECT c.id FROM claims c JOIN decisions d ON d.claim_id=c.id"
                " WHERE c.org_id=%s AND c.removal_json IS NULL"
                " AND EXISTS (SELECT 1 FROM items i WHERE i.claim_id=c.id AND i.removed_at IS NULL)"
                " AND NOT EXISTS (SELECT 1 FROM outbox o WHERE o.claim_id=c.id"
                " AND o.delivered_at IS NULL AND o.dead_at IS NULL"
                " AND (c.callback_url IS NOT NULL OR o.attempts >= 1))"
                " GROUP BY c.id HAVING MAX(d.decided_at::timestamptz) < %s::timestamptz"
                " ORDER BY MAX(d.decided_at::timestamptz) LIMIT %s",
                (self.org_id, cutoff, RETENTION_SWEEP_LIMIT)).fetchall()]
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

    # --- durable background work (track 3) ---

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

    def submit(self, token: str, answers: dict | None = None) -> dict:
        with self.db.claim_tx(token=token) as (conn, claim):
            self._check_open(claim)
            snapshot = conn.execute(
                "SELECT * FROM policy_snapshots WHERE id=%s", (claim["policy_snapshot_id"],)
            ).fetchone()
            if answers is not None:
                clean = loss_notice.effective(loss_notice.clean_answers(answers), self._policy_view(snapshot))
                self._log_hint_edits(conn, claim, snapshot, clean)
                claim["draft_json"] = policies.canonical(clean)
                conn.execute(
                    "UPDATE claims SET draft_json=%s WHERE id=%s",
                    (claim["draft_json"], claim["id"]),
                )
            items = conn.execute(
                "SELECT * FROM items WHERE claim_id=%s AND superseded=0", (claim["id"],)
            ).fetchall()
            submitted_answers = loss_notice.effective(self._answers(claim), self._policy_view(snapshot))
            layout = self._layout(claim, snapshot, items)
            if not layout["complete"]:
                raise CaptureError("Please complete: " + ", ".join(layout["missing"]))
            resubmit = claim["reopen_slots_json"] is not None
            if resubmit:
                self._require_retakes(conn, claim, items)
            policy = self._policy_view(snapshot)
            result = loss_notice.outcome(
                submitted_answers, layout, bool(snapshot["verified"]), submitted_answers.get("vin") in (policy or {}).get("vins", [])
            )
            now = self.clock.now().isoformat()
            form_json = policies.canonical(submitted_answers)
            rnd = claim["round"] + 1 if resubmit else claim["round"]
            if resubmit:
                conn.execute(
                    "UPDATE claims SET round=%s, reopen_slots_json=NULL WHERE id=%s", (rnd, claim["id"]))
            lifecycle.transition(
                conn, claim["id"], "submitted", "customer", now,
                {"form_sha256": hashlib.sha256(form_json.encode()).hexdigest(),
                 **({"round": rnd} if resubmit else {})},
            )
            conn.execute(
                "UPDATE claims SET submitted_at=%s, form_json=%s, reason_codes_json=%s, missing_info_json=%s WHERE id=%s",
                (
                    now,
                    form_json,
                    policies.canonical(result["reason_codes"]),
                    policies.canonical(result["missing_information"]),
                    claim["id"],
                ),
            )
            for name, (value, evidence) in result["facts"].items():
                self._put_fact(conn, claim["id"], name, value, "REPORTED", evidence)
            if result["reason_codes"]:
                lifecycle.append_event(
                    conn, claim["id"], now, "system", "claim.flagged",
                    data={"reason_codes": result["reason_codes"], "human_review_required": True},
                )
            self._seal(conn, claim["id"], now, rnd)
            damage_rows = 0
            for item in items:
                if not item["superseded"] and item["step_id"] in damage.DAMAGE_SLOTS:
                    # A resubmission keeps the readings of photos that did not change.
                    conn.execute(
                        "INSERT INTO analyses (claim_id, kind, item_id, status, version, started_at)"
                        " VALUES (%s, 'damage', %s, 'pending', %s, %s)"
                        " ON CONFLICT (claim_id, kind, item_id) DO NOTHING",
                        (
                            claim["id"], item["id"],
                            f"{damage.VERSION}+{damage_prompts.VERSION}", now,
                        ),
                    )
                    damage_rows += 1
            if damage_rows and self._has_damage_slots(submitted_answers):
                # The statement-vs-photos check always runs again on a resubmission.
                conn.execute(
                    "INSERT INTO analyses (claim_id, kind, item_id, status, version, started_at)"
                    " VALUES (%s, 'consistency', '', 'pending', %s, %s)"
                    " ON CONFLICT (claim_id, kind, item_id) DO UPDATE SET status='pending',"
                    " result_json=NULL, error=NULL, version=EXCLUDED.version,"
                    " started_at=EXCLUDED.started_at, finished_at=NULL, attempts=1",
                    (
                        claim["id"],
                        f"{consistency.VERSION}+{consistency_prompts.VERSION}", now,
                    ),
                )
            policy_data = json.loads(snapshot["data_json"]) if snapshot["data_json"] else None
            results = coverage.check(policy_data, bool(snapshot["verified"]), submitted_answers)
            coverage.save(conn, claim["id"], results, now)
            lifecycle.append_event(
                conn, claim["id"], now, "system", "coverage.checked",
                data={"version": coverage.VERSION, "results": {r["check_id"]: r["result"] for r in results}},
            )
            photo_check.save(conn, claim["id"], self._photo_view(conn, claim, snapshot), now)
            bought = next(r for r in results if r["check_id"] == "coverage.coverage_bought")
            if bought["result"] == "pass":
                needed = bought["value"]
                evidence = [f"policy_snapshot:{snapshot['id']}", f"policy:coverages.{needed}"]
                self._put_fact(conn, claim["id"], "coverage_type", needed, "VERIFIED", evidence)
                deductible = next(r for r in results if r["check_id"] == "coverage.deductible")
                if deductible["result"] == "value":
                    self._put_fact(conn, claim["id"], "deductible_amount", deductible["value"], "VERIFIED", evidence)
            return {"claim_id": claim["id"], "submitted_at": now}

    def _require_retakes(self, conn: psycopg.Connection, claim: dict, items: list[dict]) -> None:
        """After a Send back, every requested slot needs a shot taken since the decision."""
        missing = [(loss_notice.all_slots().get(slot) or {}).get("title") or slot
                   for slot in self._retakes_left(conn, claim, items)]
        if missing:
            raise CaptureError("Please retake: " + ", ".join(missing))

    @staticmethod
    def _retakes_left(conn: psycopg.Connection, claim: dict, items: list[dict]) -> list[str]:
        """Requested slots (after a Send back) with no shot taken since the decision."""
        if not claim["reopen_slots_json"]:
            return []
        row = conn.execute(
            "SELECT decided_at FROM decisions WHERE claim_id=%s AND round=%s", (claim["id"], claim["round"])
        ).fetchone()
        since = datetime.fromisoformat(row["decided_at"] if row else claim["created_at"])
        return [slot for slot in json.loads(claim["reopen_slots_json"])
                if not any(i["step_id"] == slot and datetime.fromisoformat(i["received_at"]) > since for i in items)]

    def _seal(self, conn: psycopg.Connection, claim_id: str, now: str, rnd: int = 1) -> None:
        """Sign the manifest in the same transaction as the submit: a claim is never submitted unsealed.
        A resubmission after Send back is sealed again as the next round, naming the earlier seal."""
        claim = conn.execute("SELECT * FROM claims WHERE id=%s", (claim_id,)).fetchone()
        snapshot = conn.execute("SELECT * FROM policy_snapshots WHERE id=%s", (claim["policy_snapshot_id"],)).fetchone()
        items = conn.execute(
            "SELECT * FROM items WHERE claim_id=%s AND superseded=0 ORDER BY received_at, id", (claim_id,)
        ).fetchall()
        sessions = [r["code"] for r in conn.execute("SELECT code FROM capture_sessions WHERE claim_id=%s ORDER BY started_at", (claim_id,))]
        chain = lifecycle.verify_chain(conn, claim_id)
        if not chain["ok"]:
            raise CaptureError("The claim's event log does not verify; it cannot be sealed", 500)
        count = conn.execute("SELECT COUNT(*) AS n FROM claim_events WHERE claim_id=%s", (claim_id,)).fetchone()["n"]
        manifest = seal.build_manifest(
            claim, snapshot, items, claim["form_json"], sessions, {"head": chain["head"], "count": count}, now, self.sealer.key_id
        )
        if rnd > 1:
            previous = conn.execute(
                "SELECT manifest_json FROM seals WHERE claim_id=%s AND round=%s", (claim_id, rnd - 1)
            ).fetchone()
            manifest["round"] = rnd
            manifest["previous_manifest_sha256"] = (
                hashlib.sha256(previous["manifest_json"].encode()).hexdigest() if previous else None)
        body, signature = self.sealer.sign(manifest)
        conn.execute(
            "INSERT INTO seals (claim_id, manifest_json, signature, key_id, sealed_at, round)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (claim_id, body.decode(), signature, self.sealer.key_id, now, rnd),
        )
        lifecycle.append_event(
            conn, claim_id, now, "system", "claim.sealed",
            data={"manifest_sha256": hashlib.sha256(body).hexdigest(), "key_id": self.sealer.key_id, "items": len(items),
                  **({"round": rnd} if rnd > 1 else {})},
        )

    def package(self, claim_id: str) -> bytes:
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM seals WHERE claim_id=%s ORDER BY round DESC LIMIT 1", (claim_id,)).fetchone()
            if not row:
                raise CaptureError("This claim has not been submitted and sealed yet", 409)
            claim = conn.execute(
                "SELECT form_json, policy_snapshot_id, removal_json, removal_signature"
                " FROM claims WHERE id=%s", (claim_id,)).fetchone()
            snapshot = conn.execute("SELECT data_json FROM policy_snapshots WHERE id=%s", (claim["policy_snapshot_id"],)).fetchone()
            paths = {r["id"]: r["path"] for r in conn.execute("SELECT id, path FROM items WHERE claim_id=%s", (claim_id,))}
        manifest = json.loads(row["manifest_json"])
        if claim.get("removal_json") and claim.get("removal_signature"):
            return seal.build_package(
                row["manifest_json"].encode(), row["signature"], self.sealer.public_key_b64,
                claim["form_json"], snapshot["data_json"] or "", {},
                removal_json=claim["removal_json"].encode(),
                removal_sig=claim["removal_signature"],
            )
        # Files are read from the blob store as they are now, so a file edited after sealing shows up in Verify.
        files: dict[str, bytes] = {}
        for i in manifest["items"]:
            try:
                files[i["file"]] = self.blobs.get(paths[i["id"]])
            except BlobNotFound:
                raise CaptureError(f"A sealed file is missing from storage: {i['file']}", 500) from None
        return seal.build_package(
            row["manifest_json"].encode(), row["signature"], self.sealer.public_key_b64,
            claim["form_json"], snapshot["data_json"] or "", files,
        )

    def verify_package(self, data) -> dict:
        """data: the package bytes or a binary file object (read entry by entry)."""
        return self.sealer.verify_package(data)

    # --- internals ---

    @staticmethod
    def _step(claim: dict, step_id: str) -> dict:
        step = loss_notice.all_slots().get(step_id) if claim["template_id"] == loss_notice.TEMPLATE_ID else None
        if not step:
            raise CaptureError(f"Unknown step '{step_id}'")
        return step

    @staticmethod
    def _answers(claim: dict) -> dict:
        return json.loads(claim["form_json"] or claim["draft_json"] or "{}")

    def _layout(self, claim: dict, snapshot: dict, items: list[dict]) -> dict:
        counts: dict[str, int] = {}
        for item in items:
            if not item["superseded"]:
                counts[item["step_id"]] = counts.get(item["step_id"], 0) + 1
        latest_date = self.clock.now().astimezone(timezone(timedelta(hours=14))).date()
        policy = self._policy_view(snapshot)
        return loss_notice.layout(
            loss_notice.effective(self._answers(claim), policy), policy, counts, latest_date=latest_date
        )

    @staticmethod
    def _policy_view(snapshot: dict) -> dict:
        """What the claimant sees of their own policy, plus VINs for matching."""
        if not snapshot["data_json"]:
            return {"policy_no": snapshot["policy_no"], "verified": False, "state": None,
                    "holder": None, "holder_name": None, "holder_phone": None, "vins": []}
        p = json.loads(snapshot["data_json"])
        v = p["vehicles"][0]
        return {
            "policy_no": p["policy_no"],
            "verified": bool(snapshot["verified"]),
            "state": p["state"],
            "holder": p["holder"]["name"],
            "holder_name": p["holder"]["name"],
            "holder_phone": p["holder"].get("phone"),
            "vehicle": f"{v['year']} {v['make']} {v['model']}",
            "plate": v["plate"],
            "vin": v["vin"],
            "coverages": p["coverages"],
            "vins": [x["vin"] for x in p["vehicles"]],
        }

    def _claim_tz(self, conn: psycopg.Connection, snapshot: dict) -> ZoneInfo:
        """Claim IDs carry the local date where the policy is written, not the UTC date."""
        state = json.loads(snapshot["data_json"])["state"] if snapshot["data_json"] else None
        if state in policies.STATE_TZ:
            return ZoneInfo(policies.STATE_TZ[state])
        org = conn.execute("SELECT default_tz FROM orgs WHERE id=%s", (self.org_id,)).fetchone()
        return ZoneInfo(org["default_tz"])

    @staticmethod
    def _claim_type_view(answers: dict) -> dict | None:
        """{"id", "label"} from the overlay; None when no type."""
        overlay = loss_notice.template()["overlays"].get((answers or {}).get("loss_type"))
        if overlay is None:
            return None
        return {"id": overlay["id"], "label": overlay["label"]}

    @staticmethod
    def _contact_view(events: list[dict], policy_data: dict | None) -> dict:
        """Who to call (T52): the capture request's claimant when given, else the
        policy holder; missing values are null."""
        claimant = None
        for e in events or []:
            if e.get("type") == "claim.opened":
                claimant = (e.get("data") or {}).get("claimant")
                break
        holder = (policy_data or {}).get("holder")
        src = claimant if isinstance(claimant, dict) and claimant else holder
        src = src if isinstance(src, dict) else {}
        return {"name": src.get("name") or None,
                "phone": src.get("phone") or None,
                "email": src.get("email") or None}

    @staticmethod
    def _coverage_view(facts: dict, policy_data: dict | None) -> dict:
        """The coverage that applies (T52): the bought type and deductible from
        the facts, the lienholder from the policy."""
        facts = facts if isinstance(facts, dict) else {}
        raw_type = facts.get("coverage_type")
        raw_type = raw_type.get("value") if isinstance(raw_type, dict) else None
        raw_deductible = facts.get("deductible_amount")
        raw_deductible = raw_deductible.get("value") if isinstance(raw_deductible, dict) else None
        return {
            "type": raw_type if raw_type in ("collision", "comprehensive") else None,
            "deductible": raw_deductible if type(raw_deductible) is int else None,
            "lienholder": (policy_data or {}).get("lienholder") or None,
        }

    @staticmethod
    def _review_due(report_created_at: str | None, tz) -> str | None:
        """The review due date (T52): the same date as the report's activity
        (two business days after the report day); None before a report exists."""
        if not report_created_at:
            return None
        try:
            stamped = datetime.fromisoformat(report_created_at)
        except (TypeError, ValueError):
            return None
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=UTC)
        return report.business_days_after(
            stamped.astimezone(tz).date(), report.REVIEW_BUSINESS_DAYS).isoformat()

    @staticmethod
    def _diary_public(row: dict) -> dict:
        """The diary shape the console shows: never the claim's internals."""
        return {
            "id": row["id"],
            "due_on": row["due_on"],
            "reason": row["reason"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "done_at": row["done_at"],
            "done_by": row["done_by"],
        }

    @classmethod
    def _diaries_view(cls, rows: list[dict] | None) -> list[dict]:
        """Diaries for a claim (T52): open first by due date, then done ones newest first."""
        rows = rows or []
        open_rows = sorted(
            (r for r in rows if not r.get("done_at")),
            key=lambda r: (r.get("due_on") or "", r.get("created_at") or ""))
        done_rows = sorted(
            (r for r in rows if r.get("done_at")),
            key=lambda r: (r.get("done_at") or "", r.get("created_at") or ""), reverse=True)
        return [cls._diary_public(r) for r in open_rows + done_rows]

    @staticmethod
    def _claim_tz_for_policy(policy_data: dict | None, org_tz) -> ZoneInfo:
        """The claim-local timezone from already-read policy data (for the queue,
        which cannot afford a snapshot read per claim)."""
        state = (policy_data or {}).get("state")
        if state in policies.STATE_TZ:
            return ZoneInfo(policies.STATE_TZ[state])
        return org_tz

    def _check_open(self, claim: dict, allow_submitted: bool = False) -> dict:
        if claim["status"] not in CAPTURE_STATUSES:
            if allow_submitted:
                return claim
            raise CaptureError("This claim was already submitted", 409)
        if datetime.fromisoformat(claim["expires_at"]) < self.clock.now():
            raise CaptureError("This link has expired", 410)
        return claim

    @staticmethod
    def _item_public(row: dict) -> dict:
        return {
            "id": row["id"],
            "step_id": row["step_id"],
            "kind": row["kind"],
            "mime": row["mime"],
            "size": row["size"],
            "sha256": row["sha256"],
            "received_at": row["received_at"],
            "superseded": bool(row["superseded"]),
            "client_meta": json.loads(row["client_meta_json"]),
            "removed_at": row.get("removed_at"),
            "removed_reason": row.get("removed_reason"),
        }
