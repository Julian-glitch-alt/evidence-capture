"""Claims, capture links and evidence storage (Postgres + blob store).

Trust boundary: anything the phone sends (client_meta) is stored as a claim,
never as fact. Facts are what the server records itself: received_at, size, sha256.

This module is the thin Store facade: the implementation lives in domain
modules (store_claims, store_capture, store_sealing, store_reports,
store_integration, store_retention, store_users_settings) as mixins, and
shared constants and helpers in store_common. Imports here stay the same
for main.py and the tests.
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

from .store_capture import CaptureMixin
from .store_claims import ClaimsMixin
from .store_common import *
from .store_common import _liveness_assessment, _mean_abs_diff, _new_claim_id, _open_photo, _report_dir, _sniff_mime, _validate_upload
from .store_integration import IntegrationMixin
from .store_reports import ReportsMixin
from .store_retention import RetentionMixin
from .store_sealing import SealingMixin
from .store_users_settings import UsersSettingsMixin


class Store(UsersSettingsMixin, IntegrationMixin, ClaimsMixin, CaptureMixin, ReportsMixin, RetentionMixin, SealingMixin):
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
        try:
            facts = json.loads(row.get("server_facts_json") or "{}")
        except (ValueError, TypeError, AttributeError):
            facts = {}
        if not isinstance(facts, dict):
            facts = {}
        flags = facts.get("flags") if isinstance(facts.get("flags"), list) else []
        # Older rows (before server facts) still expose the server times.
        if not facts:
            facts = {
                "received_at": row["received_at"],
                "client_captured_at": None,
                "client_captured_at_source": None,
                "nonce_issued_at": None,
                "nonce_to_upload_latency_seconds": None,
                "slow_upload": False,
                "liveness": None,
                "static_source": None,
                "flags": flags,
            }
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
            "flags": flags,
            "static_source": facts.get("static_source"),
            "slow_upload": bool(facts.get("slow_upload")),
            "nonce_to_upload_latency_seconds": facts.get("nonce_to_upload_latency_seconds"),
            "server_facts": facts,
            "facts": facts,
            "liveness": facts.get("liveness"),
        }

