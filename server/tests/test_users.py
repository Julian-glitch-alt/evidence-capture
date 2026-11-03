"""T47 (tasks/t47-design.md): adjuster accounts, the two roles, who did what."""

import re

import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, GOOD_STORY
from evidence_server import users
from test_damage_flow import db_rows
from test_loss_notice import detail
from test_review_flow import ready, review

OTP_ALPHABET = set("ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789")


def browser(anon) -> TestClient:
    """A second browser on the same server (its own cookie jar)."""
    return TestClient(anon.app, base_url="https://testserver")


def login(c, email, password):
    return c.post("/api/admin/login", json={"email": email, "password": password})


def add_user(client, name="Maria Lopez", email="maria@demo-mutual.example", role="adjuster"):
    r = client.post("/api/admin/users", json={"email": email, "name": name, "role": role})
    assert r.status_code == 201, r.text
    return r.json()


def signed_in(anon, client, role="adjuster", name="Maria Lopez", email="maria@demo-mutual.example"):
    """A new user who has signed in and chosen their own password: their own browser."""
    made = add_user(client, name, email, role)
    c = browser(anon)
    assert login(c, email, made["one_time_password"]).status_code == 200
    r = c.post("/api/admin/me/password", json={"current": made["one_time_password"], "new": "a long new passphrase"})
    assert r.status_code == 200, r.text
    return c, made


# --- passwords ---


def test_passwords_are_salted_scrypt_and_verify_with_the_stored_cost():
    one, two = users.hash_password("correct horse battery"), users.hash_password("correct horse battery")
    assert one != two and one.startswith("scrypt$")
    n = int(one.split("$")[1])
    assert users.verify_password("correct horse battery", one)
    assert not users.verify_password("correct horse batterY", one)
    slow = one.replace(f"scrypt${n}$", "scrypt$32$", 1)  # another cost: verification reads it from the hash
    assert not users.verify_password("correct horse battery", slow)


def test_one_time_passwords_use_the_unambiguous_alphabet():
    otps = {users.one_time_password() for _ in range(20)}
    assert len(otps) == 20
    assert all(len(p) == 16 and set(p) <= OTP_ALPHABET for p in otps)


# --- the first supervisor and login ---


def test_the_seeded_supervisor_signs_in_with_the_old_password(anon):
    r = login(anon, ADMIN_EMAIL, ADMIN_PASSWORD)
    assert r.status_code == 200, r.text
    me = r.json()["user"]
    assert (me["email"], me["name"], me["role"], me["must_change_password"]) == (
        ADMIN_EMAIL, "Administrator", "supervisor", False)
    assert anon.get("/api/admin/me").json() == me
    assert "password_hash" not in me


def test_email_is_case_and_space_insensitive(anon):
    assert login(anon, f"  {ADMIN_EMAIL.upper()} ", ADMIN_PASSWORD).status_code == 200


def test_wrong_password_unknown_email_and_password_alone_are_refused(anon):
    for body in ({"email": ADMIN_EMAIL, "password": "wrong password here"},
                 {"email": "nobody@demo-mutual.example", "password": ADMIN_PASSWORD}):
        r = anon.post("/api/admin/login", json=body)
        assert (r.status_code, r.json()["detail"]) == (401, "Wrong email or password"), body
    assert anon.post("/api/admin/login", json={"password": ADMIN_PASSWORD}).status_code in (400, 401, 422)


def test_an_old_style_cookie_is_refused(anon):
    anon.cookies.set("adj_session", "9999999999.deadbeef", domain="testserver")
    assert anon.get("/api/admin/me").status_code == 401


# --- users (supervisor) ---


