"""T53 (tasks/t53-design.md): a trial policy from the tester's own car, policy data sent by the insurer
with the capture request, the Meta / Fireworks model host switch."""

import json
import re
from datetime import date
from zoneinfo import ZoneInfo
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient

from conftest import ADMIN_EMAIL, ADMIN_PASSWORD, JPEG, FakeModel, Phone
from evidence_server import meta_client, policies, report
from evidence_server.blobstore import LocalBlobStore
from evidence_server.main import create_app
from evidence_server.meta_client import FIREWORKS_MODELS, PROVIDERS, MetaClient, settings_from_env
from evidence_server.errors import CaptureError
from pg import TEST_DATABASE_URL, reset
from test_damage_flow import CLOSEUP, WIDE, db_rows, send
from test_demo_insurer import API as MOCK_API, new_claim as mock_claim, wired  # noqa: F401
from test_integration_api import auth
from test_loss_notice import DAMAGE_ANSWERS, MEDIA, save

TODAY = date(2026, 10, 28)  # conftest.FakeClock
TOYOTA_VIN = "JTDKARFU1PA186091"
SEED = json.loads(policies.SEED_FILE.read_text())


def seed_policy(n=0):
    p = dict(SEED["policies"][n])
    p.pop("policy_no")
    return p


def trial(**vehicle):
    return {"holder_name": "Mike Tester", "state": "TX",
            "vehicle": {"year": 2023, "make": "Toyota", "model": "Camry", "plate": "8abc-123", "vin": TOYOTA_VIN, **vehicle}}


def open_trial(client, body=None):
    r = client.post("/api/admin/claims", json={"trial": body or trial()})
    assert r.status_code == 200, r.text
    return r.json()


def detail(client, claim_id):
    r = client.get(f"/api/admin/claims/{claim_id}")
    assert r.status_code == 200, r.text
    return r.json()


def checks(d):
    return {c["check_id"]: (c["result"], c.get("reason_code")) for c in d["checks"]}


# --- A1 validation ---


def test_a_seed_policy_is_valid_and_comes_back_normalised():
    raw = seed_policy() | {"note": "dropped"}
    raw["vehicles"] = [dict(raw["vehicles"][0], vin=raw["vehicles"][0]["vin"].lower(), plate="demo-101")]
    p = policies.validate_policy(raw, TODAY, "policy.")
    assert "note" not in p
    assert p["vehicles"][0]["vin"] == "1HGCV1F39LA526018" and p["vehicles"][0]["plate"] == "DEMO101"
    assert p["state"] == "CA" and p["holder"]["name"] == "Jordan Avery"
    assert p["coverages"]["collision"] == {"deductible": 500}


@pytest.mark.parametrize("change, path", [
    (lambda p: p.pop("state"), "state"),
    (lambda p: p.update(state="XX"), "state"),
    (lambda p: p.update(expires=p["effective"]), "expires"),
    (lambda p: p.update(effective="2026-02-30"), "effective"),
    (lambda p: p.update(holder={"name": ""}), "holder.name"),
    (lambda p: p.update(vehicles=[]), "vehicles"),
    (lambda p: p["vehicles"][0].update(vin="5YJ3E1EA8MA159084"), "vehicles[0].vin"),
    (lambda p: p["vehicles"][0].update(year=1975), "vehicles[0].year"),
    (lambda p: p["vehicles"][0].update(plate="TOO-LONG-PLATE"), "vehicles[0].plate"),
    (lambda p: p["vehicles"][0].pop("make"), "vehicles[0].make"),
    (lambda p: p.update(coverages={}), "coverages"),
    (lambda p: p.update(coverages={"collision": {"deductible": -1}}), "coverages.collision.deductible"),
])
def test_bad_policies_name_the_field(change, path):
    p = json.loads(json.dumps(seed_policy()))
    change(p)
    with pytest.raises(CaptureError) as e:
        policies.validate_policy(p, TODAY, "policy.")
    assert str(e.value.args[0]).startswith(f"policy.{path}: "), e.value.args[0]


def test_a_bad_vin_gives_the_form_reason():
    p = seed_policy()
    p["vehicles"] = [dict(p["vehicles"][0], vin="5YJ3E1EA8MA159084")]
    with pytest.raises(CaptureError) as e:
        policies.validate_policy(p, TODAY, "policy.")
    assert e.value.args[0] == "policy.vehicles[0].vin: This VIN is not valid. Check each character."


# --- A3 trial policy from the console ---


