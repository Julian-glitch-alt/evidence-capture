"""Claimant flow fixes of 28 Oct: skipping a shot with a reason, the signature, the first screen."""

import pytest

from conftest import JPEG, PNG, Phone
from test_loss_notice import DAMAGE_ANSWERS, capture_required, detail, open_claim, save


def test_policy_is_on_the_first_screen_before_any_choice(client):
    claim = open_claim(client)
    layout = client.get(f"/api/c/{claim['token']}").json()["layout"]
    assert layout["policy"]["vehicle"] == "2020 Honda Accord"


def test_declaration_has_a_required_signature_that_cannot_be_skipped(client):
    claim = open_claim(client)
    layout = save(client, claim["token"], {"loss_type": "vehicle_damage"})
    signature = next(s for s in layout["sections"][-1]["slots"] if s["id"] == "signature")
    assert signature["kind"] == "signature" and signature["required"] and not signature["skippable"]


def test_signature_accepts_png_only(client):
    claim = open_claim(client)
    phone = Phone(client, claim["token"])
    assert phone.upload("signature", PNG, "image/png").status_code == 200
    assert phone.upload("signature", JPEG, "image/jpeg").status_code == 400


def test_skipped_shot_with_reason_no_longer_blocks(client):
    claim = open_claim(client)
    answers = {**DAMAGE_ANSWERS, "slot_skips": {"odometer": {"reason": "It's too dark", "note": "Night, no light in the car"}}}
    layout = save(client, claim["token"], answers)
    odometer = next(s for s in layout["sections"][2]["slots"] if s["id"] == "odometer")
    assert odometer["skipped"]["reason"] == "It's too dark"
    assert "Odometer" not in layout["missing"]


def test_skipped_shot_is_flagged_on_submit(client):
    claim = open_claim(client)
    answers = {**DAMAGE_ANSWERS, "slot_skips": {"odometer": {"reason": "It's too dark"}}}
    layout = save(client, claim["token"], answers)
    phone = Phone(client, claim["token"])
    for section in layout["sections"]:
        for s in section["slots"]:
            if s["required"] and s["id"] != "odometer":
                from conftest import MP4

                data, mime = {"video": (MP4, "video/mp4"), "signature": (PNG, "image/png")}.get(s["kind"], (JPEG, "image/jpeg"))
                assert phone.upload(s["id"], data, mime).status_code == 200
    assert client.post(f"/api/c/{claim['token']}/submit", json={}).status_code == 200

    d = detail(client, claim)
    assert "REQUIRED_EVIDENCE_MISSING" in d["reason_codes"]
    assert d["missing_information"] == ["Odometer: It's too dark"]


@pytest.mark.parametrize(
    "skips",
    [
        {"odometer": {"reason": "Because I said so"}},  # reason not on the list
        {"signature": {"reason": "It's too dark"}},  # signature cannot be skipped
        {"nope": {"reason": "It's too dark"}},  # unknown slot
        "not a dict",
    ],
)
def test_invalid_skips_are_ignored(client, skips):
    claim = open_claim(client)
    layout = save(client, claim["token"], {**DAMAGE_ANSWERS, "slot_skips": skips})
    slots = {s["id"]: s for sec in layout["sections"] for s in sec["slots"]}
    assert all(s["skipped"] is None for s in slots.values())
