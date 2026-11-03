def test_policy_picker_lists_seeded_policies(client):
    policies = client.get("/api/admin/policies").json()
    by_no = {p["policy_no"]: p for p in policies}
    assert set(by_no) == {"DEMO-AUTO-1001", "DEMO-AUTO-1002", "DEMO-AUTO-1003", "DEMO-AUTO-1004"}
    assert by_no["DEMO-AUTO-1001"]["holder"] == "Jordan Avery"
    assert by_no["DEMO-AUTO-1001"]["vehicle"] == "2020 Honda Accord"
    assert by_no["DEMO-AUTO-1001"]["state"] == "CA"


def test_opened_claim_shows_policy_and_timeline(client):
    claim = client.post("/api/admin/claims", json={"policy_number": "demo-auto-1002"}).json()
    detail = client.get(f"/api/admin/claims/{claim['id']}").json()

    assert detail["policy"]["verified"] is True
    assert detail["policy"]["policy_no"] == "DEMO-AUTO-1002"
    [opened] = detail["events"]
    assert (opened["type"], opened["actor"], opened["to_status"]) == ("claim.opened", "user", "opened")


def test_claim_list_shows_policy_holder_and_verification(client):
    client.post("/api/admin/claims", json={"policy_number": "DEMO-AUTO-1001"})
    client.post("/api/admin/claims", json={"policy_number": "UNKNOWN-1"})
    rows = {c["policy_number"]: c for c in client.get("/api/admin/claims").json()}

    assert rows["DEMO-AUTO-1001"]["holder"] == "Jordan Avery"
    assert rows["DEMO-AUTO-1001"]["policy_verified"] is True
    assert rows["UNKNOWN-1"]["holder"] is None
    assert rows["UNKNOWN-1"]["policy_verified"] is False


def test_claim_needs_a_policy_number(client):
    r = client.post("/api/admin/claims", json={})
    assert r.status_code == 400