def test_a_trial_claim_freezes_the_testers_car(client):
    claim = open_trial(client)
    assert re.fullmatch(r"POL-[0-9A-F]{6}", claim["policy_number"])  # T56: made up when left empty
    policy = detail(client, claim["id"])["policy"]
    assert (policy["policy_no"], policy["verified"], policy["source"]) == (claim["policy_number"], True, "trial")
    data = policy["data"]
    assert data["vehicles"] == [{"vin": TOYOTA_VIN, "plate": "8ABC123", "year": 2023, "make": "Toyota", "model": "Camry"}]
    assert data["holder"] == {"name": "Mike Tester"} and data["drivers"] == [{"name": "Mike Tester"}]
    assert (data["effective"], data["expires"], data["state"]) == ("2026-09-28", "2027-09-28", "TX")
    assert data["coverages"] == policies.TRIAL_COVERAGES
    assert (data["source"], data["entered_by"]) == ("trial", "Administrator")


def test_a_trial_policy_is_not_kept_as_a_policy(client):
    before = client.get("/api/admin/policies").json()
    open_trial(client)
    assert client.get("/api/admin/policies").json() == before
    assert db_rows(client, "SELECT COUNT(*) AS n FROM policies WHERE policy_no LIKE 'TRIAL-%%'")[0]["n"] == 0


@pytest.mark.parametrize("body, field", [
    (trial(vin="5YJ3E1EA8MA159084"), "vin"),
    (trial(plate=""), "plate"),
    (trial(year=1900), "year"),
    (trial(make=""), "make"),
    (trial() | {"state": "ZZ"}, "state"),
    (trial() | {"holder_name": "  "}, "holder_name"),
])
def test_trial_errors_name_the_console_field(client, body, field):
    r = client.post("/api/admin/claims", json={"trial": body})
    assert r.status_code == 400
    assert r.json()["detail"].startswith(f"{field}: "), r.json()["detail"]


@pytest.mark.parametrize("body", [{}, {"policy_number": "DEMO-AUTO-1001", "trial": trial()}])
def test_send_either_a_policy_number_or_a_trial(client, body):
    r = client.post("/api/admin/claims", json=body)
    assert r.status_code == 400 and r.json()["detail"] == "Send either policy_number or trial."


def test_an_on_file_claim_still_answers_with_its_policy_number(client):
    r = client.post("/api/admin/claims", json={"policy_number": "demo-auto-1001"})
    assert r.status_code == 200 and r.json()["policy_number"] == "DEMO-AUTO-1001"
    assert detail(client, r.json()["id"])["policy"]["source"] == "on_file"


def test_an_unknown_policy_number_has_no_source(client):
    r = client.post("/api/admin/claims", json={"policy_number": "NOPE-1"})
    policy = detail(client, r.json()["id"])["policy"]
    assert (policy["verified"], policy["source"]) == (False, None)


def test_the_opened_event_says_where_the_policy_came_from(client):
    claim = open_trial(client)
    opened = [e for e in detail(client, claim["id"])["events"] if e["type"] == "claim.opened"][0]
    assert opened["data"]["policy_source"] == "trial"


def test_an_honest_trial_claim_passes_the_vehicle_checks(client, model, clock):
    # The tester photographs their own Toyota: the reads match the trial policy, so no wrong-vehicle flag.
    model.set("plate", {"is_plate": True, "text": "8ABC123", "state": "TX", "unsure": False, "problem": "none"})
    model.set("vin_photo", {"is_vin": True, "vin": TOYOTA_VIN, "unsure": False, "problem": "none"},
              {"is_vin": True, "characters": list(TOYOTA_VIN), "unsure": False, "problem": "none"})
    claim = open_trial(client)
    layout = save(client, claim["token"], DAMAGE_ANSWERS | {"vin": TOYOTA_VIN, "full_name": "Mike Tester"})
    phone = Phone(client, claim["token"])
    shots = {"damage_closeup": CLOSEUP, "damage_wide": WIDE}
    for section in layout["sections"]:
        for slot in section["slots"]:
            if slot["required"] or slot["id"] in shots:
                clock.advance(seconds=1)
                data, mime = (shots[slot["id"]], "image/jpeg") if slot["id"] in shots else MEDIA.get(slot["kind"], (JPEG, "image/jpeg"))
                assert phone.upload(slot["id"], data, mime).status_code == 200
    d = send(client, claim)
    c = checks(d)
    assert c["coverage.in_force"][0] == "pass"
    assert c["coverage.vehicle_listed"] == ("pass", None)
    assert c["photo.plate"][0] == "pass" and c["photo.vin_photo"][0] == "pass"
    codes = {code for _, code in c.values()}
    assert not codes & {"VIN_MISMATCH", "PLATE_MISMATCH", "POLICY_NOT_VERIFIED"}


