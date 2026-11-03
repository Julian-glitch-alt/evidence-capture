"""T57: System health shows the model accuracy measured by the E1 set (counts and intervals only)."""

import json

from evidence_server import evaluation
from evidence_server.store import ACCURACY_FILE


def test_system_health_carries_the_measured_accuracy(client):
    accuracy = client.get("/api/admin/health").json()["accuracy"]
    assert accuracy == json.loads(ACCURACY_FILE.read_text())
    assert accuracy["measured_on"] and accuracy["model"] and accuracy["set"]["photos"] > 0
    ids = [r["id"] for r in accuracy["rows"]]
    assert {"plate", "vin", "odometer", "retake_caught", "false_damage", "planted_caught"} <= set(ids)
    for r in accuracy["rows"]:
        assert 0 <= r["hits"] <= r["n"] and r["ci"] == evaluation.wilson(r["hits"], r["n"])


def test_the_accuracy_file_holds_no_plate_vin_or_file_name():
    text = ACCURACY_FILE.read_text()
    assert ".jpg" not in text and "DEMO101" not in text and "1HGCV1F39LA526018" not in text
