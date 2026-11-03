"""T34 meta_client (contract: tasks/t34-design.md): the only door to Meta. Cache, cost log, never raises."""

import threading

import psycopg
import pytest

from conftest import FakeClock
from evidence_server.db import Database
from evidence_server.meta_client import MetaClient
from pg import TEST_DATABASE_URL, reset

MODEL = "muse-spark-1.3"
IMAGE = b"\xff\xd8\xff\xe0one-photo"


class Transport:
    def __init__(self, *answers):
        self.answers = list(answers) or ['{"ok": 1}']
        self.calls: list[dict] = []

    def __call__(self, model, prompt, image, mime, timeout):
        self.calls.append({"model": model, "prompt": prompt, "image": image, "mime": mime, "timeout": timeout})
        answer = self.answers[min(len(self.calls) - 1, len(self.answers) - 1)]
        if isinstance(answer, Exception):
            raise answer
        if isinstance(answer, dict):
            return answer
        return {"text": answer, "input_tokens": 1000, "output_tokens": 200}


@pytest.fixture
def db():
    admin = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    try:
        reset(admin)
    finally:
        admin.close()
    database = Database(TEST_DATABASE_URL)
    yield database
    database.close()


def rows(db):
    with db.tx() as conn:
        return conn.execute("SELECT * FROM model_calls ORDER BY created_at, id").fetchall()


def read(meta, prompt="Read the plate.", image=IMAGE, **kw):
    return meta.read_json(purpose="photo.plate#1", model=MODEL, prompt=prompt, image=image, mime="image/jpeg", **kw)


def test_ok_call_returns_the_json_and_logs_tokens_and_cost(db):
    transport = Transport('{"is_plate": true, "text": "DEMO101"}')
    meta = MetaClient(db, transport, clock=FakeClock())
    out = read(meta, claim_id="CLM-1", item_id="item-1")
    assert out["ok"] is True and out["cached"] is False and out["error"] is None
    assert out["json"] == {"is_plate": True, "text": "DEMO101"}
    assert transport.calls[0]["timeout"] == 30.0
    assert transport.calls[0]["mime"] == "image/jpeg"
    [row] = rows(db)
    assert row["id"] == out["call_id"]
    assert (row["status"], row["purpose"], row["model"], row["claim_id"], row["item_id"]) == (
        "ok", "photo.plate#1", MODEL, "CLM-1", "item-1",
    )
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 200)
    assert row["cost_usd"] == pytest.approx(1000 * 1.25 / 1e6 + 200 * 4.25 / 1e6)
    assert row["latency_ms"] >= 0
    assert row["created_at"] == FakeClock().now().isoformat()


def test_same_model_prompt_and_image_is_called_once(db):
    transport = Transport('{"a": 1}')
    meta = MetaClient(db, transport)
    first = read(meta)
    second = read(meta)
    assert second["ok"] is True and second["cached"] is True
    assert second["json"] == {"a": 1}
    assert second["call_id"] == first["call_id"]
    assert len(transport.calls) == 1
    assert len(rows(db)) == 1


def test_another_prompt_or_image_is_a_new_call(db):
    transport = Transport('{"a": 1}')
    meta = MetaClient(db, transport)
    read(meta)
    read(meta, prompt="Spell the VIN.")  # the second VIN read must not hit the first one's cache
    read(meta, image=IMAGE + b"-retake")
    assert len(transport.calls) == 3
    assert len(rows(db)) == 3


def test_cache_survives_a_new_client(db):
    read(MetaClient(db, Transport('{"a": 1}')))
    transport = Transport('{"a": 2}')
    assert read(MetaClient(db, transport))["json"] == {"a": 1}
    assert transport.calls == []


@pytest.mark.parametrize("text", ['```json\n{"a": 1}\n```', '```\n{"a": 1}\n```', '  {"a": 1}  '])
def test_fenced_json_is_accepted(db, text):
    assert read(MetaClient(db, Transport(text)))["json"] == {"a": 1}


@pytest.mark.parametrize("text", ["not json", "[1, 2]", '"just a string"', ""])
def test_anything_but_a_json_object_is_bad_json_and_not_cached(db, text):
    transport = Transport(text, '{"a": 1}')
    meta = MetaClient(db, transport)
    out = read(meta)
    assert (out["ok"], out["json"], out["error"]) == (False, None, "bad_json")
    assert rows(db)[0]["status"] == "bad_json"
    assert read(meta)["json"] == {"a": 1}  # a failure is never served from the cache
    assert len(transport.calls) == 2


