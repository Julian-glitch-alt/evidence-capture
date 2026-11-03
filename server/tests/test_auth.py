import pytest
from conftest import ADMIN_EMAIL, ADMIN_PASSWORD

PUBLIC_ADMIN_ROUTES = {"/api/admin/login"}


def test_every_admin_route_refuses_without_session(anon):
    # The OpenAPI schema lists every route, including ones added later.
    paths = anon.get("/api/openapi.json").json()["paths"]
    admin_routes = [
        (method.upper(), path)
        for path, methods in paths.items()
        if path.startswith("/api/admin") and path not in PUBLIC_ADMIN_ROUTES
        for method in methods
    ]
    assert len(admin_routes) >= 6  # me, logout, claims list + create, claim detail, item file
    for method, path in admin_routes:
        url = path.replace("{claim_id}", "CLM-X").replace("{item_id}", "x")
        r = anon.request(method, url)
        assert r.status_code == 401, f"{method} {url} -> {r.status_code}"


def test_login_sets_session_and_logout_clears_it(anon):
    assert anon.get("/api/admin/me").status_code == 401
    r = anon.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 200
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert anon.get("/api/admin/me").status_code == 200

    anon.post("/api/admin/logout")
    assert anon.get("/api/admin/me").status_code == 401


def test_wrong_password_is_refused(anon):
    assert anon.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": "nope"}).status_code == 401


def test_forged_cookie_is_refused(anon):
    anon.cookies.set("adj_session", "9999999999.deadbeef", domain="testserver")
    assert anon.get("/api/admin/me").status_code == 401


def test_sixth_failure_in_window_is_rate_limited(anon):
    for _ in range(5):
        assert anon.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": "nope"}).status_code == 401
    r = anon.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    assert r.status_code == 429


def test_concurrent_wrong_passwords_leave_at_most_five_failures(anon):
    """Ten simultaneous wrong passwords: the advisory lock serialises them, so
    at most five INSERTs happen and the rest are refused with 429."""
    import threading
    from concurrent.futures import ThreadPoolExecutor

    start = threading.Barrier(10)

    def attempt(_i: int) -> int:
        start.wait()
        return anon.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": "nope"}).status_code

    with ThreadPoolExecutor(max_workers=10) as pool:
        statuses = list(pool.map(attempt, range(10)))
    assert statuses.count(429) >= 5, statuses
    with anon.app.state.store.db.tx() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM login_failures").fetchone()["n"]
    assert n <= 5, n
    assert anon.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 429


def test_claimant_and_public_routes_need_no_session(anon):
    assert anon.get("/api/health").status_code == 200
    assert anon.get("/api/c/not-a-token").status_code == 404  # reaches the handler, not 401


@pytest.mark.parametrize("password", [None, ""])
def test_login_is_closed_when_no_password_is_configured(tmp_path, password):
    from fastapi.testclient import TestClient

    import psycopg

    from evidence_server.blobstore import LocalBlobStore
    from evidence_server.main import create_app
    from pg import TEST_DATABASE_URL, reset

    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as conn:
        reset(conn)  # T47: no user left over from another test, so nobody can sign in
    app = create_app(
        data_dir=tmp_path / "d",
        frontend_dist=tmp_path / "n",
        admin_password=password,
        session_secret="s",
        database_url=TEST_DATABASE_URL,
        blobstore=LocalBlobStore(tmp_path / "b"),
    )
    try:
        c = TestClient(app, base_url="https://testserver")
        assert c.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ""}).status_code == 503
    finally:
        app.state.store.db.close()