def test_a_supervisor_adds_an_adjuster_who_must_choose_a_password(anon, client):
    made = add_user(client)
    assert (made["email"], made["name"], made["role"], made["active"]) == (
        "maria@demo-mutual.example", "Maria Lopez", "adjuster", True)
    otp = made["one_time_password"]
    assert len(otp) == 16 and set(otp) <= OTP_ALPHABET
    listed = client.get("/api/admin/users").json()
    assert [u["name"] for u in listed] == ["Administrator", "Maria Lopez"]
    assert all("password_hash" not in u and "one_time_password" not in u for u in listed)

    maria = browser(anon)
    r = login(maria, "Maria@Demo-Mutual.example", otp)
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is True
    r = maria.get("/api/admin/claims")
    assert (r.status_code, r.json()["detail"]) == (403, "Choose a new password first")
    assert maria.get("/api/admin/me").status_code == 200
    assert maria.post("/api/admin/me/password", json={"current": "not it at all", "new": "a long new passphrase"}).status_code == 400
    assert maria.post("/api/admin/me/password", json={"current": otp, "new": "short"}).status_code == 400
    assert maria.post("/api/admin/me/password", json={"current": otp, "new": otp}).status_code == 400
    r = maria.post("/api/admin/me/password", json={"current": otp, "new": "a long new passphrase"})
    assert r.status_code == 200 and r.json()["user"]["must_change_password"] is False
    assert maria.get("/api/admin/claims").status_code == 200  # the fresh cookie keeps this session
    assert login(browser(anon), "maria@demo-mutual.example", otp).status_code == 401


def test_new_users_are_validated(client):
    add_user(client)
    cases = [
        ({"email": "maria@demo-mutual.example", "name": "Maria Again", "role": "adjuster"}, 409),
        ({"email": "MARIA@demo-mutual.example ", "name": "Maria Again", "role": "adjuster"}, 409),
        ({"email": "not-an-email", "name": "X Y", "role": "adjuster"}, 400),
        ({"email": "a@b.example", "name": "", "role": "adjuster"}, 400),
        ({"email": "a@b.example", "name": "x" * 81, "role": "adjuster"}, 400),
        ({"email": "a@b.example", "name": "X Y", "role": "admin"}, 400),
    ]
    for body, status in cases:
        assert client.post("/api/admin/users", json=body).status_code == status, body


def test_an_adjuster_cannot_change_settings_users_or_run_the_sweep(anon, client):
    maria, _ = signed_in(anon, client)
    assert maria.get("/api/admin/settings").status_code == 200
    for method, url, body in (
        ("PUT", "/api/admin/settings", {"delivery_mode": "on_decision"}),
        ("GET", "/api/admin/users", None),
        ("POST", "/api/admin/users", {"email": "x@y.example", "name": "X Y", "role": "adjuster"}),
        ("POST", "/api/admin/sweep", None),
    ):
        r = maria.request(method, url, json=body)
        assert (r.status_code, r.json()["detail"]) == (403, "Supervisors only"), (method, url)


def test_deactivating_signs_the_user_out_at_once(anon, client):
    maria, made = signed_in(anon, client)
    r = client.patch(f"/api/admin/users/{made['id']}", json={"active": False})
    assert r.status_code == 200 and r.json()["active"] is False
    assert maria.get("/api/admin/claims").status_code == 401
    assert login(browser(anon), "maria@demo-mutual.example", "a long new passphrase").status_code == 401
    client.patch(f"/api/admin/users/{made['id']}", json={"active": True})
    assert login(browser(anon), "maria@demo-mutual.example", "a long new passphrase").status_code == 200


def test_a_role_change_signs_the_user_out_and_takes_effect(anon, client):
    maria, made = signed_in(anon, client)
    assert client.patch(f"/api/admin/users/{made['id']}", json={"role": "supervisor"}).json()["role"] == "supervisor"
    assert maria.get("/api/admin/claims").status_code == 401
    again = browser(anon)
    login(again, "maria@demo-mutual.example", "a long new passphrase")
    assert again.get("/api/admin/users").status_code == 200


def test_a_supervisor_cannot_lock_themselves_out(client):
    me = client.get("/api/admin/me").json()
    for body in ({"active": False}, {"role": "adjuster"}):
        r = client.patch(f"/api/admin/users/{me['id']}", json=body)
        assert (r.status_code, r.json()["detail"]) == (409, "You cannot remove your own supervisor access"), body
    assert client.patch("/api/admin/users/nope", json={"name": "X"}).status_code == 404


def test_a_password_reset_gives_a_new_one_time_password(anon, client):
    maria, made = signed_in(anon, client)
    otp = client.post(f"/api/admin/users/{made['id']}/reset-password").json()["one_time_password"]
    assert len(otp) == 16
    assert maria.get("/api/admin/claims").status_code == 401
    fresh = browser(anon)
    assert login(fresh, "maria@demo-mutual.example", otp).json()["user"]["must_change_password"] is True


