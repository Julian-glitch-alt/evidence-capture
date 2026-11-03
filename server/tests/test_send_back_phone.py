"""Track 4: what the phone needs for round 2 after a Send back. The requested slots,
which of them are still waiting for a new shot, and answers that stay as sealed."""

from conftest import Phone
from test_damage_flow import captured_claim, send
from test_decision_flow import decide
from test_send_back import NEW_WIDE


def state(client, claim):
    return client.get(f"/api/c/{claim['token']}").json()


def test_round_one_has_nothing_to_retake(client, model, clock):
    claim, _ = captured_claim(client, clock)
    s = state(client, claim)
    assert (s["requested_slots"], s["retakes_left"]) == ([], [])


def test_retakes_left_shrinks_as_the_claimant_retakes(client, model, clock):
    claim, _ = captured_claim(client, clock)
    send(client, claim)
    clock.advance(minutes=5)
    r = decide(client, claim["id"], action="send_back", reason="Too dark.", slots=["damage_wide", "odometer"])
    assert r.status_code == 200, r.text
    s = state(client, claim)
    assert (s["requested_slots"], s["retakes_left"]) == (["damage_wide", "odometer"], ["damage_wide", "odometer"])
    # The round-1 shot is still there for the read-only view; it does not count as a retake.
    assert any(i["step_id"] == "damage_wide" for i in s["items"])

    phone = Phone(client, claim["token"])
    clock.advance(seconds=5)
    assert phone.upload("damage_wide", NEW_WIDE).status_code == 200
    assert state(client, claim)["retakes_left"] == ["odometer"]

    # Answers are frozen in round 2: a draft save changes nothing the phone or the seal sees.
    before = state(client, claim)["answers"]
    r = client.put(f"/api/c/{claim['token']}/draft", json={"answers": before | {"description": "Changed my story."}})
    assert r.status_code == 200 and r.json()["answers"] == before

    clock.advance(seconds=5)
    assert phone.upload("odometer").status_code == 200
    assert state(client, claim)["retakes_left"] == []
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 200
