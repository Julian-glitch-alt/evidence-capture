"""T54 (tasks/t54-design.md): pages are fresh after every deploy; the public contact address."""

import os

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_PASSWORD, FakeModel
from evidence_server.blobstore import LocalBlobStore
from evidence_server.main import create_app
from pg import TEST_DATABASE_URL, reset


def build_dist(root):
    """A built frontend whose files carry mtime 0, as in the Modal image."""
    (root / "assets").mkdir(parents=True)
    files = {
        root / "index.html": '<div id="root"></div><script src="/assets/index-Ab12.js"></script>',
        root / "favicon.svg": "<svg/>",
        root / "assets" / "index-Ab12.js": "console.log(1)",
    }
    for path, text in files.items():
        path.write_text(text)
        os.utime(path, (0, 0))
    return root


def app_with(tmp_path, clock, monkeypatch, contact=None):
    admin = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    try:
        reset(admin)
    finally:
        admin.close()
    if contact is None:
        monkeypatch.delenv("CONTACT_EMAIL", raising=False)
    else:
        monkeypatch.setenv("CONTACT_EMAIL", contact)
    app = create_app(
        data_dir=tmp_path / "data", frontend_dist=build_dist(tmp_path / "dist"), admin_password=ADMIN_PASSWORD,
        session_secret="test-secret", clock=clock, database_url=TEST_DATABASE_URL,
        blobstore=LocalBlobStore(tmp_path / "blobs"), model_transport=FakeModel(), check_license=False,
    )
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def web(tmp_path, clock, monkeypatch):
    c = app_with(tmp_path, clock, monkeypatch)
    yield c
    c.app.state.store.db.close()


@pytest.mark.parametrize("path", ["/", "/claims/CLM-20261102-ABC123", "/login", "/c/some-token", "/favicon.svg"])
def test_the_page_shell_is_never_cached(web, path):
    # index.html had no Cache-Control and Last-Modified 1970: browsers kept old builds for years.
    r = web.get(path)
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"


def test_hashed_build_files_are_cached_for_a_year(web):
    r = web.get("/assets/index-Ab12.js")
    assert r.status_code == 200 and r.text == "console.log(1)"
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_api_answers_keep_their_own_cache_rules(web):
    assert web.get("/api/health").headers.get("cache-control") != "public, max-age=31536000, immutable"


def test_the_contact_address_is_public(tmp_path, clock, monkeypatch):
    c = app_with(tmp_path, clock, monkeypatch, contact="  trials@example.com ")
    try:
        assert c.get("/api/contact").json()["email"] == "trials@example.com"
    finally:
        c.app.state.store.db.close()


@pytest.mark.parametrize("value", [None, "", "not-an-address", "a@b@c"])
def test_no_contact_address_reads_null(tmp_path, clock, monkeypatch, value):
    c = app_with(tmp_path, clock, monkeypatch, contact=value)
    try:
        assert c.get("/api/contact").json()["email"] is None
    finally:
        c.app.state.store.db.close()



def test_the_contact_answer_names_the_model_reading_photos(web):
    # T57: the privacy page says which model and host read the photos, so it stays true after a switch.
    from evidence_server.meta_client import PROVIDERS
    assert web.get("/api/contact").json()["model"] == PROVIDERS["meta"]