def test_timeout_is_logged_and_returned(db):
    out = read(MetaClient(db, Transport(TimeoutError("slow"))))
    assert (out["ok"], out["error"]) == (False, "timeout")
    assert rows(db)[0]["status"] == "timeout"


def test_any_other_error_never_raises(db):
    out = read(MetaClient(db, Transport(RuntimeError("HTTP 500 from the API"))))
    assert out["ok"] is False
    assert out["error"].startswith("RuntimeError")
    [row] = rows(db)
    assert row["status"] == "error"
    assert len(row["error"]) <= 300


def test_no_transport_means_not_configured_and_no_row(db):
    out = read(MetaClient(db, None))
    assert (out["ok"], out["error"], out["cached"]) == (False, "model_not_configured", False)
    assert rows(db) == []


def test_cost_unknown_without_token_counts_or_price(db):
    meta = MetaClient(db, Transport({"text": '{"a": 1}', "input_tokens": None, "output_tokens": None}))
    read(meta)
    meta.read_json(purpose="p", model="some-other-model", prompt="x", image=IMAGE, mime="image/jpeg")
    assert [r["cost_usd"] for r in rows(db)] == [None, None]


def test_timeout_setting_reaches_the_transport(db):
    transport = Transport('{"a": 1}')
    read(MetaClient(db, transport, timeout_s=12.5))
    assert transport.calls[0]["timeout"] == 12.5


def test_concurrency_cap_answers_busy_instead_of_queueing_forever(db):
    release = threading.Event()
    started = threading.Event()

    def slow(model, prompt, image, mime, timeout):
        started.set()
        release.wait(5)
        return {"text": '{"a": 1}', "input_tokens": 1, "output_tokens": 1}

    meta = MetaClient(db, slow, max_concurrent=1, timeout_s=0.2)
    first: dict = {}
    worker = threading.Thread(target=lambda: first.update(read(meta)))
    worker.start()
    assert started.wait(5)
    try:
        out = read(meta, image=IMAGE + b"-other")
        assert (out["ok"], out["error"]) == (False, "busy")
    finally:
        release.set()
        worker.join(5)
    assert first["ok"] is True
    assert len(rows(db)) == 1  # "busy" adds no row


# --- text-only calls (T21) ---


def text(meta, prompt="Compare the story."):
    return meta.read_json(purpose="consistency.story", model=MODEL, prompt=prompt, claim_id=None)


def test_text_only_call_sends_no_image_and_is_cached(db):
    transport = Transport()
    meta = MetaClient(db, transport, clock=FakeClock())
    assert text(meta)["ok"] and text(meta)["cached"]
    assert [(c["image"], c["mime"]) for c in transport.calls] == [(None, None)]
    assert rows(db)[0]["purpose"] == "consistency.story"


def test_text_only_and_image_calls_do_not_share_a_cache_entry(db):
    transport = Transport()
    meta = MetaClient(db, transport, clock=FakeClock())
    text(meta, "Same prompt.")
    read(meta, "Same prompt.")
    assert len(transport.calls) == 2


# --- track 1: retries, budget cap, usage ---

from datetime import timedelta  # noqa: E402

from evidence_server import meta_client  # noqa: E402
from evidence_server.meta_client import TransientError  # noqa: E402

COST = 1000 * 1.25 / 1e6 + 200 * 4.25 / 1e6  # what one fake call costs


def test_transient_errors_are_retried_with_growing_backoff(db):
    transport = Transport(TransientError("RateLimitError: 429"), TransientError("APIConnectionError"), '{"a": 1}')
    slept: list[float] = []
    meta = MetaClient(db, transport, sleep=slept.append, backoff_s=0.5)
    out = read(meta, claim_id="CLM-1")
    assert out["ok"] is True and out["json"] == {"a": 1}
    assert len(transport.calls) == 3
    assert len(slept) == 2 and 0.5 <= slept[0] <= 0.625 and 1.0 <= slept[1] <= 1.25
    [row] = rows(db)
    assert (row["status"], row["attempts"]) == ("ok", 3)


