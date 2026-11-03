"""T59: a choice is a suggestion, never a dead end. A field with `other` names the option that opens a
free-text answer (stored as `<id>_other`); yes / no questions the checks rely on stay closed."""

import pytest

from test_loss_notice import DAMAGE_ANSWERS, open_claim, save

# (loss type, field id, the option that opens the text box)
OPEN_FIELDS = [
    ("vehicle_damage", "cause", "Scrape or other"),
    ("single_vehicle", "hit_what", "Another object"),
    ("animal", "animal", "Another animal"),
    ("vandalism", "vandalism_kind", "Something else"),
    ("glass", "glass_which", "Something else"),
    ("glass", "glass_cause", "Something else"),
    ("weather", "weather_kind", "Something else"),
    ("parts_theft", "parts_taken", "Something else"),
    ("fire", "fire_start", "Something else"),
]
CLOSED_FIELDS = [  # the checks read these exact values
    ("vehicle_damage", "other_vehicle"), ("vehicle_damage", "injuries"), ("vehicle_damage", "police_contacted"),
    ("vehicle_damage", "drivable"), ("vehicle_damage", "driver_is_holder"), ("vehicle_damage", "prior_damage"),
    ("fire", "fire_dept"),
]


def field_of(layout, field_id):
    for section in layout["sections"]:
        for f in section["fields"]:
            if f["id"] == field_id:
                return f
    return None


def answer_for(field, escape):
    return [escape] if field["type"] == "multiselect" else escape


def shown(client, loss_type, field_id, extra=None):
    """The layout of a fresh claim of this type, with the answers that make the field appear."""
    claim = open_claim(client)
    answers = {"loss_type": loss_type} | (extra or {})
    return claim, save(client, claim["token"], answers)


@pytest.mark.parametrize("loss_type, field_id, escape", OPEN_FIELDS)
def test_the_open_field_names_its_escape_option(client, loss_type, field_id, escape):
    claim, layout = shown(client, loss_type, field_id)
    f = field_of(layout, field_id)
    assert f is not None, f"{field_id} is not shown for {loss_type}"
    assert f["other"] == escape
    assert escape in f["options"]


@pytest.mark.parametrize("loss_type, field_id", CLOSED_FIELDS)
def test_the_yes_no_questions_stay_closed(client, loss_type, field_id):
    claim, layout = shown(client, loss_type, field_id, {"fire_start": "Engine"} if loss_type == "fire" else {})
    f = field_of(layout, field_id)
    assert f is not None
    assert "other" not in f


@pytest.mark.parametrize("loss_type, field_id, escape", OPEN_FIELDS)
def test_choosing_the_escape_asks_for_the_words(client, loss_type, field_id, escape):
    claim, layout = shown(client, loss_type, field_id)
    f = field_of(layout, field_id)
    layout = save(client, claim["token"], {"loss_type": loss_type, field_id: answer_for(f, escape)})
    assert f["label"] in layout["missing"]
    assert field_of(layout, field_id)["other_text"] in (None, "")

    layout = save(client, claim["token"], {"loss_type": loss_type, field_id: answer_for(f, escape),
                                           f"{field_id}_other": "  A shopping cart rolled into it  "})
    assert field_of(layout, field_id)["other_text"] == "A shopping cart rolled into it"
    assert f["label"] not in layout["missing"]
    answers = client.get(f"/api/c/{claim['token']}").json()["answers"]
    assert answers[f"{field_id}_other"] == "A shopping cart rolled into it"


def test_the_words_are_dropped_when_another_option_is_chosen(client):
    claim = open_claim(client)
    save(client, claim["token"], {"loss_type": "vehicle_damage", "cause": "Scrape or other", "cause_other": "A cart"})
    save(client, claim["token"], {"loss_type": "vehicle_damage", "cause": "Hit an object", "cause_other": "A cart"})
    answers = client.get(f"/api/c/{claim['token']}").json()["answers"]
    assert answers["cause"] == "Hit an object"
    assert "cause_other" not in answers


def test_the_words_are_limited_and_unknown_companions_are_dropped(client):
    claim = open_claim(client)
    save(client, claim["token"], {"loss_type": "vehicle_damage", "cause": "Scrape or other", "cause_other": "x" * 5000,
                                  "drivable": "Yes", "drivable_other": "sneaky", "bogus_other": "x"})
    answers = client.get(f"/api/c/{claim['token']}").json()["answers"]
    assert len(answers["cause_other"]) <= 200
    assert "drivable_other" not in answers
    assert "bogus_other" not in answers


def test_a_value_outside_the_list_is_still_refused(client):
    claim = open_claim(client)
    save(client, claim["token"], {"loss_type": "vehicle_damage", "cause": "Aliens"})
    answers = client.get(f"/api/c/{claim['token']}").json()["answers"]
    assert "cause" not in answers


def test_a_multiselect_keeps_its_ordinary_choices_next_to_the_escape(client):
    claim = open_claim(client)
    save(client, claim["token"], {"loss_type": "vandalism", "vandalism_kind": ["Scratched or keyed", "Something else"],
                                  "vandalism_kind_other": "Paint thrown on the roof"})
    answers = client.get(f"/api/c/{claim['token']}").json()["answers"]
    assert answers["vandalism_kind"] == ["Scratched or keyed", "Something else"]
    assert answers["vandalism_kind_other"] == "Paint thrown on the roof"


def test_another_damaged_part_is_a_free_optional_line(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], DAMAGE_ANSWERS)
    f = field_of(layout, "damaged_other")
    assert f is not None and f["required"] is False
    assert "Another part" in f["label"]
    assert f["label"] not in layout["missing"]
    save(client, claim["token"], DAMAGE_ANSWERS | {"damaged_other": "The tow hook"})
    answers = client.get(f"/api/c/{claim['token']}").json()["answers"]
    assert answers["damaged_other"] == "The tow hook"
    assert answers["damaged_areas"] == ["Rear bumper"]  # the area list the checks read is unchanged


def test_a_claim_with_its_own_words_submits_and_keeps_the_event_type(client):
    from test_loss_notice import capture_required, detail

    claim = open_claim(client)
    layout = save(client, claim["token"], DAMAGE_ANSWERS | {"cause": "Scrape or other",
                                                              "cause_other": "A cart rolled into the door"})
    capture_required(client, claim["token"], layout)
    state = client.get(f"/api/c/{claim['token']}").json()
    assert state["layout"]["complete"] is True, state["layout"]["missing"]
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 200
    d = detail(client, claim)
    assert d["facts"]["loss_event_type"]["value"] == "SINGLE_VEHICLE_COLLISION"
