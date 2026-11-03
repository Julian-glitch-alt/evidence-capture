"""Track 3: public surface hardening. Per-IP rate limits, a streamed and capped
/api/verify, the callback URL allowlist, the docs CSP, and no database at import."""

import io
import json
import os
import subprocess
import sys
import time
import tracemalloc
import zipfile

from evidence_server import ratelimit, seal
from evidence_server.main import callback_host_allowed, content_security_policy
from test_damage_flow import captured_claim, db_rows, send
from test_integration_api import auth, order
from test_loss_notice import open_claim


def limit(client, bucket, n):
    client.app.state.store.limiter.limits[bucket] = n


# --- rate limits ---


def test_capture_link_is_rate_limited_per_window(client, clock):
    token = open_claim(client)["token"]
    limit(client, "capture", 3)
    assert [client.get(f"/api/c/{token}").status_code for _ in range(3)] == [200, 200, 200]
    r = client.get(f"/api/c/{token}")
    assert r.status_code == 429 and 1 <= int(r.headers["Retry-After"]) <= 60
    assert "Too many requests" in r.json()["detail"]
    clock.advance(seconds=60)
    assert client.get(f"/api/c/{token}").status_code == 200


def test_each_public_bucket_has_its_own_limit(client):
    for bucket in ratelimit.LIMITS:
        limit(client, bucket, 1)
    assert client.post("/api/verify", files={"file": ("p.zip", b"nope", "application/zip")}).status_code == 200
    assert client.post("/api/verify", files={"file": ("p.zip", b"nope", "application/zip")}).status_code == 429
    assert order(client).status_code == 201
    assert order(client).status_code == 429
    assert client.post("/api/demo-insurer/webhook", content=b"{}").status_code == 401  # bad signature
    assert client.post("/api/demo-insurer/webhook", content=b"{}").status_code == 429
    # Routes without a limit are untouched.
    assert all(client.get("/api/health").status_code == 200 for _ in range(5))
    assert all(client.get("/api/verify/public-key").status_code == 200 for _ in range(5))
    assert all(client.get("/api/admin/queue").status_code == 200 for _ in range(5))


def test_limits_are_per_ip_and_shared_through_the_database(client, clock):
    store = client.app.state.store
    a = ratelimit.RateLimiter(store.db, lambda: clock.now().timestamp(), {"capture": 2})
    b = ratelimit.RateLimiter(store.db, lambda: clock.now().timestamp(), {"capture": 2})  # another container
    assert [a.hit("capture", "1.1.1.1"), b.hit("capture", "1.1.1.1")] == [None, None]
    assert a.hit("capture", "1.1.1.1") is not None and b.hit("capture", "1.1.1.1") is not None
    assert a.hit("capture", "2.2.2.2") is None


def test_limiter_lets_requests_through_when_the_database_fails(clock):
    class Broken:
        def tx(self):
            raise ConnectionError("database down")
    assert ratelimit.RateLimiter(Broken(), lambda: 0.0, {"capture": 1}).hit("capture", "1.1.1.1") is None


def test_sweeper_prunes_old_counters(client, clock):
    token = open_claim(client)["token"]
    client.get(f"/api/c/{token}")
    assert len(db_rows(client, "SELECT * FROM rate_limits")) == 1
    clock.advance(minutes=30)
    assert client.post("/api/admin/sweep").json()["rate_limit_rows_pruned"] == 0
    clock.advance(minutes=31)
    assert client.post("/api/admin/sweep").json()["rate_limit_rows_pruned"] == 1


def test_bucket_for():
    assert ratelimit.bucket_for("PUT", "/api/c/abc/draft") == "capture"
    assert ratelimit.bucket_for("POST", "/api/verify") == "verify"
    assert ratelimit.bucket_for("GET", "/api/verify/public-key") is None
    assert ratelimit.bucket_for("GET", "/api/v1/capture-requests/x") == "api"
    assert ratelimit.bucket_for("POST", "/api/demo-insurer/webhook") == "webhook"
    assert ratelimit.bucket_for("POST", "/api/admin/login") is None  # auth.py has its own limit


# --- /api/verify ---


