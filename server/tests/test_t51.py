"""T51 (tasks/t51-design.md): a licence photo kept from the AI is not a failure in System health."""

from evidence_server import photo_check
from test_damage_flow import captured_claim, send
from test_ops import health
from test_photo_check_flow import photo_rows, shot, start

NOT_CHECKED = "The photo could not be checked automatically."


def licence_note(d):
    return [n for n in d["proposal"]["notes"] if n["source"] == "photo.driver_license"]


def test_the_reason_says_why_in_plain_words():
    assert photo_check.LICENSE_NOT_SENT == (
        "License photos are not sent to the AI (privacy setting). Check the license yourself.")


def test_a_licence_kept_from_the_ai_is_not_a_failure(client, model):
    claim, _, phone = start(client)
    phone.upload("driver_license", shot(1))
    h = health(client)
    assert h["photo_checks_failed"] == 0
    assert claim["id"] not in h["claims"]
    assert model.calls == []


def test_a_real_photo_check_failure_still_counts(client, model):
    claim, _, phone = start(client)
    phone.upload("driver_license", shot(1))
    model.set("plate", RuntimeError("Meta is down"))
    phone.upload("plate", shot(2))
    h = health(client)
    assert h["photo_checks_failed"] == 1
    assert claim["id"] in h["claims"]


def test_the_adjuster_reads_why_the_licence_was_not_checked(client, model, clock):
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    row = photo_rows(d)["photo.driver_license"]
    assert (row["result"], row["reason_code"], row["reason"]) == (
        "unclear", "PHOTO_UNCHECKED", photo_check.LICENSE_NOT_SENT)
    [note] = licence_note(d)
    assert (note["level"], note["reason_code"], note["reason"]) == (
        "note", "PHOTO_UNCHECKED", photo_check.LICENSE_NOT_SENT)


def test_the_claimant_view_is_unchanged(client, model):
    claim, _, phone = start(client)
    phone.upload("driver_license", shot(1))
    [entry] = client.get(f"/api/c/{claim['token']}").json()["photo_checks"]
    assert (entry["result"], entry["hint"]) == ("unchecked", None)


def test_a_licence_check_that_failed_for_another_reason_keeps_the_old_reason(client, model, clock):
    client.app.state.store.check_license = True
    model.set("driver_license", RuntimeError("Meta is down"))
    claim, _ = captured_claim(client, clock)
    d = send(client, claim)
    row = photo_rows(d)["photo.driver_license"]
    assert (row["result"], row["reason_code"], row["reason"]) == ("unclear", "PHOTO_UNCHECKED", NOT_CHECKED)
    assert health(client)["photo_checks_failed"] >= 1
