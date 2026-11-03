"""Track 1: the E1 / E2 evaluation harness on the synthetic fixture, with recorded (fake) model answers.

The numbers asserted here were worked out by hand from server/eval/fixtures/labels.json
and replay.json; they test the harness, not the model.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import psycopg
import pytest

from evidence_server import evaluation
from evidence_server.db import Database
from evidence_server.meta_client import MetaClient
from pg import TEST_DATABASE_URL, reset

FIXTURES = Path(__file__).resolve().parents[1] / "eval" / "fixtures"
NOW = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def db():
    admin = psycopg.connect(TEST_DATABASE_URL, autocommit=True)
    try:
        reset(admin)
    finally:
        admin.close()
    database = Database(TEST_DATABASE_URL)
    yield database
    database.close()


@pytest.fixture
def labels():
    return evaluation.load_labels(FIXTURES / "labels.json")


def replay_meta(db, labels, **kw):
    replay = json.loads((FIXTURES / "replay.json").read_text())
    return MetaClient(db, evaluation.ReplayTransport(labels, replay), **kw)


@pytest.fixture
def result(db, labels):
    return evaluation.run(labels, replay_meta(db, labels), run_id="t1", now=NOW, today=date(2026, 11, 1))


def pr(m):
    """The counts and point rates of a precision / recall entry (the intervals are tested separately)."""
    return {k: m[k] for k in ("tp", "fp", "fn", "precision", "recall")}


def test_fixture_sizes(labels):
    assert len(labels["photos"]) == 16
    assert 6 <= len(labels["claims"]) <= 10


def test_reads_exact_match_per_field(result):
    r = result["reads"]
    assert (r["plate"]["labelled"], r["plate"]["read"], r["plate"]["exact"]) == (3, 2, 1)
    assert (r["vin"]["labelled"], r["vin"]["read"], r["vin"]["exact"]) == (3, 2, 2)
    assert (r["odometer"]["labelled"], r["odometer"]["read"], r["odometer"]["exact"]) == (2, 2, 1)
    assert r["plate"]["exact_rate"] == 0.3333 and r["plate"]["accuracy_when_read"] == 0.5
    assert {m["id"]: m["reason_code"] for m in r["plate"]["misses"]} == {"p02": "PLATE_MISMATCH", "p03": "PHOTO_UNREADABLE"}
    assert r["vin"]["misses"][0]["reason_code"] == "PHOTO_READS_DISAGREE"
    assert r["odometer"]["misses"][0]["got"] == 412306


def test_a_vin_read_right_but_not_on_the_policy_still_counts_as_read_right(result):
    p05 = next(p for p in result["photos"] if p["id"] == "p05")
    assert p05["check"]["result"] == "retake" and p05["check"]["reason_code"] == "VIN_MISMATCH"
    assert all(m["id"] != "p05" for m in result["reads"]["vin"]["misses"])


def test_damage_precision_recall_per_area(result):
    d = result["damage"]
    assert (d["labelled"], d["read"]) == (8, 7)
    assert pr(d["per_area"]["Front bumper"]) == {"tp": 1, "fp": 1, "fn": 0, "precision": 0.5, "recall": 1.0}
    assert pr(d["per_area"]["Trunk or tailgate"]) == {"tp": 0, "fp": 0, "fn": 1, "precision": None, "recall": 0.0}
    assert d["per_area"]["Trunk or tailgate"]["precision_ci"] is None  # 0/0: no interval, not [0, 0]
    assert pr(d["overall_strict"]) == {"tp": 5, "fp": 2, "fn": 1, "precision": 0.7143, "recall": 0.8333}
    assert pr(d["overall_adjacent"]) == {"tp": 6, "fp": 1, "fn": 0, "precision": 0.8571, "recall": 1.0}
    assert d["type_agreement"]["rate"] == 1.0
    assert (d["severity_agreement"]["hits"], d["severity_agreement"]["of"]) == (4, 5)
    assert d["age_agreement"]["rate"] == 1.0


def test_false_damage_on_undamaged_cars(result):
    assert result["damage"]["undamaged"] == {"photos": 2, "with_false_damage": 1, "rate": 0.5, "ci": [0.0945, 0.9055],
                                             "ids": ["p13"]}


def test_a_failed_reading_is_missing_not_zero(result):
    d = result["damage"]
    assert [x["id"] for x in d["not_read"]] == ["p16"]
    assert "Rear left" not in d["per_area"]  # neither a miss nor a hit


def test_photo_check_parts_before_signing(result):
    p = result["photo_check_parts"]
    assert p["photos"] == 8 and pr(p["overall_strict"]) == {"tp": 7, "fp": 1, "fn": 0, "precision": 0.875, "recall": 1.0}


def test_e2_flag_precision_and_recall(result):
    e = result["e2"]
    assert pr(e["overall"]) == {"tp": 12, "fp": 1, "fn": 1, "precision": 0.9231, "recall": 0.9231}
    assert pr(e["per_code"]["DAMAGE_AREA_MISMATCH"]) == {"tp": 3, "fp": 1, "fn": 0, "precision": 0.75, "recall": 1.0}
    assert e["per_code"]["STORY_INCONSISTENT"]["fn"] == 1
    assert (e["exact_set"]["hits"], e["exact_set"]["of"]) == (6, 8)
    assert (e["lane"]["hits"], e["lane"]["of"]) == (7, 7)
    claims = {k["id"]: k for k in result["claims"]}
    assert claims["e2-01"]["predicted_flags"] == []
    assert claims["e2-02"]["lane"] == "adjuster_review"  # one root cause (where the damage is) no longer reaches SIU
    assert claims["e2-03"]["extra"] == ["DAMAGE_AREA_MISMATCH"]
    assert claims["e2-07"]["missed"] == ["STORY_INCONSISTENT"]
    assert claims["e2-08"]["damage_status"] == "failed" and claims["e2-08"]["story"] is None


def test_cost_and_latency_come_from_the_call_log(result):
    c = result["cost"]["total"]
    assert (c["calls"], c["sent"], c["cached"], c["failed"]) == (34, 34, 0, 1)
    assert c["cost_usd_sent"] == pytest.approx(33 * (1000 * 1.25 + 200 * 4.25) / 1e6, abs=1e-4)
    assert result["cost"]["by_purpose"]["photo.vin_photo"]["calls"] == 6
    assert result["cost"]["errors"] == {"RuntimeError: replay: no recorded answer": 1}


def test_a_second_run_is_served_from_the_cache_with_the_same_metrics(db, labels, result):
    again = evaluation.run(labels, replay_meta(db, labels), run_id="t2", now=NOW, today=date(2026, 11, 1))
    for key in ("reads", "damage", "photo_check_parts", "e2"):
        assert again[key] == result[key]
    c = again["cost"]["total"]
    assert (c["sent"], c["cached"], c["cost_usd_sent"]) == (1, 33, 0)  # only the failed call is sent again
    assert c["cost_usd_uncached"] == result["cost"]["total"]["cost_usd_uncached"]


def test_the_budget_cap_applies_to_eval_runs(db, labels):
    meta = replay_meta(db, labels, claim_budget_usd=None, day_budget_usd=0.01)
    out = evaluation.run(labels, meta, run_id="t3", now=NOW, today=date(2026, 11, 1))
    assert out["cost"]["budget_blocked"] > 0
    assert out["cost"]["total"]["cost_usd_sent"] <= 0.01 + 0.0021 * 4  # at most the calls already in flight


def test_markdown_has_every_table(result):
    md = evaluation.markdown(result)
    for heading in ("## E1: reads", "## E1: damage reading", "## E2: flags", "## Cost and latency"):
        assert heading in md
    assert "| vin | 3 | 2 | 2 | 2/3 = 0.67 [0.21, 0.94] | 2/2 = 1.00 [0.34, 1.00] |" in md
    assert "| **All flags** | 12 | 1 | 1 | 12/13 = 0.92 [0.67, 0.99] | 12/13 = 0.92 [0.67, 0.99] |" in md
    assert "not counted as 0" in md
    assert "Small sample (n < 30 in at least one row): the interval is wide." in md
    assert "### Detection at a fixed false-alarm rate" in md and "not calibrated" in md
    assert "| 0.01 | -1.129 | 0/1 = 0.00 [0.00, 0.79] | 6/7 = 0.86 [0.49, 0.97] |" in md
    assert "Both rules give every claim the same lane." in md
    assert "measure the harness, not the product" in evaluation.markdown({**result, "transport": "replay"})


# --- track 5: honest uncertainty ---


@pytest.mark.parametrize("hits, n, interval", [
    (36, 40, [0.7695, 0.9604]),  # the brief's example: 0.90 [0.77, 0.96]
    (0, 10, [0.0, 0.2775]),  # 0/n: not [0, 0]
    (10, 10, [0.7225, 1.0]),  # n/n: not [1, 1]
    (1, 2, [0.0945, 0.9055]),
    (0, 1, [0.0, 0.7935]),
])
def test_wilson_interval_hand_worked(hits, n, interval):
    assert evaluation.wilson(hits, n) == interval


def test_wilson_with_no_sample_is_none():
    assert evaluation.wilson(0, 0) is None


def test_every_rate_carries_its_interval(result):
    r = result["reads"]["vin"]
    assert (r["exact_ci"], r["accuracy_when_read_ci"]) == ([0.2077, 0.9385], [0.3424, 1.0])
    assert result["e2"]["overall"]["precision_ci"] == evaluation.wilson(12, 13)
    assert result["e2"]["exact_set"] == {"hits": 6, "of": 8, "rate": 0.75, "ci": evaluation.wilson(6, 8)}
    assert result["damage"]["severity_agreement"]["ci"] == evaluation.wilson(4, 5)


@pytest.mark.parametrize("only_a, only_b, p", [(1, 9, 0.021484), (9, 1, 0.021484), (0, 5, 0.0625), (3, 3, 1.0), (0, 0, 1.0)])
def test_mcnemar_exact_hand_worked(only_a, only_b, p):
    # (1, 9): 2 * (C(10,0) + C(10,1)) / 2^10 = 22 / 1024
    assert evaluation.mcnemar_exact(only_a, only_b) == p


def test_paired_counts_only_items_in_both():
    a = {"p1": True, "p2": True, "p3": False, "p4": False, "p5": True}
    b = {"p1": True, "p2": False, "p3": True, "p4": False, "p6": True}
    m = evaluation.paired(a, b)
    assert (m["pairs"], m["both_right"], m["only_a"], m["only_b"], m["both_wrong"]) == (4, 1, 1, 1, 1)
    assert m["p_value"] == 1.0 and (m["a"]["hits"], m["b"]["hits"]) == (2, 2)


def test_compare_reads_of_a_run_with_itself_has_no_discordant_pairs(result):
    m = evaluation.compare_reads(result, result)
    assert (m["vin"]["pairs"], m["vin"]["only_a"], m["vin"]["only_b"], m["vin"]["p_value"]) == (3, 0, 0, 1.0)


# --- track 5: detection at a fixed false-alarm rate ---


def test_detection_at_false_alarm_picks_the_lowest_threshold_that_fits():
    clean = [0.0] * 19 + [1.0]
    planted = [0.5, 1.0, 1.5, 0.0]
    d = evaluation.detection_at_false_alarm(clean, planted)
    at = d["at_false_alarm"]
    assert (at["0.01"]["threshold"], at["0.01"]["false_alarm"]["hits"], at["0.01"]["detection"]["hits"]) == (1.5, 0, 1)
    assert (at["0.05"]["threshold"], at["0.05"]["false_alarm"]["hits"], at["0.05"]["detection"]["hits"]) == (0.5, 1, 3)
    assert at["0.10"]["threshold"] == 0.5
    assert [c["threshold"] for c in d["curve"]] == [0.0, 0.5, 1.0, 1.5, None]
    assert d["curve"][-1]["detection"]["hits"] == 0  # None flags nothing


def test_detection_without_clean_claims_is_not_made_up():
    d = evaluation.detection_at_false_alarm([], [0.2, 0.4])
    assert d["at_false_alarm"] == {"0.01": None, "0.05": None, "0.10": None}


def test_e2_reports_both_lane_rules_and_the_operating_point(result):
    e = result["e2"]
    assert result["lane_rule"] == "cost"
    assert e["lane_by_rule"]["cost"]["hits"] == e["lane_by_rule"]["count"]["hits"] == 7
    assert e["lane_changes"] == []  # fixture claims are cheap: the rules agree
    op = e["operating_point"]["cost"]
    assert (op["planted_reaching_a_person"]["hits"], op["planted_reaching_a_person"]["of"]) == (7, 7)
    assert (op["clean_reaching_a_person"]["hits"], op["clean_reaching_a_person"]["of"]) == (0, 1)
    assert (e["detection"]["clean"], e["detection"]["planted"]) == (1, 7)
    claims = {k["id"]: k for k in result["claims"]}
    assert claims["e2-08"]["evidence"]["missing"] == ["analysis_missing"]  # not read: missing, adds nothing
    assert claims["e2-08"]["evidence"]["weight_log10"] == 0.0


@pytest.mark.parametrize("change, message", [
    (lambda l: l["photos"][0].update(slot="selfie"), "slot must be"),
    (lambda l: l["photos"][1].update(id=l["photos"][0]["id"]), "duplicate id"),
    (lambda l: l["photos"][0]["expected"].update(vin="X"), "labelled only on vin_photo"),
    (lambda l: l["photos"][6]["expected"].update(odometer="56947"), "integer"),
    (lambda l: l["photos"][8]["expected"]["damages"][0].update(area="Bonnet"), "fixed areas"),
    (lambda l: l["photos"][8]["expected"]["damages"][0].update(type="DENT"), "DAMAGE_TYPES"),
    (lambda l: l["photos"][0]["expected"].update(damages=[]), "only on damage photos"),
    (lambda l: l["claims"][0].update(photos=["nope"]), "known photo ids"),
    (lambda l: l["claims"][0].update(expected_flags="STORY_UNCLEAR"), "array of reason codes"),
    (lambda l: l["claims"][0].update(expected_lane="pay_now"), "expected_lane"),
])
def test_labels_are_checked(tmp_path, change, message):
    raw = json.loads((FIXTURES / "labels.json").read_text())
    change(raw)
    path = tmp_path / "labels.json"
    path.write_text(json.dumps(raw))
    with pytest.raises(evaluation.LabelError, match=message):
        evaluation.load_labels(path)


def test_script_writes_the_report_and_json(tmp_path, capsys, db):
    import importlib.util
    spec = importlib.util.spec_from_file_location("e1_eval", FIXTURES.parents[1] / "scripts" / "e1_eval.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    code = script.main([str(FIXTURES / "labels.json"), "--replay", str(FIXTURES / "replay.json"),
                        "--out", str(tmp_path), "--run-id", "cli", "--database-url", TEST_DATABASE_URL])
    assert code == 0
    assert "## E2: flags" in (tmp_path / "report.md").read_text()
    data = json.loads((tmp_path / "result.json").read_text())
    assert data["run_id"] == "cli" and data["e2"]["overall"]["tp"] == 12
    assert "# Evaluation run cli" in capsys.readouterr().out


def test_script_refuses_live_without_a_key(monkeypatch, capsys):
    import importlib.util
    monkeypatch.delenv("LLAMA_API_KEY", raising=False)
    spec = importlib.util.spec_from_file_location("e1_eval", FIXTURES.parents[1] / "scripts" / "e1_eval.py")
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.main([str(FIXTURES / "labels.json"), "--live", "--database-url", TEST_DATABASE_URL]) == 2
    assert "LLAMA_API_KEY" in capsys.readouterr().err


# --- track 6: SAM 3.1 agreement (opt-in) ---

CENTRE = "<0f>0<|box;x1=40;y1=40;x2=59;y2=59;w=100;h=100|><|mask;x=0;y=0;data=20,20,~zzzz|>"


def test_sam_is_off_by_default(result):
    assert result["sam"] is None and "outline.damage" not in result["cost"]["by_purpose"]
    assert "SAM 3.1" not in evaluation.markdown(result)


def test_sam_agreement_and_latency_when_asked(db, labels, result):
    replay = json.loads((FIXTURES / "replay.json").read_text())
    replay["sam"] = {"damage": CENTRE, "crack": CENTRE}  # a region mid-crop: inside Muse's box
    meta = MetaClient(db, evaluation.ReplayTransport(labels, replay))
    out = evaluation.run(labels, meta, run_id="t6", now=NOW, today=date(2026, 11, 1), sam=True)
    boxed = sum(1 for p in out["photos"] if p["damage"] and p["damage"]["status"] == "done"
                for d in p["damage"]["result"]["damages"] if d["box"])
    assert boxed > 0
    assert out["sam"] == {"boxed": boxed, "agrees": boxed, "disagrees": 0, "not_run": 0,
                          "agreement_rate": 1.0, "disagreements": []}
    assert out["cost"]["by_purpose"]["outline.damage"]["calls"] == boxed
    for key in ("reads", "damage", "photo_check_parts", "e2"):
        assert out[key] == result[key]  # SAM changes nothing else
    md = evaluation.markdown(out)
    assert "## E1: SAM 3.1 outlines" in md and "SAM latency p50 / p95 s" in md


def test_sam_finding_nothing_is_a_disagreement(db, labels):
    meta = replay_meta(db, labels)  # no SAM answers recorded: SAM finds nothing
    out = evaluation.run(labels, meta, run_id="t7", now=NOW, today=date(2026, 11, 1), sam=True)
    assert out["sam"]["agrees"] == 0 and out["sam"]["disagrees"] == out["sam"]["boxed"]
    assert out["sam"]["agreement_rate"] == 0.0


# --- T57: does the photo check ask for a retake when it should? ---


def _done(pid, slot, retake_expected, result, reason=None):
    return {"id": pid, "slot": slot, "expected": {"retake": retake_expected},
            "check": {"result": result, "reason_code": reason, "value": None, "error": None}}


def test_retake_metrics_count_caught_missed_and_false_retakes():
    done = [
        _done("blur", "plate", True, "retake", "PHOTO_BLURRY"),      # caught
        _done("dark", "odometer", True, "pass"),                      # missed
        _done("apple", "damage_closeup", True, "retake", "WRONG_SUBJECT"),
        _done("good", "plate", False, "pass"),
        _done("good2", "damage_wide", False, "retake", "PHOTO_DARK"),  # a false retake
        {"id": "unlabelled", "slot": "plate", "expected": {}, "check": {"result": "retake"}},
    ]
    m = evaluation._retake_metrics(done)
    assert (m["should_retake"], m["caught"], m["fine"], m["false_retakes"]) == (3, 2, 1, 1)
    assert m["caught_rate"] == round(2 / 3, 4) and m["false_retake_rate"] == round(1 / 2, 4)
    assert m["caught_ci"] == evaluation.wilson(2, 3) and m["false_retake_ci"] == evaluation.wilson(1, 2)
    assert [x["id"] for x in m["misses"]] == ["dark", "good2"]
    report = "\n".join(evaluation._retake_lines(m))
    assert "Asks for a retake" in report and "2/3 = 0.67" in report and "dark" in report


def test_retake_must_be_true_or_false(tmp_path):
    labels = {"version": 1, "policy": None, "answers": {}, "claims": [],
              "photos": [{"id": "p1", "path": "x.jpg", "slot": "plate", "expected": {"retake": "yes"}}]}
    (tmp_path / "labels.json").write_text(json.dumps(labels))
    with pytest.raises(evaluation.LabelError, match="retake"):
        evaluation.load_labels(tmp_path / "labels.json")
