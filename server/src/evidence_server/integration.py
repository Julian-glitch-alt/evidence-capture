"""T16 inbound capture API credentials and the shared HTTP transport (tasks/t16-t29-design.md).

Both sides (our `Store` and the mock `DemoInsurer`) send every outbound call
through an `Http` function attribute, so tests can route `https://testserver/...`
URLs back into the TestClient. Nothing else does network I/O.

T48 (tasks/t48-design.md): one org holds many API keys (`api_keys`), each live
or test; `check_api_key` answers which org and mode a Bearer [REDACTED] belongs to.
"""

import hashlib
import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import psycopg

Http = Callable[[str, str, bytes | None, dict[str, str]], tuple[int, bytes]]
"""(method, absolute url, body bytes or headers) -> (status code, response body bytes). Raises on network failure."""


def httpx_transport(timeout: float = 15.0) -> Http:
    """The default transport."""

    def send(method: str, url: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
        import httpx

        r = httpx.request(method, url, content=body, headers=headers, timeout=timeout)
        return r.status_code, r.content

    return send


def ensure_credentials(conn: psycopg.Connection, org_id: str, now: datetime) -> None:
    """Create the API key + webhook secret pair on first start-up. Idempotent.

    In production the insurer's IT would receive these out of band; here the
    mock's row stands for that handover. The mock's key is also the seed row
    of `api_keys` ("Demo insurer (mock)", live), so it is listed and keeps working.
    """
    row = conn.execute("SELECT 1 FROM integration_credentials WHERE org_id=%s", (org_id,)).fetchone()
    if row:
        return
    api_key = "ek_live_" + secrets.token_urlsafe(32)
    webhook_secret = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO integration_credentials (org_id, api_key_sha256, webhook_secret, created_at)"
        " VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
        (org_id, hashlib.sha256(api_key.encode()).hexdigest(), webhook_secret, now.isoformat()),
    )
    conn.execute(
        "INSERT INTO mock_config (id, api_key, webhook_secret, next_claim_seq)"
        " VALUES (1, %s, %s, 415) ON CONFLICT (id) DO NOTHING",
        (api_key, webhook_secret),
    )
    conn.execute(
        "INSERT INTO api_keys (id, org_id, name, mode, prefix, key_sha256, created_at, created_by)"
        " VALUES (%s, %s, 'Demo insurer (mock)', 'live', %s, %s, %s, NULL)"
        " ON CONFLICT (key_sha256) DO NOTHING",
        (uuid.uuid4().hex, org_id, api_key[:12],
         hashlib.sha256(api_key.encode()).hexdigest(), now.isoformat()),
    )


def _bearer_key(header: str | None) -> str | None:
    scheme, _, key = (header or "").partition(" ")
    key = key.strip()
    if scheme != "Bearer" or not key:
        return None
    return key


def check_api_key(
    conn: psycopg.Connection, header: str | None, now: datetime | None = None
) -> dict | None:
    """The key a valid `Authorization: Bearer <key>` header belongs to, else None.

    -> {"org_id", "key_id", "mode"}. One indexed lookup by `key_sha256`.
    Active = not revoked and not expired. Records `last_used_at` when it is
    unset or older than 60 seconds, so steady traffic writes at most once a minute.
    """
    key = _bearer_key(header)
    if key is None:
        return None
    now = now or datetime.now(UTC)
    now_iso = now.isoformat()
    digest = hashlib.sha256(key.encode()).hexdigest()
    row = conn.execute("SELECT * FROM api_keys WHERE key_sha256=%s", (digest,)).fetchone()
    if row is None:
        return None
    if row["revoked_at"] is not None:
        return None
    if row["expires_at"] is not None and row["expires_at"] <= now_iso:
        return None
    last = row["last_used_at"]
    if last is None or last < (now - timedelta(seconds=60)).isoformat():
        conn.execute("UPDATE api_keys SET last_used_at=%s WHERE id=%s", (now_iso, row["id"]))
    return {"org_id": row["org_id"], "key_id": row["id"], "mode": row["mode"]}


def key_status(row: dict, now_iso: str) -> str:
    """active | expiring (active with an `expires_at`) | expired | revoked."""
    if row["revoked_at"] is not None:
        return "revoked"
    if row["expires_at"] is not None and row["expires_at"] <= now_iso:
        return "expired"
    if row["expires_at"] is not None:
        return "expiring"
    return "active"


def public_key(row: dict, now_iso: str) -> dict:
    """The key row as the API lists it: never the hash, never the secret."""
    return {
        "id": row["id"],
        "name": row["name"],
        "mode": row["mode"],
        "prefix": row["prefix"],
        "created_at": row["created_at"],
        "created_by": row["created_by"],
        "last_used_at": row["last_used_at"],
        "expires_at": row["expires_at"],
        "revoked_at": row["revoked_at"],
        "status": key_status(row, now_iso),
    }


def mint_key(conn: psycopg.Connection, org_id: str, name: str, mode: str,
             created_by: str | None, now_iso: str) -> tuple[dict, str]:
    """Store a new key and return (row, the full key, shown once)."""
    key = ("ek_live_" if mode == "live" else "ek_test_") + secrets.token_urlsafe(32)
    row = {
        "id": uuid.uuid4().hex,
        "org_id": org_id,
        "name": name,
        "mode": mode,
        "prefix": key[:12],
        "key_sha256": hashlib.sha256(key.encode()).hexdigest(),
        "created_at": now_iso,
        "created_by": created_by,
        "last_used_at": None,
        "expires_at": None,
        "revoked_at": None,
    }
    conn.execute(
        "INSERT INTO api_keys (id, org_id, name, mode, prefix, key_sha256,"
        " created_at, created_by, last_used_at, expires_at, revoked_at)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, NULL, NULL, NULL)",
        (row["id"], org_id, name, mode, row["prefix"], row["key_sha256"], now_iso, created_by),
    )
    return row, key
