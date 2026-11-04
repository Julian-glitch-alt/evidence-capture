"""Store domain mixin: claims (split from store.py, behaviour unchanged)."""

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


class ClaimsMixin:
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
        # Resolved through the facade so patching evidence_server.store works.
        from . import store as _facade

        new_claim_id = _facade._new_claim_id
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
                    claim_id = new_claim_id(local_day)
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
                checks += signals.rows(photo_rows, photo_ctx, evts, self._answers(claim),
                                       [self._item_public(i) for i in items])
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

