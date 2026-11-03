"""The mock claims system, "Demo Mutual · Claims System (mock)" (tasks/t16-t29-design.md).

Shaped like the systems adjusters use: a Desktop with My Activities, and a claim
file with Summary, Exposures, Parties, Policy, Financials, Workplan, Notes,
Documents and Services. It talks to us only over HTTP through its transport, as
a real carrier would: no direct calls into our claim tables, no shared tables. Its only
window into our side is `policies_source`, which stands for the insurer's own
policy system (read-only).
"""

import json
import logging
import uuid
from collections.abc import Callable

from . import integration, policies, report
from .db import Database
from .errors import CaptureError

logger = logging.getLogger(__name__)

VENDOR = "Evidence Capture"


def _with_link(description: str | None, url, base: str) -> str | None:
    """An activity may carry a link (e.g. to the vendor's console); relative links
    resolve against the vendor's base URL, like document links."""
    if not isinstance(url, str) or not url:
        return description
    if url.startswith("/"):
        url = base + url
    return f"{description}\n{url}" if description else url


def _new_id() -> str:
    return uuid.uuid4().hex


class DemoInsurer:
    def __init__(
        self,
        db: Database,
        clock,
        policies_source: Callable[[], list[dict]],
        policy_data: Callable[[str], dict | None] | None = None,
    ):
        self.db = db
        self.clock = clock
        self.policies_source = policies_source
        self.policy_data = policy_data
        self.http: integration.Http = integration.httpx_transport()

    # --- desktop and claim file (reads) ---

    def desktop(self) -> dict:
        with self.db.tx() as conn:
            activities = conn.execute(
                "SELECT a.*, c.claim_number FROM mock_activities a"
                " JOIN mock_claims c ON c.id = a.claim_id"
                " WHERE a.status = 'Open' ORDER BY a.due_date, a.created_at"
            ).fetchall()
            claims = conn.execute("SELECT * FROM mock_claims ORDER BY created_at DESC").fetchall()
            services = {
                r["claim_id"]: r for r in conn.execute("SELECT * FROM mock_services").fetchall()
            }
        return {
            "activities": [
                {
                    "id": a["id"],
                    "claim_number": a["claim_number"],
                    "subject": a["subject"],
                    "priority": a["priority"],
                    "due_date": a["due_date"],
                    "status": a["status"],
                    "source": a["source"],
                }
                for a in activities
            ],
            "claims": [
                {
                    "claim_number": c["claim_number"],
                    "insured": c["insured_name"],
                    "loss_date": c["loss_date"],
                    "status": c["status"],
                    "service_status": services[c["id"]]["status"] if c["id"] in services else None,
                }
                for c in claims
            ],
        }

    def policies(self) -> list[dict]:
        return self.policies_source()

    def claim_view(self, claim_number: str) -> dict:
        with self.db.tx() as conn:
            claim = conn.execute(
                "SELECT * FROM mock_claims WHERE claim_number=%s", (claim_number,)
            ).fetchone()
            if not claim:
                raise CaptureError("Claim not found", 404)
            exposures = conn.execute(
                "SELECT * FROM mock_exposures WHERE claim_id=%s", (claim["id"],)
            ).fetchall()
            services = conn.execute(
                "SELECT * FROM mock_services WHERE claim_id=%s", (claim["id"],)
            ).fetchall()
            activities = conn.execute(
                "SELECT * FROM mock_activities WHERE claim_id=%s"
                " ORDER BY CASE WHEN status = 'Open' THEN 0 ELSE 1 END, created_at",
                (claim["id"],),
            ).fetchall()
            notes = conn.execute(
                "SELECT * FROM mock_notes WHERE claim_id=%s ORDER BY created_at DESC",
                (claim["id"],),
            ).fetchall()
            documents = conn.execute(
                "SELECT * FROM mock_documents WHERE claim_id=%s ORDER BY created_at",
                (claim["id"],),
            ).fetchall()
            flags = conn.execute(
                "SELECT * FROM mock_flags WHERE claim_id=%s ORDER BY created_at", (claim["id"],)
            ).fetchall()
            policy = next(
                (p for p in self.policies_source() if p["policy_no"] == claim["policy_number"]),
                None,
            )
        return {
            "id": claim["id"],
            "claim_number": claim["claim_number"],
            "policy_number": claim["policy_number"],
            "insured_name": claim["insured_name"],
            "vehicle": json.loads(claim["vehicle_json"]),
            "loss_date": claim["loss_date"],
            "loss_cause": claim["loss_cause"],
            "description": claim["description"],
            "claimant_phone": claim["claimant_phone"],
            "status": claim["status"],
            "created_at": claim["created_at"],
            "policy": policy,
            "parties": [
                {"role": "Insured", "name": claim["insured_name"]},
                {"role": "Claimant", "name": claim["insured_name"], "phone": claim["claimant_phone"]},
            ],
            "exposures": [
                {
                    "id": e["id"],
                    "type": e["type"],
                    "coverage": e["coverage"],
                    "claimant": e["claimant"],
                    "reserve": e["reserve"],
                    "status": e["status"],
                }
                for e in exposures
            ],
            "financials": {
                "reserves": sum(e["reserve"] for e in exposures),
                "paid": 0,
                "recovered": 0,
            },
            "services": [dict(s) for s in services],
            "activities": [dict(a) for a in activities],
            "notes": [dict(n) for n in notes],
            "documents": [dict(d) for d in documents],
            "flags": [
                {**dict(f), "evidence": json.loads(f["evidence_json"])} for f in flags
            ],
        }

    # --- adjuster actions ---

    def create_claim(
        self,
        policy_number: str | None,
        loss_date: str | None,
        loss_cause: str | None,
        description: str | None,
        claimant_phone: str | None,
    ) -> dict:
        policy = next(
            (p for p in self.policies_source()
             if p["policy_no"] == (policy_number or "").strip().upper()),
            None,
        )
        if policy is None:
            raise CaptureError(f"Unknown policy '{policy_number}'")
        if loss_cause not in ("collision", "comprehensive"):
            raise CaptureError("loss_cause must be 'collision' or 'comprehensive'")
        if not loss_date:
            raise CaptureError("loss_date is required")
        if not description:
            raise CaptureError("description is required")
        coverage = "Collision" if loss_cause == "collision" else "Comprehensive"
        now = self.clock.now()
        with self.db.tx() as conn:
            seq = conn.execute("SELECT next_claim_seq FROM mock_config WHERE id = 1").fetchone()[
                "next_claim_seq"
            ]
            number = f"DM-{now.year}-{seq:04d}"
            conn.execute("UPDATE mock_config SET next_claim_seq = %s WHERE id = 1", (seq + 1,))
            claim_id = _new_id()
            conn.execute(
                "INSERT INTO mock_claims (id, claim_number, policy_number, insured_name, vehicle_json,"
                " loss_date, loss_cause, description, claimant_phone, status, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 'Open', %s)",
                (
                    claim_id,
                    number,
                    policy["policy_no"],
                    policy["holder"],
                    policies.canonical({"vehicle": policy["vehicle"], "state": policy.get("state")}),
                    loss_date,
                    loss_cause,
                    description,
                    claimant_phone,
                    now.isoformat(),
                ),
            )
            conn.execute(
                "INSERT INTO mock_exposures (id, claim_id, type, coverage, claimant, reserve, status)"
                " VALUES (%s, %s, 'Vehicle damage', %s, %s, 0, 'Open')",
                (_new_id(), claim_id, coverage, policy["holder"]),
            )
            conn.execute(
                "INSERT INTO mock_activities (id, claim_id, subject, priority, due_date,"
                " status, source, created_at)"
                " VALUES (%s, %s, 'Make initial contact with the insured',"
                " 'normal', %s, 'Open', 'system', %s)",
                (
                    _new_id(),
                    claim_id,
                    report.business_days_after(now.date(), 1).isoformat(),
                    now.isoformat(),
                ),
            )
        return self.claim_view(number)

    def request_capture(self, claim_number: str, base: str) -> dict:
        """Order a verified capture from the vendor over HTTP. Idempotent."""
        with self.db.tx() as conn:
            claim = conn.execute(
                "SELECT * FROM mock_claims WHERE claim_number=%s", (claim_number,)
            ).fetchone()
            if not claim:
                raise CaptureError("Claim not found", 404)
            if conn.execute(
                "SELECT 1 FROM mock_services WHERE claim_id=%s", (claim["id"],)
            ).fetchone():
                service_exists = True
            else:
                service_exists = False
            api_key = conn.execute("SELECT api_key FROM mock_config WHERE id = 1").fetchone()[
                "api_key"
            ]
        if service_exists:
            return self.claim_view(claim_number)
        payload = {
            "insurer_claim_number": claim["claim_number"],
            "policy_number": claim["policy_number"],
            "callback_url": f"{base}/api/demo-insurer/webhook",
            "claimant": {"name": claim["insured_name"], "phone": claim["claimant_phone"]},
            "loss": {"date": claim["loss_date"], "cause": claim["loss_cause"], "description": claim["description"]},
        }
        if self.policy_data is not None:
            full = self.policy_data(claim["policy_number"])
            if isinstance(full, dict):
                payload["policy"] = full
        body = json.dumps(payload).encode()
        try:
            status, resp_body = self.http(
                "POST",
                f"{base}/api/v1/capture-requests",
                body,
                {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            )
        except Exception as e:
            raise CaptureError(f"Vendor request failed ({type(e).__name__}: {e})", 502) from e
        if not 200 <= status < 300:
            raise CaptureError(
                f"Vendor refused the capture request ({status}):"
                f" {resp_body.decode(errors='replace')[:500]}",
                502,
            )
        data = json.loads(resp_body)
        now = self.clock.now().isoformat()
        with self.db.tx() as conn:
            if conn.execute(
                "SELECT 1 FROM mock_services WHERE claim_id=%s", (claim["id"],)
            ).fetchone():
                pass  # a parallel order won; keep the first service
            else:
                conn.execute(
                    "INSERT INTO mock_services (id, claim_id, type, vendor, status, vendor_ref,"
                    " capture_url, vendor_base_url, requested_at)"
                    " VALUES (%s, %s, 'Verified evidence capture', %s,"
                    " 'Requested', %s, %s, %s, %s)",
                    (
                        _new_id(),
                        claim["id"],
                        VENDOR,
                        data["case_id"],
                        data["capture_url"],
                        base,
                        now,
                    ),
                )
                conn.execute(
                    "INSERT INTO mock_notes (id, claim_id, subject, body, author, created_at)"
                    " VALUES (%s, %s, 'Verified capture requested', %s, 'System', %s)",
                    (
                        _new_id(),
                        claim["id"],
                        f"Link sent to the claimant (SMS simulated): {data['capture_url']}",
                        now,
                    ),
                )
        return self.claim_view(claim_number)

    def add_note(self, claim_number: str, subject: str | None, body: str | None) -> dict:
        if not subject or not body:
            raise CaptureError("subject and body are required")
        with self.db.tx() as conn:
            claim = conn.execute(
                "SELECT id FROM mock_claims WHERE claim_number=%s", (claim_number,)
            ).fetchone()
            if not claim:
                raise CaptureError("Claim not found", 404)
            conn.execute(
                "INSERT INTO mock_notes (id, claim_id, subject, body, author, created_at)"
                " VALUES (%s, %s, %s, %s, 'Adjuster', %s)",
                (_new_id(), claim["id"], subject, body, self.clock.now().isoformat()),
            )
        return self.claim_view(claim_number)

    def complete_activity(self, activity_id: str) -> dict:
        now = self.clock.now().isoformat()
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT a.*, c.claim_number FROM mock_activities a"
                " JOIN mock_claims c ON c.id = a.claim_id WHERE a.id=%s",
                (activity_id,),
            ).fetchone()
            if not row:
                raise CaptureError("Activity not found", 404)
            conn.execute(
                "UPDATE mock_activities SET status = 'Done', completed_at = %s WHERE id = %s",
                (now, activity_id),
            )
            number = row["claim_number"]
        return self.claim_view(number)

    def set_reserve(self, exposure_id: str, amount) -> dict:
        if type(amount) is not int or amount < 0:
            raise CaptureError("amount must be an integer of 0 or more")
        now = self.clock.now().isoformat()
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT e.*, c.claim_number FROM mock_exposures e"
                " JOIN mock_claims c ON c.id = e.claim_id WHERE e.id=%s",
                (exposure_id,),
            ).fetchone()
            if not row:
                raise CaptureError("Exposure not found", 404)
            conn.execute("UPDATE mock_exposures SET reserve = %s WHERE id = %s", (amount, exposure_id))
            conn.execute(
                "INSERT INTO mock_notes (id, claim_id, subject, body, author, created_at)"
                " VALUES (%s, %s, 'Reserve set', %s, 'Adjuster', %s)",
                (
                    _new_id(),
                    row["claim_id"],
                    f"Reserve on {row['type']} set to ${amount:,} by the adjuster.",
                    now,
                ),
            )
            number = row["claim_number"]
        return self.claim_view(number)

    # --- webhook (no login; the signature is the auth) ---

    def handle_webhook(self, raw_body: bytes, signature: str | None) -> dict:
        """Accept one vendor event. 401 and nothing written on a bad signature."""
        with self.db.tx() as conn:
            cfg = conn.execute("SELECT webhook_secret FROM mock_config WHERE id = 1").fetchone()
        secret = (cfg["webhook_secret"] if cfg else "").encode()
        if not signature or not report.verify_signature(raw_body, secret, signature):
            raise CaptureError("Bad signature", 401)
        try:
            stamp = int(signature.split(",", 1)[0][2:])
        except (ValueError, IndexError):
            raise CaptureError("Bad signature", 401) from None
        if abs(int(self.clock.now().timestamp()) - stamp) > 300:
            raise CaptureError("Stale signature", 401)
        try:
            msg = json.loads(raw_body)
        except ValueError:
            raise CaptureError("Body must be JSON", 400) from None
        if not isinstance(msg, dict):
            raise CaptureError("Body must be an object", 400)
        event_id = msg.get("event_id")
        if not event_id:
            raise CaptureError("event_id is required", 400)
        number = (msg.get("claim") or {}).get("insurer_claim_number")
        now = self.clock.now().isoformat()
        with self.db.tx() as conn:
            if conn.execute(
                "SELECT 1 FROM mock_webhook_log WHERE event_id=%s", (event_id,)
            ).fetchone():
                return {"ok": True, "duplicate": True}
            claim = (
                conn.execute(
                    "SELECT * FROM mock_claims WHERE claim_number=%s", (number,)
                ).fetchone()
                if number
                else None
            )
            if claim is None:
                conn.execute(
                    "INSERT INTO mock_webhook_log (event_id, event, claim_number, received_at)"
                    " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (event_id, msg.get("event") or "", number, now),
                )
                raise CaptureError("Unknown claim", 404)
            service = conn.execute(
                "SELECT * FROM mock_services WHERE claim_id=%s", (claim["id"],)
            ).fetchone()
            base = (service["vendor_base_url"] if service else "") or ""
            for d in msg.get("documents") or []:
                url = d.get("url") or ""
                if url.startswith("/"):
                    url = base + url
                conn.execute(
                    "INSERT INTO mock_documents (id, claim_id, name, document_type, url, sha256,"
                    " source, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        _new_id(),
                        claim["id"],
                        d.get("name") or "",
                        d.get("document_type") or "",
                        url,
                        d.get("sha256"),
                        VENDOR,
                        now,
                    ),
                )
            for n in msg.get("notes") or []:
                conn.execute(
                    "INSERT INTO mock_notes (id, claim_id, subject, body, author, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        _new_id(),
                        claim["id"],
                        n.get("subject") or "",
                        n.get("body") or "",
                        f"{VENDOR} (vendor)",
                        now,
                    ),
                )
            for a in msg.get("activities") or []:
                conn.execute(
                    "INSERT INTO mock_activities (id, claim_id, subject, description, priority,"
                    " due_date, status, source, created_at)"
                    " VALUES (%s, %s, %s, %s, %s, %s, 'Open', %s, %s)",
                    (
                        _new_id(),
                        claim["id"],
                        a.get("subject") or "",
                        _with_link(a.get("description"), a.get("url"), base),
                        a.get("priority") or "normal",
                        a.get("due_date") or self.clock.now().date().isoformat(),
                        VENDOR,
                        now,
                    ),
                )
            for f in msg.get("flags") or []:
                conn.execute(
                    "INSERT INTO mock_flags (id, claim_id, level, reason, evidence_json, source,"
                    " created_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        _new_id(),
                        claim["id"],
                        f.get("level") or "",
                        f.get("reason") or "",
                        policies.canonical(f.get("evidence") or []),
                        VENDOR,
                        now,
                    ),
                )
            for prefix in msg.get("complete_activities") or []:
                conn.execute(
                    "UPDATE mock_activities SET status = 'Done', completed_at = %s"
                    " WHERE claim_id = %s AND source = %s AND status = 'Open' AND subject LIKE %s",
                    (now, claim["id"], VENDOR, f"{prefix}%"),
                )
            if msg.get("event") == "evidence.ready" and service is not None:
                conn.execute(
                    "UPDATE mock_services SET status = 'Completed', completed_at = %s WHERE id = %s",
                    (now, service["id"]),
                )
            conn.execute(
                "INSERT INTO mock_webhook_log (event_id, event, claim_number, received_at)"
                " VALUES (%s, %s, %s, %s)",
                (event_id, msg.get("event") or "", number, now),
            )
        return {"ok": True}
