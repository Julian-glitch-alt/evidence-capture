"""Track 2 decision algorithms through the API (docs/explorations/algorithms.md), with a fake model."""

from test_damage_flow import captured_claim, send


def rows_of(d):
    return {c["check_id"]: c for c in d["checks"]}


# --- 3. no policy on file: never "matches the policy" ---


def test_no_policy_on_file_never_passes_plate_or_vin(client, model, clock):
    claim, _ = captured_claim(client, clock, policy="NOT-ON-FILE-1")
    d = send(client, claim)
    rows = rows_of(d)
    for check_id in ("photo.plate", "photo.vin_photo"):
        assert (rows[check_id]["result"], rows[check_id]["reason_code"]) == ("unclear", "POLICY_NOT_VERIFIED")
    assert rows["photo.plate"]["value"] == "DEMO101"  # the read is kept
    assert "No verified policy on file, so the vehicle could not be matched to a policy" in d["summary"]
    assert "match the vehicle on the policy" not in d["summary"]
    # one root cause, however many rows say it
    assert [c["cause"] for c in d["proposal"]["causes"] if c["cause"] == "policy_on_file"] == ["policy_on_file"]
    assert d["proposal"]["lane"] == "adjuster_review"


def test_the_claimant_is_not_asked_to_retake_when_there_is_no_policy(client, model, clock):
    claim, _ = captured_claim(client, clock, policy="NOT-ON-FILE-1")
    state = client.get(f"/api/c/{claim['token']}").json()
    plate = [c for c in state["photo_checks"] if c["slot_id"] == "plate"][0]
    assert (plate["result"], plate["hint"]) == ("unchecked", None)


# --- 4. coaching trail ---

from conftest import GOOD_READS, Phone  # noqa: E402
from evidence_server import report  # noqa: E402
from test_integration_api import capture, order  # noqa: E402
from test_loss_notice import DAMAGE_ANSWERS, save  # noqa: E402


def flags_by_source(d):
    return {f["source"]: f for f in d["proposal"]["flags"]}


def test_a_retaken_wrong_plate_reaches_the_adjuster_with_the_old_photo(client, model, clock):
    claim, items = captured_claim(client, clock)
    phone = Phone(client, claim["token"])
    plates = [i for i in client.get(f"/api/c/{claim['token']}").json()["items"] if i["step_id"] == "plate"]
    first = plates[0]["id"]
    # the claimant first shot another car's plate, saw "Retake this photo.", then shot the right one
    with client.app.state.store.db.tx() as conn:
        conn.execute("UPDATE photo_checks SET readings_json=%s WHERE item_id=%s",
                     ('[{"is_plate": true, "text": "8ABC123", "state": "CA", "unsure": false, "problem": "none"}]', first))
    clock.advance(seconds=1)
    from conftest import JPEG
    assert phone.upload("plate", JPEG + b"second-plate").status_code == 200
    d = send(client, claim)
    trail = flags_by_source(d)["capture.retake_trail"]
    assert (trail["level"], trail["reason_code"], trail["cause"]) == ("review", "RETAKE_AFTER_MISMATCH", "coaching")
    assert trail["evidence"] == [{"item_id": first, "slot_id": "plate", "earlier": True}]
    assert rows_of(d)["photo.plate"]["result"] == "pass"  # the current shot is fine
    assert d["proposal"]["lane"] == "adjuster_review"


def test_a_form_edit_after_the_area_hint_reaches_the_adjuster(client, model, clock):
    model.set("damage_wide", {**GOOD_READS["damage_wide"][0], "parts": ["Front left"]})
    claim, items = captured_claim(client, clock)
    save(client, claim["token"], {**DAMAGE_ANSWERS, "damaged_areas": ["Front left"]})
    d = send(client, claim)
    edits = flags_by_source(d)["capture.hint_edits"]
    assert edits["reason_code"] == "ANSWER_CHANGED_AFTER_HINT"
    assert edits["evidence"] == [{"item_id": items["damage_wide"][0]["id"], "slot_id": "damage_wide", "earlier": True}]


# --- 5. screen or print ---


def test_a_photo_of_a_screen_is_a_review_finding_and_never_blocks(client, model, clock):
    model.set("plate", {**GOOD_READS["plate"][0], "screen_or_print": "yes"})
    claim, _ = captured_claim(client, clock)
    plate = [c for c in client.get(f"/api/c/{claim['token']}").json()["photo_checks"] if c["slot_id"] == "plate"][0]
    assert plate["result"] == "pass"  # the claimant goes on
    d = send(client, claim)
    screen = flags_by_source(d)["capture.screen"]
    assert (screen["level"], screen["reason_code"], screen["model_only"]) == ("review", "SCREEN_RECAPTURE_SUSPECTED", True)
    assert screen["evidence"][0]["slot_id"] == "plate"
    assert report.CHECK_TITLES["capture.screen"] == "Photo of a screen or print"


def test_a_screen_photo_plus_one_code_conflict_is_not_siu(client, model, clock):
    model.set("plate", {**GOOD_READS["plate"][0], "text": "8ABC123", "screen_or_print": "yes"})
    claim, _ = captured_claim(client, clock)
    assert send(client, claim)["proposal"]["lane"] == "adjuster_review"


# --- 6. first notice vs statement ---


def test_the_first_notice_from_the_capture_request_is_compared(client, model, clock):
    c = order(client, loss={"date": "2026-10-20", "cause": "collision", "description": "Insured was rear-ended."}).json()
    token = c["capture_url"].rsplit("/", 1)[1]
    capture(client, clock, token)
    d = client.get(f"/api/admin/claims/{c['case_id']}").json()
    fnol = flags_by_source(d)["fnol.statement"]
    assert (fnol["level"], fnol["reason_code"], fnol["cause"]) == ("review", "FNOL_MISMATCH", "fnol_statement")
    row = rows_of(d)["fnol.statement"]
    assert row["value"]["differences"] == [{"field": "loss date", "fnol": "2026-10-20", "statement": "2026-10-27"}]
    assert row["value"]["fnol"]["description"] == "Insured was rear-ended."


def test_a_matching_first_notice_is_a_pass_row(client, model, clock):
    c = order(client).json()  # date 2026-10-27, as the claimant answers
    capture(client, clock, c["capture_url"].rsplit("/", 1)[1])
    d = client.get(f"/api/admin/claims/{c['case_id']}").json()
    assert rows_of(d)["fnol.statement"]["result"] == "pass"
    assert "fnol.statement" not in flags_by_source(d)


def test_a_claim_opened_by_hand_has_no_first_notice_row(client, model, clock):
    claim, _ = captured_claim(client, clock)
    assert "fnol.statement" not in rows_of(send(client, claim))
