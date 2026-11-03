"""T45 (tasks/t45-design.md): one topic per screen (data-driven), the car-parts widget, the insurer's brand."""

from test_loss_notice import open_claim, save

SCREENS = {  # field id -> screen key, for the fields the contract names
    "loss_date": "when_where", "loss_time": "when_where", "loss_street": "when_where", "loss_city": "when_where",
    "loss_state": "when_where", "description": "story", "police_contacted": "police", "vin": "vehicle",
    "drivable": "vehicle", "driver_is_holder": "driver", "damaged_areas": "parts", "prior_damage": "prior",
    "other_driver_name": "other_party", "other_plate": "other_party", "injuries": "injuries", "witnesses": "witnesses",
}


def fields(layout):
    return {f["id"]: f for s in layout["sections"] for f in s["fields"]}


def test_fields_carry_their_screen(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "collision_vehicle", "police_contacted": "Yes"})
    f = fields(layout)
    for field_id, key in SCREENS.items():
        assert f[field_id].get("screen") == key, field_id
    assert {f[x]["screen"] for x in ("police_agency", "police_report_no", "police_officer")} == {"police"}


def test_screen_titles_come_with_the_layout(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "glass"})
    titles = layout["screen_titles"]
    assert titles["when_where"] == "When and where"
    assert titles["story"] == "In your own words"
    assert titles["parts"] == "Where is the damage?"
    assert titles["police"] == "Police report"
    assert titles["prior"] == "Damage from before"
    assert titles["injuries"] == "Was anyone hurt?"
    assert titles["witnesses"] == "Anyone who saw it?"
    assert "details" not in titles  # the per-type section title is used instead ("About the glass")


def test_screen_order_comes_with_the_layout(client):
    claim = open_claim(client)
    order = save(client, claim["token"], {"loss_type": "glass"})["screen_order"]
    assert order[:3] == ["when_where", "cause", "details"]  # the type's details say what the loss was
    assert [k for k in order if k in ("story", "police", "vehicle", "parts", "prior")] == [
        "story", "police", "vehicle", "parts", "prior"]
    assert set(order) >= {"theft_seen", "theft_keys", "theft_owner", "theft_items", "contact_what", "contact_call"}


def test_type_details_share_a_screen_and_car_theft_is_split(client):
    claim = open_claim(client)
    f = fields(save(client, claim["token"], {"loss_type": "glass"}))
    assert {f[x]["screen"] for x in ("glass_which", "glass_kind", "glass_cause")} == {"details"}
    f = fields(save(client, claim["token"], {"loss_type": "vehicle_theft", "ownership": "It has a loan"}))
    assert (f["last_seen_place"]["screen"], f["last_seen_time"]["screen"]) == ("theft_seen", "theft_seen")
    assert {f[x]["screen"] for x in ("keys_count", "keys_all", "car_locked")} == {"theft_keys"}
    assert (f["ownership"]["screen"], f["lender"]["screen"]) == ("theft_owner", "theft_owner")
    assert f["items_in_car"]["screen"] == "theft_items"


def test_damaged_parts_use_the_car_widget(client):
    claim = open_claim(client)
    f = fields(save(client, claim["token"], {"loss_type": "single_vehicle"}))
    assert f["damaged_areas"]["widget"] == "car_parts"


# --- the insurer's brand ---


def test_session_state_carries_the_brand(client):
    claim = open_claim(client)
    brand = client.get(f"/api/c/{claim['token']}").json()["brand"]
    assert brand == {"name": "Demo Mutual Insurance", "color": "#1673e6"}


def test_settings_return_the_default_brand_color_until_one_is_set(client):
    assert client.get("/api/admin/settings").json()["brand_color"] == "#1673e6"


def test_a_supervisor_sets_the_brand_color(client, anon):
    r = client.put("/api/admin/settings", json={"brand_color": "#0b6e4f"})
    assert r.status_code == 200, r.text
    assert client.get("/api/admin/settings").json()["brand_color"] == "#0b6e4f"
    claim = open_claim(client)
    assert client.get(f"/api/c/{claim['token']}").json()["brand"]["color"] == "#0b6e4f"


def test_bad_brand_colors_are_refused(client, anon):
    for bad in ("blue", "#12345", "#GGGGGG", "", None, 123):
        r = client.put("/api/admin/settings", json={"brand_color": bad})
        assert r.status_code == 400, (bad, r.text)
    client.post("/api/admin/logout")  # `client` is `anon` after login
    assert anon.put("/api/admin/settings", json={"brand_color": "#000000"}).status_code == 401


def test_settings_keep_the_delivery_mode_when_only_the_brand_changes(client):
    client.put("/api/admin/settings", json={"delivery_mode": "on_decision"})
    client.put("/api/admin/settings", json={"brand_color": "#112233"})
    s = client.get("/api/admin/settings").json()
    assert (s["delivery_mode"], s["brand_color"]) == ("on_decision", "#112233")
