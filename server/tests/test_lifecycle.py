import pytest

from evidence_server.store import CaptureError


@pytest.fixture
def store(client):
    return client.app.state.store


def test_known_policy_opens_claim_with_verified_snapshot(store):
    claim = store.open_claim("DEMO-AUTO-1001")
    detail = store.claim_detail(claim["id"])

    assert detail["status"] == "opened"
    snapshot = detail["policy"]
    assert snapshot["verified"] is True
    assert snapshot["policy_no"] == "DEMO-AUTO-1001"
    assert snapshot["data"]["vehicles"][0]["vin"] == "1HGCV1F39LA526018"
    assert snapshot["data"]["coverages"]["collision"]["deductible"] == 500
    assert [e["type"] for e in store.events(claim["id"])] == ["claim.opened"]


def test_unknown_policy_still_opens_but_unverified(store):
    claim = store.open_claim("NOPE-999")
    snapshot = store.claim_detail(claim["id"])["policy"]

    assert snapshot["verified"] is False
    assert snapshot["policy_no"] == "NOPE-999"
    assert snapshot["data"] is None


# Decision 29, main path of a photo-lane claim, with the actor who moves each step.
HAPPY_PATH = [
    ("capturing", "customer"),
    ("submitted", "customer"),
    ("verifying", "system"),
    ("assessed", "system"),
    ("in_review", "system"),
    ("approved", "user"),
    ("repairing", "user"),
    ("repair_proof", "customer"),
    ("bill_submitted", "customer"),
    ("awaiting_payment", "user"),
    ("paid", "user"),
    ("closed", "user"),
]


def test_main_path_writes_one_event_per_step(store):
    claim = store.open_claim("DEMO-AUTO-1001")
    for to_status, actor in HAPPY_PATH:
        store.transition(claim["id"], to_status, actor)

    events = store.events(claim["id"])
    assert [e["to_status"] for e in events] == ["opened"] + [s for s, _ in HAPPY_PATH]
    assert [e["actor"] for e in events[1:]] == [a for _, a in HAPPY_PATH]
    assert store.claim_detail(claim["id"])["status"] == "closed"
    assert store.verify_chain(claim["id"])["ok"] is True


@pytest.mark.parametrize(
    "path, to_status, actor",
    [
        ([], "submitted", "customer"),  # cannot skip capture
        ([("capturing", "customer"), ("submitted", "customer")], "approved", "user"),  # skips review
        (HAPPY_PATH[:5], "approved", "system"),  # only a person approves
        (HAPPY_PATH[:5], "approved", "customer"),
        (HAPPY_PATH, "opened", "user"),  # closed is final
        ([], "flying", "user"),  # unknown status
    ],
)
def test_disallowed_transition_is_refused_and_writes_nothing(store, path, to_status, actor):
    claim = store.open_claim("DEMO-AUTO-1001")
    for s, a in path:
        store.transition(claim["id"], s, a)
    before = store.events(claim["id"])

    with pytest.raises(CaptureError) as err:
        store.transition(claim["id"], to_status, actor)

    assert err.value.status == 409
    assert store.events(claim["id"]) == before


def test_branches_exist(store):
    for branch in ["more_evidence", "siu", "total_loss", "handed_to_human"]:
        claim = store.open_claim("DEMO-AUTO-1001")
        for s, a in HAPPY_PATH[:5]:
            store.transition(claim["id"], s, a)
        store.transition(claim["id"], branch, "user")
        assert store.claim_detail(claim["id"])["status"] == branch


def test_unverified_policy_cannot_reach_payment(store):
    claim = store.open_claim("NOPE-999")
    for s, a in HAPPY_PATH[:9]:  # up to bill_submitted
        store.transition(claim["id"], s, a)

    with pytest.raises(CaptureError) as err:
        store.transition(claim["id"], "awaiting_payment", "user")
    assert "policy" in str(err.value).lower()


def test_editing_a_stored_event_breaks_the_chain_at_that_event(store):
    from evidence_server import lifecycle

    claim = store.open_claim("DEMO-AUTO-1001")
    for s, a in HAPPY_PATH[:3]:
        store.transition(claim["id"], s, a)
    assert store.verify_chain(claim["id"])["ok"] is True

    # An attacker with database access can drop the append-only trigger.
    # Run the DROP + tamper inside a forced-rollback transaction so the trigger
    # (schema, not data, so TRUNCATE in pg.reset would not restore it) is still
    # present for later tests. Postgres DDL is transactional, so the rollback
    # restores the trigger.
    with store.db.pool.connection() as conn:
        with conn.transaction(force_rollback=True):
            conn.execute("DROP TRIGGER claim_events_no_write ON claim_events")
            conn.execute(
                "UPDATE claim_events SET actor='user' WHERE claim_id=%s AND seq=3",
                (claim["id"],),
            )
            assert lifecycle.verify_chain(conn, claim["id"]) == {"ok": False, "broken_at": 3}


def test_event_log_refuses_update_and_delete(store):
    import psycopg

    claim = store.open_claim("DEMO-AUTO-1001")
    # One transaction each: after a refused statement its transaction is aborted.
    with pytest.raises(psycopg.Error, match="append-only"):
        with store.db.tx() as conn:
            conn.execute("UPDATE claim_events SET actor='x' WHERE claim_id=%s", (claim["id"],))
    with pytest.raises(psycopg.Error, match="append-only"):
        with store.db.tx() as conn:
            conn.execute("DELETE FROM claim_events WHERE claim_id=%s", (claim["id"],))


def test_new_claim_has_four_undetermined_decisions(store):
    claim = store.open_claim("DEMO-AUTO-1001")
    decisions = store.claim_detail(claim["id"])["decisions"]
    assert decisions == {
        "liability": "UNDETERMINED",
        "coverage_eligibility": "UNDETERMINED",
        "damage_severity": "UNDETERMINED",
        "payment_approval": "UNDETERMINED",
    }


def test_fact_keeps_source_and_evidence(store):
    claim = store.open_claim("DEMO-AUTO-1001")
    store.set_fact(claim["id"], "incident_date", "2026-10-27", "REPORTED", ["form:loss.date"])

    fact = store.claim_detail(claim["id"])["facts"]["incident_date"]
    assert fact == {"value": "2026-10-27", "source": "REPORTED", "evidence": ["form:loss.date"]}


@pytest.mark.parametrize(
    "source, evidence",
    [
        ("REPORTED", ["form:loss.description"]),
        ("INFERRED", ["item:abc"]),
        ("VERIFIED", ["form:loss.description"]),  # verified, but not from the policy
    ],
)
def test_coverage_only_from_policy_snapshot(store, source, evidence):
    claim = store.open_claim("DEMO-AUTO-1001")
    with pytest.raises(CaptureError):
        store.set_fact(claim["id"], "coverage_type", "COLLISION", source, evidence)


def test_coverage_from_policy_snapshot_is_accepted(store):
    claim = store.open_claim("DEMO-AUTO-1001")
    snapshot_id = store.claim_detail(claim["id"])["policy"]["id"]
    store.set_fact(
        claim["id"], "coverage_type", "COLLISION", "VERIFIED", [f"policy_snapshot:{snapshot_id}"]
    )
    assert store.claim_detail(claim["id"])["facts"]["coverage_type"]["source"] == "VERIFIED"


def test_unknown_source_is_refused(store):
    claim = store.open_claim("DEMO-AUTO-1001")
    with pytest.raises(CaptureError):
        store.set_fact(claim["id"], "incident_date", "2026-10-27", "GUESSED", [])