def test_changing_my_password_signs_my_other_sessions_out(anon, client):
    maria, made = signed_in(anon, client)
    laptop = browser(anon)
    login(laptop, "maria@demo-mutual.example", "a long new passphrase")
    r = maria.post("/api/admin/me/password", json={"current": "a long new passphrase", "new": "another long passphrase"})
    assert r.status_code == 200
    assert laptop.get("/api/admin/claims").status_code == 401
    assert maria.get("/api/admin/claims").status_code == 200


# --- who did it ---


def test_reviews_and_decisions_name_the_person(anon, client, model, clock):
    model.set("story", {**GOOD_STORY, "consistent": "no"})
    claim = ready(client, clock)
    maria, made = signed_in(anon, client)
    key = detail(maria, claim)["proposal"]["flags"][0]["key"]
    d = review(maria, claim["id"], kind="flag", key=key, verdict="false_alarm", reason="The dent is on the rear, as told.").json()
    assert d["review"]["flags"][key]["by"] == "Maria Lopez"
    r = maria.post(f"/api/admin/claims/{claim['id']}/decision", json={"action": "approve", "reason": "Checked the photos myself.", "by": "Someone Else"})
    assert r.status_code == 200, r.text  # a `by` sent by the browser is ignored: the session says who
    [row] = db_rows(client, "SELECT user_id, decided_by FROM decisions WHERE claim_id=%s", claim["id"])
    assert (row["user_id"], row["decided_by"]) == (made["id"], "Maria Lopez")
    [rev] = db_rows(client, "SELECT user_id, decided_by FROM reviews WHERE claim_id=%s", claim["id"])
    assert (rev["user_id"], rev["decided_by"]) == (made["id"], "Maria Lopez")
    events = detail(client, claim)["events"]
    [flagged] = [e for e in events if e["type"] == "review.flag"]
    assert (flagged["data"]["user_id"], flagged["data"]["user_name"]) == (made["id"], "Maria Lopez")
    decided = [e for e in events if (e.get("data") or {}).get("action") == "approve"]
    assert decided and (decided[-1]["data"]["user_id"], decided[-1]["data"]["user_name"]) == (made["id"], "Maria Lopez")


def test_settings_remember_who_changed_them(client, clock):
    s = client.get("/api/admin/settings").json()
    assert (s["updated_by"], s["updated_at"]) == (None, None)
    client.put("/api/admin/settings", json={"delivery_mode": "on_decision"})
    s = client.get("/api/admin/settings").json()
    assert s["updated_by"] == "Administrator" and re.match(r"\d{4}-\d{2}-\d{2}T", s["updated_at"])


@pytest.mark.parametrize("password", ["", "x" * 11, "x" * 129])
def test_password_rules(password):
    with pytest.raises(ValueError):
        users.check_new_password(password)
    users.check_new_password("x" * 12)
    users.check_new_password("x" * 128)


def test_a_bad_field_leaves_the_settings_unchanged(client):
    r = client.put("/api/admin/settings", json={"delivery_mode": "on_decision", "brand_color": "blue"})
    assert r.status_code == 400
    s = client.get("/api/admin/settings").json()
    assert (s["delivery_mode"], s["updated_by"]) == ("on_ready", None)


def test_wrong_current_passwords_count_toward_the_login_limit(anon, client):
    maria, _ = signed_in(anon, client)
    for _ in range(5):
        r = maria.post("/api/admin/me/password", json={"current": "wrong wrong wrong", "new": "another long passphrase"})
        assert r.status_code == 400
    r = maria.post("/api/admin/me/password", json={"current": "a long new passphrase", "new": "another long passphrase"})
    assert r.status_code == 429


def test_two_containers_seeding_at_once_keep_one_supervisor(anon):
    import threading

    store = anon.app.state.store
    with store.db.tx() as conn:
        conn.execute("DELETE FROM users")
    barrier = threading.Barrier(2)
    errors = []

    def seed():
        try:
            with store.db.tx() as conn:
                barrier.wait(timeout=10)  # both transactions open: neither sees a user yet
                store._seed_supervisor(conn, ADMIN_EMAIL, ADMIN_PASSWORD)
        except Exception as e:  # noqa: BLE001 - the test reports whatever a container would crash on
            errors.append(e)

    threads = [threading.Thread(target=seed) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    with store.db.tx() as conn:
        assert [r["email"] for r in conn.execute("SELECT email FROM users").fetchall()] == [ADMIN_EMAIL]