def test_the_queue_marks_trial_claims(client):
    trial_claim = open_trial(client)
    on_file = client.post("/api/admin/claims", json={"policy_number": "DEMO-AUTO-1001"}).json()
    entries = {c["id"]: c for g in client.get("/api/admin/queue").json()["groups"] for c in g["claims"]}
    assert entries[trial_claim["id"]]["trial"] is True
    assert entries[on_file["id"]]["trial"] is False


def test_the_report_says_where_the_policy_came_from(client):
    claim = open_trial(client)
    built = report.build(detail(client, claim["id"]), "Demo Mutual Insurance",
                         client.app.state.store.clock.now(), ZoneInfo("America/Chicago"))
    assert built["policy_source"] == "trial" and built["policy_verified"] is True


# --- A4 policy data from the insurer's system ---


def capture_request(client, number="DM-2026-0700", **extra):
    body = {"insurer_claim_number": number, "policy_number": "PAS-778812", "callback_url": "https://insurer.example/hook",
            "claimant": {"name": "Ana Lopez"}, **extra}
    return client.post("/api/v1/capture-requests", json=body, headers=auth(client))


def insurer_policy():
    p = seed_policy(3)  # Riley Chen's Toyota, as if from the insurer's own policy system
    p["holder"] = {"name": "Ana Lopez"}
    return p


def test_the_insurer_can_send_the_policy_with_the_request(client):
    r = capture_request(client, policy=insurer_policy())
    assert r.status_code == 201, r.text
    policy = detail(client, r.json()["case_id"])["policy"]
    assert (policy["policy_no"], policy["verified"], policy["source"]) == ("PAS-778812", True, "insurer")
    assert policy["data"]["holder"] == {"name": "Ana Lopez"}
    assert policy["data"]["vehicles"][0]["vin"] == "JTDKARFU1PA186091"
    assert db_rows(client, "SELECT COUNT(*) AS n FROM policies WHERE policy_no='PAS-778812'")[0]["n"] == 0


def test_a_bad_policy_from_the_insurer_is_refused_before_anything_is_written(client):
    p = insurer_policy()
    p["vehicles"] = [dict(p["vehicles"][0], vin="5YJ3E1EA8MA159084")]
    before = db_rows(client, "SELECT COUNT(*) AS n FROM claims")[0]["n"]
    r = capture_request(client, policy=p)
    assert r.status_code == 400
    assert r.json()["detail"] == "policy.vehicles[0].vin: This VIN is not valid. Check each character."
    assert db_rows(client, "SELECT COUNT(*) AS n FROM claims")[0]["n"] == before


def test_a_replay_keeps_the_first_policy(client):
    first = capture_request(client, policy=insurer_policy()).json()
    other = insurer_policy() | {"holder": {"name": "Someone Else"}}
    again = capture_request(client, policy=other)
    assert again.status_code == 200 and again.json()["case_id"] == first["case_id"]
    assert detail(client, first["case_id"])["policy"]["data"]["holder"] == {"name": "Ana Lopez"}


def test_without_a_policy_the_request_looks_the_number_up_as_before(client):
    r = capture_request(client, number="DM-2026-0701")
    policy = detail(client, r.json()["case_id"])["policy"]
    assert (policy["verified"], policy["source"]) == (False, None)


def test_the_store_hands_out_a_seeded_policy_in_full(client):
    assert client.app.state.store.policy_data("DEMO-AUTO-1001") == seed_policy(0)
    assert client.app.state.store.policy_data("NOPE-1") is None


def test_the_mock_claims_system_sends_its_policy(wired):  # noqa: F811
    c = mock_claim(wired)
    v = wired.post(f"{MOCK_API}/claims/{c['claim_number']}/services/verified-capture").json()
    policy = detail(wired, v["services"][0]["vendor_ref"])["policy"]
    assert (policy["policy_no"], policy["source"]) == ("DEMO-AUTO-1001", "insurer")
    assert policy["data"]["vehicles"][0]["plate"] == "DEMO101"


# --- B model host ---


def test_provider_settings_from_the_environment():
    assert (settings_from_env({})["provider"], settings_from_env({})["timeout_s"]) == ("meta", 30)
    fw = settings_from_env({"MODEL_PROVIDER": "Fireworks"})
    assert (fw["provider"], fw["timeout_s"]) == ("fireworks", 60)
    assert settings_from_env({"MODEL_PROVIDER": "meta", "MODEL_TIMEOUT_S": "45"})["timeout_s"] == 45
    with pytest.raises(ValueError):
        settings_from_env({"MODEL_PROVIDER": "openai"})
    with pytest.raises(ValueError):
        settings_from_env({"MODEL_TIMEOUT_S": "2"})


class Recorder:
    def __init__(self):
        self.models = []

    def __call__(self, model, prompt, image, mime, timeout):
        self.models.append(model)
        return {"text": '{"ok": true}', "input_tokens": 1000, "output_tokens": 200}


