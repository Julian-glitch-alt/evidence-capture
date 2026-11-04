"""Store domain mixin: sealing (split from store.py, behaviour unchanged)."""

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


class SealingMixin:
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
            manifest_for_events = json.loads(row["manifest_json"])
            sealed_count = manifest_for_events.get("event_count")
            all_events = lifecycle.events(conn, claim_id)
            # The manifest signs the chain as it stood at sealing; later
            # analysis events are not part of the sealed log.
            sealed_events = all_events[:sealed_count] if isinstance(sealed_count, int) else all_events
            events_jsonl = lifecycle.events_jsonl(sealed_events)
        manifest = json.loads(row["manifest_json"])
        if claim.get("removal_json") and claim.get("removal_signature"):
            return seal.build_package(
                row["manifest_json"].encode(), row["signature"], self.sealer.public_key_b64,
                claim["form_json"], snapshot["data_json"] or "", {},
                removal_json=claim["removal_json"].encode(),
                removal_sig=claim["removal_signature"],
                events_jsonl=events_jsonl,
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
            events_jsonl=events_jsonl,
        )

    def verify_package(self, data) -> dict:
        """data: the package bytes or a binary file object (read entry by entry)."""
        return self.sealer.verify_package(data)

