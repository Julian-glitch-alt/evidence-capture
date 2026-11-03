"""T59b: what the insurer's policy already says (state, holder phone, holder name) is filled in for the claimant,
who can change it. The date, city and everything about the story are never guessed."""

from test_loss_notice import DAMAGE_ANSWERS, capture_required, detail, open_claim, save
from test_loss_notice_other import field_of

NOTE_START = {"loss_state": "From your policy", "callback_phone": "The number we have", "full_name": "From your policy"}


def test_state_and_name_are_filled_in_from_the_policy(client):
    claim = open_claim(client)  # DEMO-AUTO-1001: CA, Jordan Avery
    layout = save(client, claim["token"], {"loss_type": "vehicle_damage"})
    state, name = field_of(layout, "loss_state"), field_of(layout, "full_name")
    assert state["value"] == "CA" and state["prefilled"] is True and state["note"].startswith(NOTE_START["loss_state"])
    assert name["value"] == "Jordan Avery" and name["prefilled"] is True and name["note"].startswith(NOTE_START["full_name"])


def test_the_phone_is_filled_in_on_the_short_form(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "something_else"})
    phone = field_of(layout, "callback_phone")
    assert phone["value"] == "+1 415 555 0101" and phone["prefilled"] is True
    assert phone["note"].startswith(NOTE_START["callback_phone"])


def test_typing_replaces_the_filled_in_value(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "vehicle_damage", "loss_state": "NV"})
    state = field_of(layout, "loss_state")
    assert state["value"] == "NV" and not state.get("prefilled") and not state.get("note")


def test_nothing_else_is_guessed(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "vehicle_damage"})
    for field_id in ("loss_date", "loss_city", "loss_street", "description", "driver_name"):
        f = field_of(layout, field_id)
        if f is not None:
            assert not f.get("value") and not f.get("prefilled"), field_id


def test_no_policy_on_file_means_nothing_is_filled_in(client):
    claim = open_claim(client, "NOT-ON-FILE-1")
    layout = save(client, claim["token"], {"loss_type": "vehicle_damage"})
    for field_id in ("loss_state", "full_name"):
        f = field_of(layout, field_id)
        assert not f.get("value") and not f.get("prefilled") and not f.get("note"), field_id
    assert "State" in layout["missing"]


def test_a_claim_with_untouched_filled_in_fields_completes_and_submits(client):
    claim = open_claim(client)
    answers = {k: v for k, v in DAMAGE_ANSWERS.items() if k not in ("loss_state", "full_name")}
    layout = save(client, claim["token"], answers)
    assert "State" not in layout["missing"] and "Your full name" not in layout["missing"]
    capture_required(client, claim["token"], layout)
    state = client.get(f"/api/c/{claim['token']}").json()
    assert state["layout"]["complete"] is True, state["layout"]["missing"]
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 200
    d = detail(client, claim)
    assert d["facts"]["loss_state"]["value"] == "CA"
