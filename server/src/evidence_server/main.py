import json
import logging
import os
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import integration, meta_client, policies, proposal, ratelimit, report, users as users_mod
from .auth import AdminAuth
from .blobstore import BlobStore, blobstore_from_env
from .db import Database
from .demo_insurer import DemoInsurer
from .meta_client import MetaClient, Transport, openai_transport, settings_from_env
from .photo_check import CHECKED_SLOTS
from .store import MAX_BYTES, OUTBOX_BACKOFF_BASE, OUTBOX_BACKOFF_CAP, OUTBOX_MAX_ATTEMPTS, CaptureError, Clock, Store

MAX_BYTES_ANY = max(MAX_BYTES.values())
MAX_REQUEST_BYTES = MAX_BYTES_ANY + 5 * 1024 * 1024  # file + form fields + multipart overhead
MAX_CLIENT_META = 4000
MAX_VERIFY_BYTES = 200 * 1024 * 1024  # a sealed package: every file of one claim
_PRIVATE_HEADERS = {
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "private, no-store",
    "Referrer-Policy": "no-referrer",
}


def content_security_policy(media_origin: str | None) -> str:
    """Photos, the signature and video redirect to presigned links on the file store (R2 in
    production), so its origin is allowed for images and media and nowhere else."""
    media = f" {media_origin}" if media_origin else ""
    return (
        f"default-src 'self'; img-src 'self' data: blob:{media}; media-src 'self' blob:{media}; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src 'self' https://fonts.gstatic.com; connect-src 'self'; frame-ancestors 'none'; "
        "base-uri 'self'; form-action 'self'"
    )


# Only /api/docs: Swagger UI loads its script and stylesheet from jsDelivr and runs one
# inline script; the page holds no claim data. Everything else keeps the CSP above.
DOCS_CSP = (
    "default-src 'self'; img-src 'self' data: https://fastapi.tiangolo.com; "
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; "
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; connect-src 'self'; "
    "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
)
# The SPA shell is a few hundred bytes and must change on every deploy: never cache it. Modal's image
# files carry mtime 0, so without this browsers cached index.html heuristically for years (T54).
SHELL_CACHE = "no-store"
ASSET_CACHE = "public, max-age=31536000, immutable"  # /assets/* names carry a content hash
logger = logging.getLogger(__name__)


class _HashedAssets(StaticFiles):
    """Build files under /assets never change under one name, so browsers may keep them a year."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = ASSET_CACHE
        return response


def contact_email(raw: str | None) -> str | None:
    """CONTACT_EMAIL for the privacy page and the feedback link; None unless it looks like one address."""
    value = (raw or "").strip()
    return value if value.count("@") == 1 and " " not in value and value.index("@") not in (0, len(value) - 1) else None

_VERIFY_PYTHON = '''def verify(body: bytes, header: str, secret: str) -> bool:
    """Check an X-Evidence-Signature header: shape, signature, then freshness."""
    import hashlib
    import hmac
    import time

    try:
        stamp, signature = header.split(",", 1)
        if not stamp.startswith("t=") or not signature.startswith("v1="):
            return False
        sent_at = int(stamp[2:])
        if abs(time.time() - sent_at) > 300:
            return False
        expected = hmac.new(secret.encode(), f"{sent_at}.".encode() + body,
                            hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature[len("v1="):])
    except (ValueError, TypeError, AttributeError):
        return False
'''

_VERIFY_NODE = '''function verify(body, header, secret) {
  const crypto = require("crypto");
  if (typeof header !== "string") return false;
  const comma = header.indexOf(",");
  if (comma < 0) return false;
  const stamp = header.slice(0, comma);
  const signature = header.slice(comma + 1);
  if (!stamp.startsWith("t=") || !signature.startsWith("v1=")) return false;
  const sentAt = Number(stamp.slice(2));
  if (!Number.isFinite(sentAt)) return false;
  if (Math.abs(Date.now() / 1000 - sentAt) > 300) return false;
  try {
    const expected = Buffer.from(
      crypto.createHmac("sha256", secret).update(`${sentAt}.`, "utf8").update(body).digest("hex"),
      "utf8",
    );
    const given = Buffer.from(signature.slice(3), "utf8");
    return expected.length === given.length && crypto.timingSafeEqual(expected, given);
  } catch {
    return false;
  }
}
'''


_LOCAL_CALLBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def callback_host_allowed(url: str, allowed: list[str], own_host: str | None,
                          test_mode: bool = False) -> bool:
    """Whether a callback URL may be used.

    https is required. http is allowed only in test mode for localhost /
    127.0.0.1 or the server's own host (local integration testing). For
    https, the host must be our own host or on the configured list: entries
    are exact host names or "*.example.com" (any subdomain). The URL is
    fixed when the capture request is created, so this is checked once.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    scheme = (parts.scheme or "").lower()
    if scheme == "http":
        if not test_mode:
            return False
        return host in _LOCAL_CALLBACK_HOSTS or bool(own_host and host == own_host.lower())
    if scheme != "https":
        return False
    if own_host and host == own_host.lower():
        return True
    for entry in allowed:
        entry = entry.strip().lower()
        if entry.startswith("*.") and host.endswith(entry[1:]) and host != entry[2:]:
            return True
        if entry and host == entry:
            return True
    return False