def test_retries_are_bounded(db):
    transport = Transport(TransientError("InternalServerError: 503"))
    meta = MetaClient(db, transport, sleep=lambda s: None, max_attempts=3)
    out = read(meta)
    assert out["ok"] is False and "503" in out["error"]
    assert len(transport.calls) == 3
    [row] = rows(db)
    assert (row["status"], row["attempts"]) == ("error", 3)


@pytest.mark.parametrize("answer", [RuntimeError("HTTP 400 bad request"), "not json"])
def test_permanent_errors_and_bad_json_are_not_retried(db, answer):
    transport = Transport(answer, '{"a": 1}')
    out = read(MetaClient(db, transport, sleep=lambda s: None))
    assert out["ok"] is False and len(transport.calls) == 1


def test_timeouts_are_not_retried_by_default_but_can_be(db):
    transport = Transport(TimeoutError("slow"), '{"a": 1}')
    assert read(MetaClient(db, transport, sleep=lambda s: None))["error"] == "timeout"
    assert len(transport.calls) == 1
    transport = Transport(TimeoutError("slow"), '{"a": 1}')
    out = read(MetaClient(db, transport, sleep=lambda s: None, retry_timeouts=True), image=IMAGE + b"-2")
    assert out["ok"] is True and len(transport.calls) == 2


def test_claim_budget_turns_further_calls_into_a_budget_failure(db):
    transport = Transport('{"a": 1}')
    meta = MetaClient(db, transport, claim_budget_usd=COST * 2 - 1e-9)
    assert read(meta, image=b"1", claim_id="CLM-1")["ok"]
    assert read(meta, image=b"2", claim_id="CLM-1")["ok"]
    out = read(meta, image=b"3", claim_id="CLM-1")
    assert out["ok"] is False and out["error"].startswith("budget: claim cap")
    assert len(transport.calls) == 2  # nothing sent
    assert read(meta, image=b"1", claim_id="CLM-1")["cached"] is True  # cache hits are free and allowed
    assert read(meta, image=b"4", claim_id="CLM-2")["ok"]  # another claim has its own cap
    statuses = [r["status"] for r in rows(db)]
    assert statuses.count("budget") == 1


def test_day_budget_counts_every_claim_of_the_utc_day(db):
    clock = FakeClock()
    transport = Transport('{"a": 1}')
    meta = MetaClient(db, transport, clock=clock, claim_budget_usd=None, day_budget_usd=COST * 2 - 1e-9)
    assert read(meta, image=b"1", claim_id="A")["ok"]
    assert read(meta, image=b"2", claim_id="B")["ok"]
    out = read(meta, image=b"3", claim_id="C")
    assert out["error"].startswith("budget: daily cap")
    clock.advance(days=1)
    assert read(meta, image=b"3", claim_id="C")["ok"]  # a new UTC day


def test_calls_without_a_cost_count_against_the_cap(db):
    transport = Transport({"text": '{"a": 1}', "input_tokens": None, "output_tokens": None})
    meta = MetaClient(db, transport, claim_budget_usd=meta_client.UNKNOWN_CALL_COST_USD)
    assert read(meta, image=b"1", claim_id="X")["ok"]
    assert read(meta, image=b"2", claim_id="X")["error"].startswith("budget")


def test_failed_calls_without_tokens_do_not_count(db):
    transport = Transport(RuntimeError("HTTP 400"))
    meta = MetaClient(db, transport, claim_budget_usd=0.001)
    for n in range(3):
        assert read(meta, image=bytes([n]), claim_id="X")["error"].startswith("RuntimeError")


def test_budget_off_never_blocks(db):
    meta = MetaClient(db, Transport('{"a": 1}'), claim_budget_usd=None, day_budget_usd=None)
    for n in range(5):
        assert read(meta, image=bytes([n]), claim_id="X")["ok"]


def test_usage_sums_the_claim_log(db):
    transport = Transport('{"a": 1}', TimeoutError("slow"), {"text": "{}", "input_tokens": None, "output_tokens": None})
    meta = MetaClient(db, transport, claim_budget_usd=None)
    read(meta, image=b"1", claim_id="CLM-1")
    read(meta, image=b"2", claim_id="CLM-1")
    read(meta, image=b"3", claim_id="CLM-1")
    read(meta, image=b"1", claim_id="CLM-1")  # cached: no row
    read(meta, image=b"9", claim_id="OTHER")
    with db.tx() as conn:
        conn.execute("UPDATE model_calls SET latency_ms=1500 WHERE claim_id='CLM-1'")
        u = meta_client.usage(conn, "CLM-1")
    assert u == {"calls": 3, "failed": 1, "budget_blocked": 0, "cost_usd": round(COST, 4),
                 "cost_unknown_calls": 2, "seconds": 4.5}


