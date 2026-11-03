"""Track 3: the console queue reads a bounded number of SQL statements, whatever the number of claims."""

import psycopg
import pytest

from test_damage_flow import captured_claim, send
from test_decision_flow import decide
from test_loss_notice import open_claim


class Counter:
    def __init__(self):
        self.n = 0


@pytest.fixture
def statements(monkeypatch):
    counter = Counter()
    real = psycopg.Cursor.execute

    def counting(self, *args, **kwargs):
        counter.n += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(psycopg.Cursor, "execute", counting)
    return counter


def add_claims(client, clock, submitted, opened, decided=0):
    for _ in range(opened):
        open_claim(client)
    for i in range(submitted + decided):
        claim, _ = captured_claim(client, clock)
        send(client, claim)
        if i < decided:
            assert decide(client, claim["id"], action="approve", by="J. Smith").status_code == 200


def queue_statements(client, statements):
    statements.n = 0
    r = client.get("/api/admin/queue")
    assert r.status_code == 200, r.text
    return statements.n, r.json()


def test_queue_statements_do_not_grow_with_claims(client, model, clock, statements):
    add_claims(client, clock, submitted=1, opened=1, decided=1)
    small, q_small = queue_statements(client, statements)
    add_claims(client, clock, submitted=2, opened=3, decided=1)
    large, q_large = queue_statements(client, statements)
    assert sum(g["count"] for g in q_small["groups"]) == 3
    assert sum(g["count"] for g in q_large["groups"]) == 9
    print(f"queue statements: 3 claims -> {small}, 9 claims -> {large}")
    assert large == small
    assert large <= 15


def reference_queue(store):
    """The queue built the old way, from claim_detail per claim (before track 3)."""
    from evidence_server import desk

    details = [(s, store.claim_detail(s["id"])) for s in store.list_claims()]
    groups = []
    for key, label in desk.QUEUES:
        members = [(s, d) for s, d in details
                   if desk.queue_key(d["submitted_at"], d["proposal"], d["decision"], d["status"]) == key]
        if key == "decided":
            members.sort(key=lambda sd: ((sd[1]["decision"] or {}).get("decided_at") or ""), reverse=True)
        elif key == "capturing":
            members.sort(key=lambda sd: sd[1]["created_at"] or "")
        else:
            members.sort(key=lambda sd: sd[1]["submitted_at"] or sd[1]["created_at"] or "")
        claims = []
        for s, d in members:
            p = d["proposal"]
            ready = p is not None and p.get("status") == "ready"
            v = (((d.get("policy") or {}).get("data") or {}).get("vehicles") or [None])[0]
            vehicle = f"{v['year']} {v['make']} {v['model']}" if v else None
            open_diaries = [x for x in d["diaries"] if not x["done_at"]]
            claims.append({**s, "lane": p.get("lane") if ready else None,
                           "headline": p.get("headline") if ready else None,
                           "flags": len(p.get("flags") or []) if ready else 0, "vehicle": vehicle,
                           # T52
                           "claim_type": (d["claim_type"] or {}).get("label") if d["answers"].get("loss_type") else None,
                           "review_due": d["review_due"],
                           "overdue": bool(d["review_due"]) and d["decision"] is None
                           and d["review_due"] < store.clock.now().date().isoformat(),
                           "diary_due": open_diaries[0]["due_on"] if open_diaries else None,
                           "diary_reason": open_diaries[0]["reason"] if open_diaries else None})
        groups.append({"key": key, "label": label, "count": len(claims), "claims": claims})
    return {"groups": groups, "averages": desk.averages([d["cycle"] for _, d in details])}


def test_queue_matches_the_per_claim_detail(client, model, clock):
    from conftest import GOOD_STORY
    from test_loss_notice import detail

    add_claims(client, clock, submitted=1, opened=2, decided=1)
    model.set("story", {**GOOD_STORY, "consistent": "no"})  # a flagged claim
    flagged, _ = captured_claim(client, clock)
    d = send(client, flagged)
    key = d["damage"]["damages"][0]["key"]
    r = client.post(f"/api/admin/claims/{flagged['id']}/review",
                    json={"kind": "damage", "key": key, "verdict": "excluded", "reason": "Old scratch, not this loss."})
    assert r.status_code == 200, r.text
    pending, _ = captured_claim(client, clock)
    client.app.state.store.submit(pending["token"])  # analysis not run yet
    clock.advance(minutes=1)
    assert detail(client, pending)["proposal"] == {"status": "pending"}
    q = client.get("/api/admin/queue").json()
    assert q == reference_queue(client.app.state.store)
    assert {g["key"]: g["count"] for g in q["groups"]}["analysing"] == 1