def test_verify_still_checks_a_real_package(client, model, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    package = client.get(f"/api/admin/claims/{claim['id']}/package").content
    r = client.post("/api/verify", files={"file": ("p.zip", package, "application/zip")})
    assert r.json()["valid"] is True
    # Change one evidence file: the streamed hash catches it.
    src = zipfile.ZipFile(io.BytesIO(package))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            data = src.read(info)
            if info.filename.startswith("files/") and "closeup" in info.filename:
                data = data + b"edited"
            z.writestr(info.filename, data)
    r = client.post("/api/verify", files={"file": ("p.zip", out.getvalue(), "application/zip")})
    body = r.json()
    assert body["valid"] is False and any("was changed" in p for p in body["problems"])


def test_verify_refuses_an_oversized_body_before_reading_it(client):
    r = client.post("/api/verify", content=b"x", headers={"Content-Length": str(300 * 1024 * 1024),
                                                           "Content-Type": "application/zip"})
    assert r.status_code == 413


def big_package(size: int) -> bytes:
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as z:
        z.writestr("manifest.json", json.dumps({"items": [{"file": "files/video.mp4", "sha256": "0" * 64}]}))
        z.writestr("signature.txt", "AAAA")
        z.writestr("files/video.mp4", os.urandom(1024) * (size // 1024))
    return out.getvalue()


def test_verify_memory_stays_small_for_a_large_package(tmp_path, client):
    path = tmp_path / "package.zip"
    path.write_bytes(big_package(40 * 1024 * 1024))
    sealer = client.app.state.store.sealer
    tracemalloc.start()
    with path.open("rb") as f:
        result = sealer.verify_package(f)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert result["valid"] is False and any("video.mp4 was changed" in p for p in result["problems"])
    assert peak < 8 * 1024 * 1024, peak  # was > 80 MB: the whole upload, then every file, in memory


def test_corrupt_deflate_stream_is_invalid_not_500(client):
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("files/a.jpg", os.urandom(4096))
    data = bytearray(out.getvalue())
    data[60:80] = b"\x00" * 20  # break the compressed bytes, keep the headers
    r = client.post("/api/verify", files={"file": ("p.zip", bytes(data), "application/zip")})
    assert r.status_code == 200 and r.json()["valid"] is False


# --- callback URL allowlist (SSRF) ---


def test_callback_must_be_on_the_allowed_list(client):
    r = order(client, number="DM-2026-0601", callback="https://169.254.169.254/latest/meta-data")
    assert r.status_code == 400 and "CALLBACK_ALLOWED_HOSTS" in r.json()["detail"]
    assert order(client, number="DM-2026-0602", callback="https://insurer.example@evil.example/h").status_code == 400
    assert order(client, number="DM-2026-0603", callback="http://localhost:8080/h").status_code == 400
    assert order(client, number="DM-2026-0604", callback="https://insurer.example/hook").status_code == 201
    # The app's own host is always allowed (the mock claims system lives there).
    assert order(client, number="DM-2026-0605", callback="https://testserver/api/demo-insurer/webhook").status_code == 201
    assert db_rows(client, "SELECT COUNT(*) AS n FROM claims")[0]["n"] == 2


def test_callback_host_rules():
    allowed = ["insurer.example", "*.carrier.example"]
    assert callback_host_allowed("https://INSURER.example/x", allowed, None)
    assert callback_host_allowed("https://api.carrier.example/x", allowed, None)
    assert not callback_host_allowed("https://carrier.example/x", allowed, None)
    assert not callback_host_allowed("https://evilcarrier.example/x", allowed, None)
    assert not callback_host_allowed("https://insurer.example.evil.example/x", allowed, None)
    assert callback_host_allowed("https://own.example/x", [], "own.example")
    assert not callback_host_allowed("https://[::1/x", allowed, None)
    assert not callback_host_allowed("https:///x", allowed, None)


def test_api_still_needs_its_key(client):
    r = client.post("/api/v1/capture-requests", json={})
    assert r.status_code == 401
    assert client.post("/api/v1/capture-requests", json={}, headers=auth(client)).status_code == 400


# --- docs CSP ---


def test_docs_page_may_load_swagger_and_nothing_else_does(client):
    r = client.get("/api/docs")
    assert r.status_code == 200 and "swagger-ui" in r.text
    assert "https://cdn.jsdelivr.net" in r.headers["Content-Security-Policy"]
    assert "cdn.jsdelivr.net" in r.text
    for path in ("/api/health", "/api/openapi.json", "/api/verify/public-key"):
        assert "jsdelivr" not in client.get(path).headers["Content-Security-Policy"]
    assert client.get("/redoc").status_code == 404  # was a blank page under the CSP


def directive(csp, name):
    return next(d.split()[1:] for d in csp.split(";") if d.split() and d.split()[0] == name)


def test_console_may_load_photos_and_video_from_the_file_store():
    # Media routes redirect to presigned R2 links; a CSP of 'self' alone blanked every
    # photo, the signature and the video on the live claim page (2 Nov).
    csp = content_security_policy("https://acct123.r2.cloudflarestorage.com")
    for name in ("img-src", "media-src"):
        assert "https://acct123.r2.cloudflarestorage.com" in directive(csp, name)
    for name in ("default-src", "connect-src", "script-src"):
        if any(d.split()[:1] == [name] for d in csp.split(";")):
            assert "https://acct123.r2.cloudflarestorage.com" not in directive(csp, name)


def test_with_local_files_media_stays_on_our_own_origin(client):
    csp = client.get("/api/health").headers["Content-Security-Policy"]
    assert csp == content_security_policy(None)
    assert directive(csp, "img-src") == ["'self'", "data:", "blob:"]
    assert directive(csp, "media-src") == ["'self'", "blob:"]


# --- import ---


def test_importing_the_package_does_not_connect_to_the_database():
    env = {**os.environ, "DATABASE_URL": "postgresql://nobody:x@127.0.0.1:1/none?connect_timeout=30"}
    started = time.monotonic()
    r = subprocess.run([sys.executable, "-c", "import evidence_server, evidence_server.main"],
                       env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert time.monotonic() - started < 20
    env.pop("DATABASE_URL")
    r = subprocess.run([sys.executable, "-c", "import evidence_server.main"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    # How uvicorn loads "evidence_server.main:app" (the package's `main` name is the CLI function).
    r = subprocess.run([sys.executable, "-c", "import importlib; importlib.import_module('evidence_server.main').app"],
                       env=env, capture_output=True, text=True)
    assert r.returncode != 0 and "DATABASE_URL is not set" in r.stderr  # built on first use


def test_seal_small_file_list():
    assert {"manifest.json", "signature.txt", "public_key.txt"} == seal.SMALL_FILES