def _path_from_env(name: str, *repo_parts: str) -> Path:
    """Env var if set (deployed), else a path inside the repo checkout (local dev)."""
    if name in os.environ:
        return Path(os.environ[name])
    return Path(__file__).resolve().parents[3].joinpath(*repo_parts)


def create_app(
    data_dir: Path | None = None,
    frontend_dist: Path | None = None,
    admin_email: str | None = None,
    admin_password: str | None = None,
    session_secret: str | None = None,
    clock: Clock | None = None,
    database_url: str | None = None,
    blobstore: BlobStore | None = None,
    model_transport: Transport | None = None,
    check_license: bool | None = None,
    callback_hosts: list[str] | None = None,
    rate_limits: dict[str, int] | None = None,
    lane_rule: str | None = None,
    model_provider: str | None = None,
) -> FastAPI:
    data_dir = data_dir or _path_from_env("DATA_DIR", "data")
    database_url = database_url or os.environ.get("DATABASE_URL")
    if not database_url:
        raise RuntimeError("DATABASE_URL is not set; point it at the Postgres database")
    blobstore = blobstore or blobstore_from_env(data_dir)
    csp = content_security_policy(blobstore.origin())
    env_override = dict(os.environ)
    if model_provider is not None:
        env_override["MODEL_PROVIDER"] = model_provider
    meta_settings = settings_from_env(env_override)
    provider = meta_settings["provider"]
    # Only the transport is replaceable in tests; the mapping, timeout and
    # provider always follow the provider.
    if model_transport is None:
        if provider == "fireworks":
            fireworks_key = os.environ.get("FIREWORKS_API_KEY")
            model_transport = (
                openai_transport(
                    fireworks_key, base_url=meta_client.FIREWORKS_BASE_URL,
                    store=False, max_output_tokens=meta_settings["max_output_tokens"],
                )
                if fireworks_key else None
            )
        else:
            api_key = os.environ.get("LLAMA_API_KEY")
            model_transport = (openai_transport(api_key, max_output_tokens=meta_settings["max_output_tokens"])
                               if api_key else None)
    if check_license is None:
        check_license = os.environ.get("PHOTO_CHECK_LICENSE") == "on"
    if lane_rule is None:
        lane_rule = os.environ.get("LANE_RULE", "").strip().lower() or proposal.DEFAULT_LANE_RULE
    if lane_rule not in proposal.LANE_RULES:
        raise RuntimeError(f"LANE_RULE must be one of {', '.join(proposal.LANE_RULES)}")
    db = Database(database_url)
    meta = MetaClient(
        db, model_transport, models=(meta_client.FIREWORKS_MODELS if provider == "fireworks" else None),
        provider=provider, timeout_s=meta_settings["timeout_s"],
        max_attempts=meta_settings["max_attempts"],
        claim_budget_usd=meta_settings["claim_budget_usd"], day_budget_usd=meta_settings["day_budget_usd"],
        clock=clock,
    )
    if admin_password is None:
        admin_password = os.environ.get("ADMIN_PASSWORD")
    if admin_email is None:
        admin_email = os.environ.get("ADMIN_EMAIL")
    if admin_email is not None:
        try:
            admin_email = users_mod.check_email(admin_email)
        except ValueError:
            raise RuntimeError(
                "ADMIN_EMAIL must be an email address (like name@example.com)") from None
    store = Store(data_dir, db, blobstore, clock, meta=meta, check_license=check_license,
                  sam_max_calls=0 if provider == "fireworks" else meta_settings["sam_max_calls"] or 0,
                  lane_rule=lane_rule, admin_email=admin_email, admin_password=admin_password)
    auth = AdminAuth(
        session_secret or os.environ.get("SESSION_SECRET"),
        db,
        store.org_id,
        login_configured=bool(admin_password),
    )
    dist = frontend_dist or _path_from_env("FRONTEND_DIST", "app", "dist")
    if callback_hosts is None:
        callback_hosts = [h for h in os.environ.get("CALLBACK_ALLOWED_HOSTS", "").split(",") if h.strip()]
    limiter = ratelimit.RateLimiter(db, lambda: store.clock.now().timestamp(), rate_limits)
    store.limiter = limiter  # the sweeper prunes old counters

    @asynccontextmanager
    async def _lifespan(_: FastAPI):
        yield
        db.close()

    app = FastAPI(
        title="Evidence capture", docs_url="/api/docs", redoc_url=None,
        openapi_url="/api/openapi.json", lifespan=_lifespan,
    )

    @app.exception_handler(CaptureError)
    async def capture_error(_: Request, exc: CaptureError):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        path = request.url.path
        bucket = ratelimit.bucket_for(request.method, path)
        if bucket is not None:
            ip = request.client.host if request.client else "unknown"
            wait = await run_in_threadpool(limiter.hit, bucket, ip)
            if wait is not None:
                return JSONResponse(
                    {"detail": "Too many requests. Please wait a moment and try again."},
                    status_code=429, headers={"Retry-After": str(wait)},
                )
        if request.method == "POST" and (path.startswith("/api/c/") or path == "/api/verify"):
            # Refuse oversized bodies from their declared size, before anything is parsed.
            limit = MAX_VERIFY_BYTES if path == "/api/verify" else MAX_REQUEST_BYTES
            length = request.headers.get("content-length")
            if length is None or not length.isdigit():
                return JSONResponse({"detail": "Content-Length required"}, status_code=411)
            if int(length) > limit:
                return JSONResponse({"detail": "Upload too large"}, status_code=413)
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        docs = path == "/api/docs" or path.startswith("/api/docs/")
        response.headers["Content-Security-Policy"] = DOCS_CSP if docs else csp
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
        return response

    # --- adjuster side (login required) ---

    @app.post("/api/admin/login")
    def login(request: Request, response: Response, body: dict | None = Body(None)):
        body = body if isinstance(body, dict) else {}
        me = auth.login(
            request, response, str(body.get("email") or ""), str(body.get("password") or ""))
        return {"user": me}

    admin = APIRouter(prefix="/api/admin", dependencies=[Depends(auth.require)])

    @admin.post("/logout")
    def logout(response: Response):
        auth.logout(response)
        return {"ok": True}

    @admin.get("/me")
    def me(user: dict = Depends(auth.require)):
        return user

    @admin.post("/me/password")
    def change_my_password(
        request: Request, response: Response, body: dict | None = Body(None),
        user: dict = Depends(auth.require),
    ):
        body = body if isinstance(body, dict) else {}
        updated, session_version = auth.change_password(
            request, user["id"], str(body.get("current") or ""), str(body.get("new") or ""))
        auth.set_session(response, updated["id"], session_version)
        return {"user": updated}

    @admin.get("/users")
    def list_users(_: dict = Depends(auth.require_supervisor)):
        return store.list_users()

    @admin.post("/users", status_code=201)
    def create_user(body: dict | None = Body(None), me: dict = Depends(auth.require_supervisor)):
        body = body if isinstance(body, dict) else {}
        return store.create_user(
            str(body.get("email") or ""), str(body.get("name") or ""),
            str(body.get("role") or ""), created_by=me["id"],
        )

    @admin.patch("/users/{user_id}")
    def update_user(
        user_id: str, body: dict | None = Body(None), me: dict = Depends(auth.require_supervisor)
    ):
        return store.update_user(user_id, body if isinstance(body, dict) else {}, me)

    @admin.post("/users/{user_id}/reset-password")
    def reset_user_password(user_id: str, _: dict = Depends(auth.require_supervisor)):
        return store.reset_user_password(user_id)

    @admin.get("/policies")
    def list_policies():
        return store.list_policies()

    _TRIAL_FIELDS = {
        "holder.name": "holder_name",
        "drivers[0].name": "holder_name",
        "state": "state",
        "vehicles[0].year": "year",
        "vehicles[0].make": "make",
        "vehicles[0].model": "model",
        "vehicles[0].plate": "plate",
        "vehicles[0].vin": "vin",
    }

    _POLICY_NUMBER = re.compile(r"^[A-Z0-9\-_./]{1,40}$")

    def _deductible(raw, field: str, default: int) -> int:
        """A console deductible: an integer 0-10000 (digits as text accepted), the default when missing."""
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return default
        value = int(raw.strip()) if isinstance(raw, str) and raw.strip().isdigit() else raw
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
            raise CaptureError(f"{field}: must be a whole number of dollars between 0 and 10000", 400)
        return value

    @admin.post("/claims")
    def create_claim(body: dict | None = Body(None), user: dict = Depends(auth.require)):
        """Open a claim on a seeded policy, or freeze the tester's own car as a trial policy."""
        data = body if isinstance(body, dict) else {}
        raw_policy = data.get("policy_number")
        raw_trial = data.get("trial")
        has_policy = isinstance(raw_policy, str) and raw_policy.strip() != ""
        has_trial = isinstance(raw_trial, dict) and bool(raw_trial)
        if has_policy == has_trial:
            raise CaptureError("Send either policy_number or trial.", 400)
        if has_trial:
            raw_number = raw_trial.get("policy_number")
            policy_no = raw_number.strip().upper() if isinstance(raw_number, str) else ""
            if not policy_no:
                policy_no = "POL-" + secrets.token_hex(3).upper()  # T56: made up for a test
            elif not _POLICY_NUMBER.match(policy_no):
                raise CaptureError("policy_number: use 1-40 letters, digits, - _ . or /", 400)
            coverages = {
                "collision": {"deductible": _deductible(raw_trial.get("collision_deductible"), "collision_deductible", 500)},
                "comprehensive": {"deductible": _deductible(
                    raw_trial.get("comprehensive_deductible"), "comprehensive_deductible", 250)},
                "liability": policies.TRIAL_COVERAGES["liability"],
            }
            vehicle_in = raw_trial.get("vehicle")
            vehicle_in = vehicle_in if isinstance(vehicle_in, dict) else {}
            utc_today = store.clock.now().date()
            effective = utc_today - timedelta(days=30)
            core = {
                "state": raw_trial.get("state"),
                "effective": effective.isoformat(),
                "expires": (effective + timedelta(days=365)).isoformat(),
                "holder": {"name": raw_trial.get("holder_name")},
                "drivers": [{"name": raw_trial.get("holder_name")}],
                "vehicles": [{
                    "vin": vehicle_in.get("vin"),
                    "plate": vehicle_in.get("plate"),
                    "year": vehicle_in.get("year"),
                    "make": vehicle_in.get("make"),
                    "model": vehicle_in.get("model"),
                }],
                "coverages": coverages,
            }
            try:
                validated = policies.validate_policy(core, utc_today, "")
            except CaptureError as e:
                message = str(e)
                path, _, reason = message.partition(": ")
                field = _TRIAL_FIELDS.get(path, path.rsplit(".", 1)[-1].rstrip("]"))
                raise CaptureError(f"{field}: {reason}" if reason else message, 400) from e
            validated["source"] = "trial"
            validated["entered_by"] = (user.get("name") or "").strip() or user.get("email")
            created = store.open_claim(policy_no, policy_data=validated)
            return {"id": created["id"], "token": created["token"], "policy_number": policy_no}
        policy_no = raw_policy.strip().upper()
        created = store.open_claim(policy_no)
        return {"id": created["id"], "token": created["token"], "policy_number": policy_no}

    @admin.get("/claims")
    def list_claims():
        return store.list_claims()

    @admin.get("/claims/{claim_id}")
    def claim_detail(claim_id: str):
        return store.claim_detail(claim_id)

    @admin.post("/claims/{claim_id}/deliver")
    def deliver(claim_id: str):
        store.deliver(claim_id, force=True)  # the adjuster's Resend: ignores backoff and dead letter
        return {"ok": True}

    @admin.post("/sweep")
    def sweep(_: dict = Depends(auth.require_supervisor)):
        """Finish background work a lost container left behind (store.sweep). The
        Modal cron calls store.sweep() in-process; this route is for an operator."""
        return store.sweep()

    @admin.post("/claims/{claim_id}/decision")
    def decide_claim(
        claim_id: str, body: dict | None = Body(None), user: dict = Depends(auth.require)
    ):
        return store.decide(claim_id, body or {}, user)

    @admin.post("/claims/{claim_id}/delete-media")
    def delete_claim_media(
        claim_id: str, body: dict | None = Body(None), user: dict = Depends(auth.require_supervisor)
    ):
        data = body if isinstance(body, dict) else {}
        return store.delete_claim_media(claim_id, data.get("reason", ""), user)

    @admin.post("/claims/{claim_id}/review")
    def review_claim(
        claim_id: str, body: dict | None = Body(None), user: dict = Depends(auth.require)
    ):
        return store.add_review(claim_id, body or {}, user)

    @admin.post("/claims/{claim_id}/diaries", status_code=201)
    def set_diary(
        claim_id: str, body: dict | None = Body(None), user: dict = Depends(auth.require)
    ):
        return store.set_diary(claim_id, body or {}, user)

    @admin.post("/diaries/{diary_id}/done")
    def complete_diary(diary_id: str, user: dict = Depends(auth.require)):
        return store.complete_diary(diary_id, user)

    @admin.get("/settings")
    def get_settings():
        return store.settings()

    @admin.put("/settings")
    def put_settings(
        body: dict | None = Body(None), me: dict = Depends(auth.require_supervisor)
    ):
        return store.update_settings(body if isinstance(body, dict) else {}, me)

    @admin.get("/queue")
    def queue():
        return store.queue()

    @admin.get("/health")
    def admin_health():
        return store.health()

    @admin.get("/feedback")
    def admin_feedback():
        return store.feedback()

    @admin.get("/claims/{claim_id}/package")
    def claim_package(claim_id: str):
        return Response(
            store.package(claim_id),
            media_type="application/zip",
            headers={"Content-Disposition": f'attachment; filename="{claim_id}-sealed.zip"'},
        )

    @admin.get("/claims/{claim_id}/report")
    def claim_report(claim_id: str):
        return Response(
            store.report_pdf_by_claim(claim_id),
            media_type="application/pdf",
            headers={"Content-Disposition": 'inline; filename="verified-evidence-report.pdf"'},
        )

    @admin.get("/items/{item_id}/file")
    def item_file(item_id: str):
        target = store.item_file(item_id)
        if target.url is not None:
            return RedirectResponse(url=target.url, status_code=307, headers={"Cache-Control": "no-store"})
        assert target.path is not None
        return FileResponse(target.path, media_type=target.mime)

    # --- insurer integration kit (T48, supervisors only) ---

    @admin.get("/api-keys")
    def list_api_keys(_: dict = Depends(auth.require_supervisor)):
        return store.list_api_keys()

    @admin.post("/api-keys", status_code=201)
    def create_api_key(
        body: dict | None = Body(None), me: dict = Depends(auth.require_supervisor)
    ):
        body = body if isinstance(body, dict) else {}
        return store.create_api_key(body.get("name"), body.get("mode"), me)

    @admin.post("/api-keys/{key_id}/revoke")
    def revoke_api_key(key_id: str, _: dict = Depends(auth.require_supervisor)):
        return store.revoke_api_key(key_id)

    @admin.post("/api-keys/{key_id}/rotate", status_code=201)
    def rotate_api_key(
        key_id: str, body: dict | None = Body(None), me: dict = Depends(auth.require_supervisor)
    ):
        body = body if isinstance(body, dict) else {}
        return store.rotate_api_key(key_id, body.get("old_key_expires_in", "1h"), me)

    @admin.get("/webhook-secret")
    def webhook_secret(_: dict = Depends(auth.require_supervisor)):
        return store.get_webhook_secret()

    @admin.post("/webhook-secret/rotate")
    def rotate_webhook_secret(_: dict = Depends(auth.require_supervisor)):
        return store.rotate_webhook_secret()

    @admin.post("/webhooks/test")
    def test_webhook(
        request: Request,
        body: dict | None = Body(None),
        _: dict = Depends(auth.require_supervisor),
    ):
        """Send one signed `ping` to the insurer's URL and report the answer.

        Never queued: nothing goes to the outbox.
        """
        url = (body or {}).get("url") if isinstance(body, dict) else None
        if not isinstance(url, str) or not (
            url.startswith("https://") or url.startswith("http://")
        ):
            raise CaptureError("callback_url must be an https:// URL", 400)
        # A test ping is a test-mode use: http to localhost / our own host is allowed.
        if not callback_host_allowed(url, callback_hosts, request.url.hostname, test_mode=True):
            raise CaptureError(
                "callback_url host is not on this deployment's allowed list (CALLBACK_ALLOWED_HOSTS)",
                400,
            )
        with store.db.tx() as conn:
            cred = conn.execute(
                "SELECT webhook_secret FROM integration_credentials WHERE org_id=%s",
                (store.org_id,),
            ).fetchone()
            org = conn.execute(
                "SELECT name FROM orgs WHERE id=%s", (store.org_id,)).fetchone()
        now = store.clock.now()
        payload = policies.canonical(report.ping(
            "evt_" + secrets.token_urlsafe(16), now.isoformat(),
            org["name"] if org else "")).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Evidence-Signature": report.sign(
                payload, cred["webhook_secret"].encode(), int(now.timestamp())),
        }
        started = time.perf_counter()
        try:
            status, _ = store.http("POST", url, payload, headers)
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}
        ms = int((time.perf_counter() - started) * 1000)
        if 200 <= status < 300:
            return {"ok": True, "status_code": status, "ms": ms}
        return {"ok": False, "status_code": status, "ms": ms}

    app.include_router(admin)

    # --- insurer integration API (T16): Bearer [REDACTED] key, no login ---

    _INSURER_NUMBER = re.compile(r"^[A-Za-z0-9\-_./]{1,64}$")

    def _api_auth(request: Request) -> dict:
        """The key behind a Bearer header: {"org_id", "key_id", "mode"}. 401 without one."""
        with store.db.tx() as conn:
            authn = integration.check_api_key(
                conn, request.headers.get("authorization"), store.clock.now())
        if authn is None:
            raise HTTPException(401, "A valid API key is required")
        return authn

    def _case_response(case_id: str, base: str) -> dict:
        d = store.claim_detail(case_id)
        return {
            "case_id": d["id"],
            "insurer_claim_number": d["insurer_claim_number"],
            "status": d["status"],
            "test_mode": d["test_mode"],
            "capture_url": f"{base}/c/{d['token']}",
            "expires_at": d["expires_at"],
            "report_url": f"{base}{d['report']['url']}" if d["report"] else None,
        }

    v1 = APIRouter(prefix="/api/v1")

    @v1.post("/capture-requests")
    def create_capture_request(request: Request, body: dict = Body(...)):
        authn = _api_auth(request)
        org_id, test_mode = authn["org_id"], authn["mode"] == "test"
        base = str(request.base_url).rstrip("/")
        if not isinstance(body, dict):
            raise CaptureError("Body must be an object")
        number = body.get("insurer_claim_number")
        policy_no = body.get("policy_number")
        callback = body.get("callback_url")
        if not isinstance(number, str) or not _INSURER_NUMBER.match(number):
            raise CaptureError(
                "insurer_claim_number is required (1-64 chars: letters, digits, - _ . /)"
            )
        if not isinstance(policy_no, str) or not policy_no.strip():
            raise CaptureError("policy_number is required")
        if not isinstance(callback, str) or not (
            callback.startswith("https://") or callback.startswith("http://")
        ):
            raise CaptureError("callback_url must be an https:// URL")
        claimant = body.get("claimant")
        loss = body.get("loss")
        opened_data = {
            "claimant": claimant if isinstance(claimant, dict) else {},
            "loss": loss if isinstance(loss, dict) else {},
        }
        with store.db.tx() as conn:
            existing = conn.execute(
                "SELECT id FROM claims WHERE org_id=%s AND test_mode=%s AND insurer_claim_number=%s",
                (org_id, test_mode, number),
            ).fetchone()
        if existing:  # idempotent within the mode: the stored callback stays, the new one is ignored
            return JSONResponse(_case_response(existing["id"], base), 200)
        policy_in = body.get("policy")
        validated_policy = None
        if policy_in is not None:
            if not isinstance(policy_in, dict):
                raise CaptureError("policy: must be an object", 400)
            validated_policy = policies.validate_policy(policy_in, store.clock.now().date(), "policy.")
            validated_policy["source"] = "insurer"
        if not callback_host_allowed(callback, callback_hosts, request.url.hostname,
                                     test_mode=test_mode):
            if callback.startswith("http://"):
                raise CaptureError(
                    "callback_url must be an https:// URL "
                    "(http:// is only allowed for localhost or this host in test mode)", 400)
            raise CaptureError(
                "callback_url host is not on this deployment's allowed list (CALLBACK_ALLOWED_HOSTS)"
            )
        created = store.open_claim(
            policy_no,
            actor="system",
            insurer_claim_number=number,
            callback_url=callback,
            opened_data=opened_data,
            test_mode=test_mode,
            policy_data=validated_policy,
        )
        return JSONResponse(_case_response(created["id"], base), 201)

    @v1.get("/capture-requests/{case_id}")
    def get_capture_request(case_id: str, request: Request):
        authn = _api_auth(request)
        base = str(request.base_url).rstrip("/")
        with store.db.tx() as conn:
            row = conn.execute(
                "SELECT id, test_mode FROM claims WHERE id=%s AND org_id=%s",
                (case_id, authn["org_id"]),
            ).fetchone()
        # Test and live never see each other's cases.
        if not row or bool(row["test_mode"]) != (authn["mode"] == "test"):
            raise CaptureError("Case not found", 404)
        return _case_response(case_id, base)

    app.include_router(v1)

    # --- mock claims system (T29): the insurer's side, login like the console ---

    demo = DemoInsurer(db, store.clock, store.list_policies, store.policy_data)

    async def _demo_webhook(request: Request):
        raw = await request.body()
        return demo.handle_webhook(raw, request.headers.get("X-Evidence-Signature"))

    app.add_api_route(
        "/api/demo-insurer/webhook", _demo_webhook, methods=["POST"], include_in_schema=False
    )

    insured = APIRouter(prefix="/api/demo-insurer", dependencies=[Depends(auth.require)])

    @insured.get("/desktop")
    def demo_desktop():
        return demo.desktop()

    @insured.get("/policies")
    def demo_policies():
        return demo.policies()

    @insured.post("/claims")
    def demo_create_claim(body: dict = Body(...)):
        if not isinstance(body, dict):
            raise CaptureError("Body must be an object")
        return demo.create_claim(
            body.get("policy_number"),
            body.get("loss_date"),
            body.get("loss_cause"),
            body.get("description"),
            body.get("claimant_phone"),
        )

    @insured.get("/claims/{claim_number}")
    def demo_claim_view(claim_number: str):
        return demo.claim_view(claim_number)

    @insured.post("/claims/{claim_number}/services/verified-capture")
    def demo_request_capture(claim_number: str, request: Request):
        return demo.request_capture(claim_number, str(request.base_url).rstrip("/"))

    @insured.post("/claims/{claim_number}/notes")
    def demo_add_note(claim_number: str, body: dict = Body(...)):
        if not isinstance(body, dict):
            raise CaptureError("Body must be an object")
        return demo.add_note(claim_number, body.get("subject"), body.get("body"))

    @insured.post("/activities/{activity_id}/complete")
    def demo_complete_activity(activity_id: str):
        return demo.complete_activity(activity_id)

    @insured.post("/exposures/{exposure_id}/reserve")
    def demo_set_reserve(exposure_id: str, body: dict = Body(...)):
        if not isinstance(body, dict):
            raise CaptureError("Body must be an object")
        return demo.set_reserve(exposure_id, body.get("amount"))

    app.include_router(insured)

    # --- customer side (capture link) ---

    @app.get("/api/c/{token}")
    def session_state(token: str):
        return store.session_state(token)

    @app.put("/api/c/{token}/draft")
    def save_draft(token: str, body: dict = Body(...)):
        return store.save_draft(token, body.get("answers") or {})

    @app.post("/api/c/{token}/classify")
    def classify_text(token: str, body: dict | None = Body(None)):
        """Suggest a loss type from the claimant's own words; saves nothing."""
        raw = body.get("text") if isinstance(body, dict) else None
        text = raw.strip() if isinstance(raw, str) else ""
        if not isinstance(raw, str) or not 8 <= len(text) <= 1000:
            raise CaptureError("Tell us what happened in 8 to 1000 characters", 400)
        return store.classify(token, text)

    @app.post("/api/c/{token}/session")
    def start_session(token: str):
        return store.start_session(token)

    @app.post("/api/c/{token}/nonce")
    def issue_nonce(token: str, body: dict = Body(...)):
        return store.issue_nonce(token, str(body.get("session_id") or ""), str(body.get("slot_id") or ""))

    @app.post("/api/c/{token}/items")
    def add_item(
        token: str,
        background_tasks: BackgroundTasks,
        step_id: str = Form(...),
        session_id: str = Form(""),
        nonce: str = Form(""),
        client_meta: str = Form("{}"),
        file: UploadFile = File(...),
        frame_before: UploadFile | None = File(None),
        frame_after: UploadFile | None = File(None),
    ):
        if len(client_meta) > MAX_CLIENT_META:
            raise CaptureError("Device metadata too large", 413)
        try:
            meta = json.loads(client_meta)
        except json.JSONDecodeError:
            meta = {"unparsed": client_meta[:500]}
        store.check_link(token)  # before reading the file
        data = file.file.read(MAX_BYTES_ANY + 1)
        frame_before_bytes = frame_before.file.read() if frame_before is not None else None
        frame_after_bytes = frame_after.file.read() if frame_after is not None else None
        item = store.add_item(token, step_id, data, file.content_type or "", meta, session_id, nonce,
                              frame_before=frame_before_bytes, frame_after=frame_after_bytes)
        if step_id in CHECKED_SLOTS:
            background_tasks.add_task(store.run_photo_check, item["id"])
        return item

    @app.get("/api/c/{token}/items/{item_id}/file")
    def claimant_item_file(token: str, item_id: str):
        target = store.claimant_item_file(token, item_id)
        if target.url is not None:
            return RedirectResponse(url=target.url, status_code=307, headers={"Cache-Control": "no-store"})
        assert target.path is not None
        return FileResponse(target.path, media_type=target.mime)

    @app.delete("/api/c/{token}/items/{item_id}")
    def remove_item(token: str, item_id: str, session_id: str = ""):
        store.remove_item(token, item_id, session_id)
        return {"ok": True}

    @app.post("/api/c/{token}/submit")
    async def submit(token: str, request: Request, background_tasks: BackgroundTasks):
        try:
            body = await request.json()
        except ValueError:
            raise CaptureError("Body must be JSON")
        if not isinstance(body, dict):
            raise CaptureError("Body must be an object")
        # Off the event loop: the store blocks on its Postgres connection.
        result = await run_in_threadpool(store.submit, token, body.get("answers"))
        background_tasks.add_task(store.run_damage, result["claim_id"])
        background_tasks.add_task(store.run_consistency, result["claim_id"])
        return result

    @app.post("/api/verify")
    def verify(file: UploadFile = File(...)):
        # The body was capped by its Content-Length in the middleware; Starlette spools
        # the upload to a temp file past 1 MB, and verify reads it entry by entry.
        file.file.seek(0, os.SEEK_END)
        if file.file.tell() > MAX_VERIFY_BYTES:
            raise CaptureError("Upload too large", 413)
        file.file.seek(0)
        return store.verify_package(file.file)

    @app.get("/api/verify/public-key")
    def public_key():
        return {"algorithm": "Ed25519", "public_key": store.sealer.public_key_b64, "key_id": store.sealer.key_id}

    # --- the verified evidence report (secret link, no login) ---
    # Registered here, well before the SPA catch-all below.

    @app.get("/api/r/{token}")
    def report_pdf(token: str, version: str | None = None):
        data, _claim_id = store.report_by_token(token, version)
        return Response(
            data,
            media_type="application/pdf",
            headers=_PRIVATE_HEADERS
            | {"Content-Disposition": 'inline; filename="verified-evidence-report.pdf"'},
        )

    @app.get("/api/r/{token}/package")
    def report_package(token: str):
        data, claim_id = store.package_by_token(token)
        return Response(
            data,
            media_type="application/zip",
            headers=_PRIVATE_HEADERS
            | {"Content-Disposition": f'attachment; filename="{claim_id}-sealed.zip"'},
        )

    contact = contact_email(os.environ.get("CONTACT_EMAIL"))

    @app.get("/api/contact")
    def public_contact():
        """The privacy page's contact address and the model actually reading photos (T57)."""
        return {"email": contact, "model": meta_client.PROVIDERS[meta.provider]}

    @app.get("/api/health")
    def health():
        return {"ok": True}

    # --- developers (T48, public like Stripe's docs: no secrets here) ---

    @app.get("/api/developers")
    def developers():
        """What an insurer's IT needs: the event shapes, the verify samples, the limits."""
        now = store.clock.now()
        with store.db.tx() as conn:
            org = conn.execute(
                "SELECT name FROM orgs WHERE id=%s", (store.org_id,)).fetchone()
        org_name = org["name"] if org and org["name"] else "Demo Mutual Insurance"
        stamp = now.isoformat()
        sample = {
            "generated_at": stamp,
            "generated_date": now.date().isoformat(),
            "case_id": "CLM-2026-0101",
            "insurer_claim_number": "DM-2026-0101",
            "policy_number": "DEMO-AUTO-1001",
            "vehicle": {"vin": "1HGCV1F39LA526018"},
            "recommendation": {
                "lane": "fast_track",
                "label": "Fast track",
                "headline": "Verified capture complete: 9 items sealed.",
                "reasons": ["All checks passed."],
                "next_step": "Review the verified evidence report and record your decision.",
            },
            "flags": [],
            "cost": {
                "status": "complete",
                "likely": [800, 1200],
                "full": [800, 1200],
                "basis": "Public ballpark figures, not an estimate.",
                "suggested_reserve": 1000,
                "deductible": 500,
            },
            "seal": {
                "sealed_at": stamp,
                "key_id": "dev",
                "manifest_sha256": "0" * 64,
                "items": 9,
            },
            "report_version": report.VERSION,
            "test_mode": False,
        }
        sample_ready = report.package(
            sample, "sample-evidence-ready-01",
            "1" * 64, "/api/r/sample", "/api/r/sample/package")
        sample_decision = report.decision_recorded(
            {
                "case_id": sample["case_id"],
                "insurer_claim_number": sample["insurer_claim_number"],
                "policy_number": sample["policy_number"],
                "vin": "1HGCV1F39LA526018",
            },
            "sample-decision-recorded-01", stamp,
            {
                "action": "approve",
                "label": "Approved",
                "reason": None,
                "by": "A. Adjuster",
                "lane_before": "fast_track",
                "lane_after": "fast_track",
                "reserve_before": 1000,
                "reserve_after": 1000,
                "slots": [],
            },
            [{
                "name": "reviewed-evidence-report.pdf",
                "document_type": "evidence_report",
                "mime": "application/pdf",
                "url": "/api/r/sample",
                "sha256": "2" * 64,
            }],
            [{
                "topic": "general",
                "subject": "Adjuster decision",
                "body": ("Decision by A. Adjuster: Approved. Lane: Fast track."
                         " Suggested initial reserve $1,000. See verified-evidence-report.pdf."),
            }],
            [{
                "subject": "No further action",
                "description": "The claim was approved on the fast track.",
                "priority": "normal",
                "due_date": report.business_days_after(now.date(), 2).isoformat(),
                "url": "/claims/CLM-2026-0101",
            }],
            complete_activities=["Review verified evidence"],
            test_mode=False,
        )
        return {
            "events": {
                "evidence.ready": sample_ready,
                "decision.recorded": sample_decision,
                "ping": report.ping("evt_sample_01", stamp, org_name),
            },
            "verify": {"python": _VERIFY_PYTHON, "node": _VERIFY_NODE},
            "limits": {
                "api_per_minute": ratelimit.LIMITS["api"],
                "retries": OUTBOX_MAX_ATTEMPTS,
                "first_retry_seconds": int(OUTBOX_BACKOFF_BASE.total_seconds()),
                "max_retry_wait_seconds": int(OUTBOX_BACKOFF_CAP.total_seconds()),
            },
        }

    # --- frontend (single-page app) ---

    if dist.exists():
        app.mount("/assets", _HashedAssets(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            candidate = (dist / path).resolve()
            if path and candidate.is_file() and dist.resolve() in candidate.parents:
                return FileResponse(candidate, headers={"Cache-Control": SHELL_CACHE})
            return FileResponse(dist / "index.html", headers={"Cache-Control": SHELL_CACHE})

    app.state.store = store
    app.state.demo = demo
    return app


_app: FastAPI | None = None


def __getattr__(name: str):
    """`evidence_server.main:app` for uvicorn, built on first use: importing the
    package never connects to the database (Modal calls create_app() itself)."""
    global _app
    if name == "app":
        if _app is None:
            _app = create_app()
        return _app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    import uvicorn

    uvicorn.run(
        "evidence_server.main:app",
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8000)),
        proxy_headers=True,
        forwarded_allow_ips=os.environ.get("FORWARDED_ALLOW_IPS", "127.0.0.1"),
    )