def test_usage_of_a_claim_with_no_calls(db):
    with db.tx() as conn:
        assert meta_client.usage(conn, "none") == {"calls": 0, "failed": 0, "budget_blocked": 0, "cost_usd": 0,
                                                   "cost_unknown_calls": 0, "seconds": 0.0}


def test_settings_from_env():
    s = meta_client.settings_from_env({})
    assert s == {"max_output_tokens": 4096, "max_attempts": 3, "claim_budget_usd": 1.0, "day_budget_usd": 15.0,
                 "sam_max_calls": 0, "provider": "meta", "timeout_s": 30}  # T53 added the host and its timeout
    s = meta_client.settings_from_env({"META_MAX_OUTPUT_TOKENS": "off", "META_BUDGET_CLAIM_USD": "0.25",
                                       "META_BUDGET_DAY_USD": "OFF", "META_MAX_ATTEMPTS": "1",
                                       "META_SAM_MAX_CALLS": "off"})
    assert s == {"max_output_tokens": None, "max_attempts": 1, "claim_budget_usd": 0.25, "day_budget_usd": None,
                 "sam_max_calls": None, "provider": "meta", "timeout_s": 30}
    assert meta_client.settings_from_env({"META_SAM_MAX_CALLS": "2"})["sam_max_calls"] == 2
    for bad in ({"META_MAX_ATTEMPTS": "0"}, {"META_MAX_ATTEMPTS": "off"}, {"META_BUDGET_DAY_USD": "lots"},
                {"META_SAM_MAX_CALLS": "-1"}):
        with pytest.raises(ValueError):
            meta_client.settings_from_env(bad)


def test_claim_detail_shows_ai_usage(client, model, clock):
    from test_damage_flow import captured_claim, send
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    u = d["ai_usage"]
    assert u["calls"] == len(model.calls) and u["failed"] == 0 and u["budget_blocked"] == 0
    assert u["cost_usd"] == round(COST * len(model.calls), 4)
    assert set(u) == {"calls", "failed", "budget_blocked", "cost_usd", "cost_unknown_calls", "seconds"}


# --- openai_transport: error mapping and the output cap (no network) ---

import httpx  # noqa: E402
import openai  # noqa: E402
from types import SimpleNamespace  # noqa: E402

REQ = httpx.Request("POST", "https://api.meta.ai/v1/responses")


def _status(cls, code, message="x"):
    return cls(message, response=httpx.Response(code, request=REQ), body=None)


class FakeOpenAI:
    script: list = []
    calls: list = []

    def __init__(self, **kw):
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        FakeOpenAI.calls.append(kw)
        step = FakeOpenAI.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _ok(status="completed", reason=None):
    return SimpleNamespace(status=status, output_text='{"a": 1}',
                           incomplete_details=SimpleNamespace(reason=reason) if reason else None,
                           usage=SimpleNamespace(input_tokens=10, output_tokens=5))


@pytest.fixture
def fake_openai(monkeypatch):
    monkeypatch.setattr(openai, "OpenAI", FakeOpenAI)
    FakeOpenAI.script, FakeOpenAI.calls = [], []
    return FakeOpenAI


def test_transport_sends_the_output_cap(fake_openai):
    fake_openai.script = [_ok()]
    out = meta_client.openai_transport("k", max_output_tokens=777)("m", "p", None, None, 5)
    assert out == {"text": '{"a": 1}', "input_tokens": 10, "output_tokens": 5}
    assert fake_openai.calls[0]["max_output_tokens"] == 777


@pytest.mark.parametrize("error, expected", [
    (openai.APITimeoutError(request=REQ), TimeoutError),
    (openai.APIConnectionError(request=REQ), TransientError),
    (_status(openai.RateLimitError, 429), TransientError),
    (_status(openai.InternalServerError, 503), TransientError),
    (_status(openai.AuthenticationError, 401), openai.AuthenticationError),
])
def test_transport_maps_errors(fake_openai, error, expected):
    fake_openai.script = [error]
    with pytest.raises(expected):
        meta_client.openai_transport("k")("m", "p", None, None, 5)