def test_the_client_maps_the_model_and_logs_what_was_served(client):
    db = client.app.state.store.db
    sent = Recorder()
    fw = MetaClient(db, sent, models=FIREWORKS_MODELS, provider="fireworks")
    assert fw.read_json(purpose="t53", model="muse-spark-1.3", prompt="p", image=b"x", mime="image/jpeg")["ok"]
    assert sent.models == ["accounts/fireworks/models/muse-spark-open"] and fw.provider == "fireworks"
    row = db_rows(client, "SELECT model, cost_usd FROM model_calls WHERE purpose='t53'")[0]
    assert row["model"] == "accounts/fireworks/models/muse-spark-open"
    assert row["cost_usd"] == pytest.approx(1000 * 1.25e-6 + 200 * 4.25e-6)
    meta = MetaClient(db, sent)
    assert meta.read_json(purpose="t53", model="muse-spark-1.3", prompt="p", image=b"x", mime="image/jpeg")["cached"] is False
    assert sent.models[-1] == "muse-spark-1.3"  # one host's answer is never served as the other's


def test_the_transport_sends_store_only_when_asked(monkeypatch):
    seen = []

    class FakeOpenAI:
        def __init__(self, **kw):
            self.responses = SimpleNamespace(create=lambda **args: seen.append(args) or SimpleNamespace(
                status="completed", output_text="{}", usage=SimpleNamespace(input_tokens=1, output_tokens=1)))

    import openai
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    meta_client.openai_transport("k", base_url=meta_client.FIREWORKS_BASE_URL, store=False)("m", "p", b"x", "image/jpeg", 5)
    meta_client.openai_transport("k")("m", "p", b"x", "image/jpeg", 5)
    assert seen[0]["store"] is False and "store" not in seen[1]


@pytest.fixture
def fireworks(tmp_path, clock):
    admin = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    try:
        reset(admin)
    finally:
        admin.close()
    app = create_app(
        data_dir=tmp_path / "data", frontend_dist=tmp_path / "none", admin_password=ADMIN_PASSWORD,
        session_secret="test-secret", clock=clock, database_url=TEST_DATABASE_URL,
        blobstore=LocalBlobStore(tmp_path / "blobs"), model_transport=FakeModel(), check_license=False,
        model_provider="fireworks",
    )
    c = TestClient(app, base_url="https://testserver")
    assert c.post("/api/admin/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}).status_code == 200
    yield c
    app.state.store.db.close()


def test_on_fireworks_every_call_goes_to_muse_spark_open(fireworks, clock):
    claim = open_trial(fireworks)
    phone = Phone(fireworks, claim["token"])
    assert phone.upload("plate").status_code == 200
    model = fireworks.app.state.store.meta.transport
    assert model.calls and {c["model"] for c in model.calls} == {"accounts/fireworks/models/muse-spark-open"}
    assert {c["timeout"] for c in model.calls} == {60}
    assert fireworks.app.state.store.sam_max_calls == 0


def test_system_health_names_the_model_on_meta(client):
    assert client.get("/api/admin/health").json()["model"] == PROVIDERS["meta"]


def test_system_health_names_the_model_and_its_data_rule_on_fireworks(fireworks):
    assert fireworks.get("/api/admin/health").json()["model"] == PROVIDERS["fireworks"]
    assert PROVIDERS["fireworks"] == {"provider": "fireworks", "model": "Muse Spark Open", "host": "Fireworks",
                                      "retention": "Zero data retention"}



# --- T56: the console's "Open a claim" takes the policy number and the deductibles ---


def test_open_a_claim_keeps_the_policy_number_and_deductibles(client):
    body = trial() | {"policy_number": " pas-778812 ", "collision_deductible": "1000", "comprehensive_deductible": 0}
    claim = open_trial(client, body)
    assert claim["policy_number"] == "PAS-778812"
    data = detail(client, claim["id"])["policy"]["data"]
    assert data["coverages"] == {"collision": {"deductible": 1000}, "comprehensive": {"deductible": 0},
                                 "liability": {"limits": "100/300/100"}}


@pytest.mark.parametrize("extra, field", [
    ({"policy_number": "BAD NUMBER!"}, "policy_number"),
    ({"policy_number": "X" * 41}, "policy_number"),
    ({"collision_deductible": 20000}, "collision_deductible"),
    ({"comprehensive_deductible": "lots"}, "comprehensive_deductible"),
])
def test_open_a_claim_names_a_bad_number(client, extra, field):
    r = client.post("/api/admin/claims", json={"trial": trial() | extra})
    assert r.status_code == 400 and r.json()["detail"].startswith(f"{field}: "), r.json()["detail"]
