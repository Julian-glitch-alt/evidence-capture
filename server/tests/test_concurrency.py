"""Concurrency on Postgres (tasks/t35-design.md I2–I4).

Driven through the store directly, with real threads: one claim's writes wait for
each other, different claims' writes do not, a nonce is consumed once.
"""

import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import JPEG
from evidence_server.errors import CaptureError

SLOTS = ["plate", "vin_photo", "odometer", "driver_license", "damage_wide", "damage_closeup", "damage_closeup", "damage_closeup"]


def store_of(client):
    return client.app.state.store


def opened(client) -> dict:
    r = client.post("/api/admin/claims", json={"policy_number": "DEMO-AUTO-1001"})
    assert r.status_code == 200, r.text
    return r.json()


def hold_claim(store, claim_id: str, seconds: float) -> threading.Thread:
    """Hold one claim's lock in the background; returns once the lock is taken."""
    taken = threading.Event()

    def hold():
        with store.db.claim_tx(claim_id=claim_id):
            taken.set()
            time.sleep(seconds)

    thread = threading.Thread(target=hold)
    thread.start()
    assert taken.wait(5)
    return thread


def test_two_claims_do_not_wait(client):
    store = store_of(client)
    a, b = opened(client), opened(client)
    holder = hold_claim(store, a["id"], 1.0)
    t0 = time.monotonic()
    store.start_session(b["token"])
    elapsed = time.monotonic() - t0
    holder.join()
    assert elapsed < 0.5, f"claim B waited {elapsed:.2f} s for claim A's lock"


def test_same_claim_waits_for_its_lock(client):
    store = store_of(client)
    a = opened(client)
    holder = hold_claim(store, a["id"], 1.0)
    t0 = time.monotonic()
    store.start_session(a["token"])
    elapsed = time.monotonic() - t0
    holder.join()
    assert elapsed >= 0.8, f"a write to the locked claim did not wait ({elapsed:.2f} s)"


def test_parallel_uploads_keep_the_chain(client):
    store = store_of(client)
    claim = opened(client)
    session = store.start_session(claim["token"])["session_id"]
    nonces = [store.issue_nonce(claim["token"], session, slot)["nonce"] for slot in SLOTS]
    start = threading.Barrier(len(SLOTS))

    def upload(i: int):
        start.wait()
        return store.add_item(claim["token"], SLOTS[i], JPEG + bytes([i]), "image/jpeg", {}, session, nonces[i])

    with ThreadPoolExecutor(len(SLOTS)) as pool:
        items = list(pool.map(upload, range(len(SLOTS))))

    assert len({i["id"] for i in items}) == len(SLOTS)
    seqs = [e["seq"] for e in store.events(claim["id"])]
    assert seqs == list(range(1, len(seqs) + 1))
    assert store.verify_chain(claim["id"])["ok"] is True


def test_one_nonce_one_item(client):
    store = store_of(client)
    claim = opened(client)
    session = store.start_session(claim["token"])["session_id"]
    nonce = store.issue_nonce(claim["token"], session, "plate")["nonce"]
    start = threading.Barrier(2)

    def upload(data: bytes):
        start.wait()
        try:
            return store.add_item(claim["token"], "plate", data, "image/jpeg", {}, session, nonce)
        except CaptureError as e:
            return e

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(upload, [JPEG + b"first", JPEG + b"second"]))

    stored = [r for r in results if isinstance(r, dict)]
    refused = [r for r in results if isinstance(r, CaptureError)]
    assert len(stored) == 1 and len(refused) == 1
    assert refused[0].status == 403


@pytest.mark.parametrize("statement", ["UPDATE claim_events SET actor = 'x'", "DELETE FROM claim_events"])
def test_database_refuses_to_rewrite_the_log(client, statement):
    store = store_of(client)
    opened(client)
    with pytest.raises(Exception, match="append-only"):
        with store.db.tx() as conn:
            conn.execute(statement)