def test_transport_drops_an_unsupported_output_cap_once(fake_openai):
    fake_openai.script = [_status(openai.BadRequestError, 400, "Unknown parameter: max_output_tokens"), _ok(), _ok()]
    transport = meta_client.openai_transport("k")
    assert transport("m", "p", None, None, 5)["text"] == '{"a": 1}'
    transport("m", "p", None, None, 5)
    assert ["max_output_tokens" in c for c in fake_openai.calls] == [True, False, False]


def test_transport_reports_an_answer_cut_off_by_the_cap(fake_openai):
    fake_openai.script = [_ok("incomplete", "max_output_tokens")]
    with pytest.raises(RuntimeError, match="incomplete: max_output_tokens"):
        meta_client.openai_transport("k")("m", "p", None, None, 5)


# --- track 6: SAM 3.1 through the same door ---

SAM_TEXT = "<0f>0<|box;x1=1;y1=1;x2=2;y2=2;w=4;h=4|><|mask;x=0;y=0;data=2,2,~!!!!!|>"


def segment(meta, concept="damage", image=IMAGE, **kw):
    return meta.segment(image, concept, purpose="outline.damage", claim_id="CLM-1", **kw)


def test_transport_sends_sam_the_concept_and_image_only(fake_openai):
    fake_openai.script = [SimpleNamespace(status="completed", output_text=SAM_TEXT, incomplete_details=None,
                                          usage=SimpleNamespace(input_tokens=0, output_tokens=0))]
    out = meta_client.openai_transport("k", max_output_tokens=777)("sam-3.1", "damage", b"jpeg", "image/jpeg", 5)
    assert out == {"text": SAM_TEXT, "input_tokens": 0, "output_tokens": 0}
    sent = fake_openai.calls[0]
    assert set(sent) == {"model", "input", "timeout"}  # no json format, no reasoning, no output cap
    content = sent["input"][0]["content"]
    assert content[0] == {"type": "input_text", "text": "damage"}
    assert content[1]["type"] == "input_image" and content[1]["image_url"].startswith("data:image/jpeg;base64,")


def test_segment_returns_the_raw_text_and_logs_an_unknown_cost(db):
    t = Transport({"text": SAM_TEXT, "input_tokens": 0, "output_tokens": 0})
    meta = MetaClient(db, t, clock=FakeClock())
    r = segment(meta, item_id="I-1")
    assert r["ok"] and r["text"] == SAM_TEXT and not r["cached"] and r["error"] is None
    assert t.calls[0]["model"] == "sam-3.1" and t.calls[0]["prompt"] == "damage"
    [row] = rows(db)
    assert (row["model"], row["purpose"], row["claim_id"], row["item_id"], row["status"]) == (
        "sam-3.1", "outline.damage", "CLM-1", "I-1", "ok")
    assert (row["input_tokens"], row["output_tokens"], row["cost_usd"]) == (0, 0, None)
    with db.tx() as conn:
        assert meta_client.usage(conn, "CLM-1")["cost_unknown_calls"] == 1


def test_segment_is_cached_like_any_call_and_an_empty_answer_is_an_answer(db):
    t = Transport({"text": "", "input_tokens": 0, "output_tokens": 0})
    meta = MetaClient(db, t, clock=FakeClock())
    first, second = segment(meta), segment(meta)
    assert (first["ok"], first["text"], second["ok"], second["text"], second["cached"]) == (True, "", True, "", True)
    assert len(t.calls) == 1
    assert segment(meta, concept="hole")["cached"] is False  # another concept is another call


def test_segment_failures_never_raise(db):
    meta = MetaClient(db, Transport(RuntimeError("boom")), clock=FakeClock())
    r = segment(meta)
    assert (r["ok"], r["text"]) == (False, None) and "boom" in r["error"]
    assert MetaClient(db, None).segment(IMAGE, "damage", purpose="x")["error"] == "model_not_configured"


def test_sam_calls_count_against_the_claim_budget_as_unknown_cost(db):
    t = Transport({"text": "", "input_tokens": 0, "output_tokens": 0})
    meta = MetaClient(db, t, clock=FakeClock(), claim_budget_usd=0.02)
    assert segment(meta, concept="a")["ok"] and segment(meta, concept="b")["ok"]
    r = segment(meta, concept="c")  # two unknown-cost calls count $0.01 each: the $0.02 cap is reached
    assert not r["ok"] and r["error"].startswith("budget:")
    assert len(t.calls) == 2
