"""Adjuster accounts: email + password login, a signed session cookie, per-IP limit (T47).

No global failure cap: it would let anyone lock adjusters out. The per-IP
limit (5 failures in 15 minutes) lives in Postgres so every container enforces
the same limit. Sessions are `adj_session` =
`{user_id}.{session_version}.{expires}.{hmac}`; changing the role,
deactivating, resetting, or the user changing their own password bumps
`session_version`, which signs every other session out at once.
"""

import hashlib
import hmac
import secrets
import time

from fastapi import HTTPException, Request, Response

from . import users as users_mod
from .db import Database
from .errors import CaptureError

COOKIE = "adj_session"
SESSION_SECONDS = 12 * 3600
FAIL_WINDOW = 15 * 60
FAILS_PER_IP = 5  # per client IP (Modal passes the real client address)

#: While the user must choose a new password, every admin route answers 403
#: except these three.
GATE_MESSAGE = "Choose a new password first"
OPEN_WHILE_FORCED = frozenset({
    ("GET", "/api/admin/me"),
    ("POST", "/api/admin/me/password"),
    ("POST", "/api/admin/logout"),
})


class AdminAuth:
    def __init__(self, session_secret: str | None, db: Database, org_id: str, login_configured: bool):
        # Without a configured secret, sessions only last as long as this process.
        self.secret = (session_secret or secrets.token_hex(32)).encode()
        self.db = db
        self.org_id = org_id
        self.login_configured = login_configured
        # One scrypt for unknown emails, so the timing does not tell (T47).
        self._dummy_hash = users_mod.hash_password(secrets.token_hex(16))

    @staticmethod
    def public(row: dict) -> dict:
        """The `me` shape the console shows: never the hash."""
        return {
            "id": row["id"],
            "email": row["email"],
            "name": row["name"],
            "role": row["role"],
            "must_change_password": bool(row["must_change_password"]),
        }

    def set_session(self, response: Response, user_id: str, session_version: int) -> None:
        expires = int(time.time()) + SESSION_SECONDS
        body = f"{user_id}.{session_version}.{expires}"
        response.set_cookie(
            COOKIE,
            f"{body}.{self._sign(body)}",
            max_age=SESSION_SECONDS,
            httponly=True,
            secure=True,
            samesite="lax",
        )

    def login(self, request: Request, response: Response, email: str, password: str) -> dict:
        """Check the email + password, set the session cookie, and return `me`."""
        email = (email or "").strip().lower()
        password = password or ""
        with self.db.tx() as conn:
            configured = self.login_configured or conn.execute(
                "SELECT 1 FROM users WHERE org_id=%s LIMIT 1", (self.org_id,)
            ).fetchone()
        if not configured:
            raise HTTPException(503, "Login is not configured on this server")
        row = self._find_by_email(email)
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        limited = False
        failed = False
        with self.db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (ip,))
            conn.execute(
                "DELETE FROM login_failures WHERE ip = %s AND at <= %s",
                (ip, now - FAIL_WINDOW),
            )
            failures = conn.execute(
                "SELECT COUNT(*) AS n FROM login_failures WHERE ip = %s AND at > %s",
                (ip, now - FAIL_WINDOW),
            ).fetchone()
            if failures["n"] >= FAILS_PER_IP:
                limited = True
            else:
                candidate = row["password_hash"] if row is not None else self._dummy_hash
                password_ok = users_mod.verify_password(password, candidate)
                if row is not None and row["active"] and password_ok:
                    conn.execute("DELETE FROM login_failures WHERE ip = %s", (ip,))
                else:
                    conn.execute("INSERT INTO login_failures (ip, at) VALUES (%s, %s)", (ip, now))
                    failed = True
        if limited:
            raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
        if failed or row is None:
            raise HTTPException(401, "Wrong email or password")
        self.set_session(response, row["id"], row["session_version"])
        return self.public(row)

    def logout(self, response: Response) -> None:
        response.delete_cookie(COOKIE, httponly=True, secure=True, samesite="lax")

    def require(self, request: Request) -> dict:
        """FastAPI dependency for every adjuster route: the signed-in user (`me`).

        A forced password change gates everything except me / my password / logout.
        """
        parts = request.cookies.get(COOKIE, "").split(".")
        if len(parts) != 4:
            raise HTTPException(401, "Please log in")
        user_id, version, expires, signature = parts
        if (
            not version.isdigit()
            or not expires.isdigit()
            or not hmac.compare_digest(signature.encode(), self._sign(f"{user_id}.{version}.{expires}").encode())
            or int(expires) <= time.time()
        ):
            raise HTTPException(401, "Please log in")
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id=%s AND org_id=%s", (user_id, self.org_id)
            ).fetchone()
        if row is None or not row["active"] or str(row["session_version"]) != version:
            raise HTTPException(401, "Please log in")
        if row["must_change_password"] and (request.method, request.url.path) not in OPEN_WHILE_FORCED:
            raise HTTPException(403, GATE_MESSAGE)
        return self.public(row)

    def require_supervisor(self, request: Request) -> dict:
        """FastAPI dependency for the supervisor routes: settings, sweep, users."""
        me = self.require(request)
        if me["role"] != "supervisor":
            raise HTTPException(403, "Supervisors only")
        return me

    def change_password(self, request: Request, user_id: str, current: str, new: str) -> tuple[dict, int]:
        """The signed-in user chooses a new password: verifies, stores, signs
        every other session out. Returns (`me`, the new session_version) so the
        caller can set a fresh cookie for this session.

        A wrong current password counts as a failed login for the same per-IP
        limit as `login` (the sixth try in 15 minutes answers 429, checked
        before the password is verified); a correct one leaves the IP's
        failures alone."""
        with self.db.tx() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id=%s AND org_id=%s", (user_id, self.org_id)
            ).fetchone()
            if row is None or not row["active"]:
                raise HTTPException(401, "Please log in")
            stored_hash = row["password_hash"]
        ip = request.client.host if request is not None and request.client else "unknown"
        now = time.time()
        limited = False
        failed = False
        with self.db.tx() as conn:
            conn.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (ip,))
            conn.execute(
                "DELETE FROM login_failures WHERE ip = %s AND at <= %s",
                (ip, now - FAIL_WINDOW),
            )
            failures = conn.execute(
                "SELECT COUNT(*) AS n FROM login_failures WHERE ip = %s AND at > %s",
                (ip, now - FAIL_WINDOW),
            ).fetchone()
            if failures["n"] >= FAILS_PER_IP:
                limited = True
            elif not users_mod.verify_password(current or "", stored_hash):
                conn.execute("INSERT INTO login_failures (ip, at) VALUES (%s, %s)", (ip, now))
                failed = True
        if limited:
            raise HTTPException(429, "Too many failed attempts. Try again in 15 minutes.")
        if failed:
            raise CaptureError("The current password is wrong", 400)
        with self.db.tx() as conn:
            try:
                users_mod.check_new_password(new or "")
            except ValueError as e:
                raise CaptureError(str(e), 400) from e
            if (current or "") == (new or ""):
                raise CaptureError("The new password must be different from the current password", 400)
            conn.execute(
                "UPDATE users SET password_hash=%s, must_change_password=FALSE, session_version=session_version+1"
                " WHERE id=%s",
                (users_mod.hash_password(new), user_id),
            )
            fresh = conn.execute(
                "SELECT * FROM users WHERE id=%s", (user_id,)
            ).fetchone()
            return self.public(fresh), fresh["session_version"]

    def _find_by_email(self, email: str) -> dict | None:
        with self.db.tx() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE org_id=%s AND email=%s", (self.org_id, email)
            ).fetchone()

    def _sign(self, value: str) -> str:
        return hmac.new(self.secret, value.encode(), hashlib.sha256).hexdigest()
