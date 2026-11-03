"""T29a report data, note, activity, package and PDF, pure (contract: tasks/t29a-design.md)."""

import io
import json
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest
from PIL import Image

from evidence_server import report, report_pdf

NY = ZoneInfo("America/New_York")
FRIDAY = datetime(2026, 10, 30, 14, 0, tzinfo=UTC)  # 10:00 AM in New York


def proposal(lane="fast_track", flags=None, notes=None, estimate=None, deductible=500, reserve=1200):
    labels = {"siu_referral": "Refer to SIU", "adjuster_review": "Adjuster review",
              "fast_track": "Fast track", "standard": "Standard"}
    return {
        "status": "ready", "version": "proposal/v1", "lane": lane, "label": labels[lane], "rule": "R3_FAST",
        "headline": "All checks passed; likely cost up to $2,050.",
        "reasons": [f["reason"] for f in (flags or [])][:3] or ["All checks passed."],
        "flags": flags or [], "notes": notes or [],
        "estimate": estimate or {"status": "complete", "full": [600, 2800], "likely": [1350, 2050], "midpoint": 1700,
                                 "basis": "PERT 80% interval from public repair-cost ballparks.", "priced": [],
                                 "unpriced": [], "excluded_old": [], "version": "estimate/v1"},
        "deductible": deductible, "suggested_reserve": reserve,
        "next_step": "Send to estimating for the final estimate; payment needs a person's approval.",
    }


def detail(**kw):
    return {
        "id": "CLM-20261030-0001", "token": "CLAIMANT-LINK-TOKEN-must-never-appear", "status": "submitted",
        "policy": {"policy_no": "DEMO-AUTO-1001", "verified": True, "data": {
            "policy_no": "DEMO-AUTO-1001",
            "holder": {"name": "Jordan Avery"},
            "vehicles": [{"vin": "1HGCV1F39LA526018", "plate": "DEMO101", "year": 2020, "make": "Honda", "model": "Accord"}],
        }},
        "answers": {"loss_date": "2026-10-29", "cause": "collision", "description": "Backed into a bollard."},
        "checks": [
            {"check_id": "coverage.in_force", "result": "pass", "reason_code": "OK", "reason": "Policy active on the loss date."},
            {"check_id": "photo.vin_photo", "result": "pass", "reason_code": "OK", "reason": "VIN matches the policy."},
            {"check_id": "no.such_check", "result": "value", "reason_code": "X", "reason": "Something."},
        ],
        "damage": {"status": "done", "damages": [
            {"item_id": "it_close", "slot_id": "damage_closeup", "area": "Rear bumper", "damage_type": "BUMPER_DAMAGE",
             "severity": "moderate", "age": "new", "box": [200, 450, 800, 700], "observed": "Pushed in"},
            {"item_id": "it_wide", "slot_id": "damage_wide", "area": "Rear bumper", "damage_type": "BUMPER_DAMAGE",
             "severity": "moderate", "age": "new", "box": [100, 500, 600, 800], "observed": "Pushed in"},
            {"item_id": "it_wide", "slot_id": "damage_wide", "area": "Hood", "damage_type": "SCRATCH",
             "severity": "minor", "age": "old", "box": None, "observed": "Faded scratch"},
        ]},
        "proposal": kw.get("proposal") or proposal(),
        "seal": {"sealed_at": "2026-10-30T13:55:00+00:00", "key_id": "key-1"},
        "events": [
            {"type": "claim.submitted", "data": {}, "hash": "a" * 64},
            {"type": "claim.sealed", "data": {"manifest_sha256": "f" * 64, "key_id": "key-1", "items": 7}, "hash": "b" * 64},
        ],
        "insurer_claim_number": kw.get("insurer_claim_number"),
    }


def build(**kw):
    return report.build(detail(**kw), "Demo Mutual Insurance", FRIDAY, NY)


REVIEW_FLAG = {"source": "consistency.story", "level": "conflict", "reason_code": "STORY_DOES_NOT_FIT",
               "reason": "The statement does not fit the damage in the photos.",
               "evidence": [{"item_id": "it_wide", "slot_id": "damage_wide", "number": 1}]}
SLOT_FLAG = {"source": "photo.odometer", "level": "review", "reason_code": "UNREADABLE", "reason": "Odometer unreadable.",
             "evidence": [{"item_id": "it_odo", "slot_id": "odometer"}]}


# --- build ---


def test_report_carries_the_claim_policy_and_vehicle():
    r = build(insurer_claim_number="DM-2026-0412")
    assert (r["version"], r["org"], r["case_id"], r["insurer_claim_number"]) == (
        report.VERSION, "Demo Mutual Insurance", "CLM-20261030-0001", "DM-2026-0412")
    assert (r["policy_number"], r["insured"]) == ("DEMO-AUTO-1001", "Jordan Avery")
    assert r["vehicle"] == {"year": 2020, "make": "Honda", "model": "Accord", "vin": "1HGCV1F39LA526018", "plate": "DEMO101"}
    assert r["loss"] == {"date": "2026-10-29", "type": "collision", "description": "Backed into a bollard."}


def test_insurer_claim_number_is_none_until_t16():
    assert build()["insurer_claim_number"] is None


def test_report_takes_the_recommendation_from_the_proposal():
    r = build()
    assert r["recommendation"] == {
        "lane": "fast_track", "label": "Fast track", "headline": "All checks passed; likely cost up to $2,050.",
        "reasons": ["All checks passed."],
        "next_step": "Send to estimating for the final estimate; payment needs a person's approval.",
    }
    assert r["cost"] == {"status": "complete", "likely": [1350, 2050], "full": [600, 2800],
                         "basis": "PERT 80% interval from public repair-cost ballparks.",
                         "deductible": 500, "suggested_reserve": 1200}


def test_evidence_becomes_labels_the_adjuster_can_find():
    r = build(proposal=proposal("adjuster_review", flags=[REVIEW_FLAG, SLOT_FLAG]))
    assert [f["evidence"] for f in r["flags"]] == [["Wide #1"], ["Odometer photo"]]
    assert [(f["level"], f["reason_code"]) for f in r["flags"]] == [("conflict", "STORY_DOES_NOT_FIT"), ("review", "UNREADABLE")]


def test_damages_are_numbered_per_photo_and_photos_listed_once():
    r = build()
    assert [d["label"] for d in r["damages"]] == ["Close-up #1", "Wide #1", "Wide #2"]
    assert r["damages"][2]["age"] == "old"
    assert r["photos"] == [{"item_id": "it_close", "slot_id": "damage_closeup", "label": "Close-up photo"},
                           {"item_id": "it_wide", "slot_id": "damage_wide", "label": "Wide photo"}]


def test_the_damage_list_counts_each_physical_damage_once():
    """The close-up and the wide photo both show the rear bumper dent: one entry, both photos named."""
    r = build()
    assert [(d["n"], d["labels"], d["area"]) for d in r["damage_list"]] == [
        (1, ["Close-up #1", "Wide #1"], "Rear bumper"), (2, ["Wide #2"], "Hood")]
    assert r["damage_list"][0]["keys"] == ["it_close:1", "it_wide:1"]
    assert len(r["damages"]) == 3  # per-photo boxes stay for display


def test_every_check_row_is_listed_with_a_title():
    checks = build()["checks"]
    assert [c["check_id"] for c in checks] == ["coverage.in_force", "photo.vin_photo", "no.such_check"]
    assert checks[0]["title"] != "coverage.in_force"  # a known id gets a plain title
    assert checks[2]["title"] == "no.such_check"  # an unknown id keeps its id


def test_seal_and_chain_come_from_the_log():
    r = build()
    assert r["seal"] == {"sealed_at": "2026-10-30T13:55:00+00:00", "key_id": "key-1", "manifest_sha256": "f" * 64, "items": 7}
    assert r["chain"] == {"events": 2, "head": "b" * 64}


def test_build_is_deterministic_and_leaks_no_claimant_token():
    assert build() == build()
    assert "CLAIMANT-LINK-TOKEN" not in json.dumps(build())


# --- note and activity ---


def test_note_is_three_short_lines_from_code():
    lines = report.note(build())["body"].split("\n")
    assert len(lines) == 3
    assert lines[0].startswith("Verified capture complete: 7 items sealed")
    assert "Recommendation: Fast track." in lines[0]
    assert lines[1] == "Findings: All checks passed."
    assert lines[2] == ("Rough range $1,350-$2,050 (public ballpark, not an estimate); "
                        "suggested initial reserve $1,200. See verified-evidence-report.pdf.")
    assert report.note(build())["subject"] == "Verified evidence capture"


def test_note_drops_range_and_reserve_when_there_are_none():
    none = {"status": "none", "full": None, "likely": None, "midpoint": None, "basis": "x", "priced": [],
            "unpriced": [], "excluded_old": []}
    body = report.note(build(proposal=proposal("adjuster_review", flags=[SLOT_FLAG], estimate=none, reserve=None)))["body"]
    assert body.split("\n")[1] == "Findings: Odometer unreadable (Odometer photo)."
    assert body.split("\n")[2] == "See verified-evidence-report.pdf."


def test_note_findings_are_distinct_and_name_the_photo():  # T40 V8
    same = {"source": "photo.plate", "level": "review", "reason_code": "PHOTO_UNCHECKED",
            "reason": "The photo could not be checked automatically.", "evidence": [{"item_id": "p", "slot_id": "plate"}]}
    other = {**same, "source": "photo.odometer", "evidence": [{"item_id": "o", "slot_id": "odometer"}]}
    body = report.note(build(proposal=proposal("adjuster_review", flags=[same, dict(same), other])))["body"]
    assert body.split("\n")[1] == ("Findings: The photo could not be checked automatically (Plate photo); "
                                    "The photo could not be checked automatically (Odometer photo).")


@pytest.mark.parametrize("lane, flags, subject, priority", [
    ("siu_referral", [REVIEW_FLAG, REVIEW_FLAG], "Review verified evidence: SIU screening suggested", "high"),
    ("adjuster_review", [REVIEW_FLAG, SLOT_FLAG], "Review verified evidence: 2 finding(s)", "high"),
    ("fast_track", [], "Review verified evidence: fast track suggested", "normal"),
    ("standard", [], "Review verified evidence", "normal"),
])
def test_activity_per_lane(lane, flags, subject, priority):
    a = report.activity(build(proposal=proposal(lane, flags=flags)))
    assert (a["subject"], a["priority"]) == (subject, priority)
    assert a["description"] == proposal(lane)["next_step"]
    assert a["due_date"] == "2026-11-03"  # Friday + 2 business days = Tuesday


def test_business_days_skip_the_weekend():
    assert report.business_days_after(date(2026, 10, 30), 2) == date(2026, 11, 3)
    assert report.business_days_after(date(2026, 10, 28), 2) == date(2026, 10, 30)
    assert report.business_days_after(date(2026, 10, 31), 1) == date(2026, 11, 2)


@pytest.mark.parametrize("lane", ["siu_referral", "adjuster_review", "fast_track", "standard"])
def test_wording_never_decides(lane):
    r = build(proposal=proposal(lane, flags=[REVIEW_FLAG] if lane != "fast_track" else []))
    text = (report.note(r)["body"] + report.activity(r)["subject"] + report.activity(r)["description"]).lower()
    for word in ("approved", "denied", "deny", "fraud", "rejected"):
        assert word not in text


# --- package ---


def test_package_uses_the_insurers_nouns():
    r = build(insurer_claim_number="DM-2026-0412")
    p = report.package(r, "evt_1", "c" * 64, "/api/r/TOKEN", "/api/r/TOKEN/package")
    assert (p["schema"], p["event"], p["event_id"], p["occurred_at"]) == (
        report.PACKAGE_SCHEMA, "evidence.ready", "evt_1", r["generated_at"])
    assert p["claim"] == {"case_id": "CLM-20261030-0001", "insurer_claim_number": "DM-2026-0412",
                          "policy_number": "DEMO-AUTO-1001", "vin": "1HGCV1F39LA526018"}
    assert p["documents"][0] == {"name": "verified-evidence-report.pdf", "document_type": "evidence_report",
                                 "mime": "application/pdf", "url": "/api/r/TOKEN", "sha256": "c" * 64}
    assert (p["documents"][1]["document_type"], p["documents"][1]["url"]) == ("sealed_evidence", "/api/r/TOKEN/package")
    assert p["notes"] == [report.note(r)] and p["activities"] == [report.activity(r)]
    assert p["recommendation"]["suggested_reserve"] == 1200
    assert p["recommendation"]["estimate"]["likely"] == [1350, 2050]
    assert p["seal"] == r["seal"] and p["report_version"] == report.VERSION
    json.dumps(p)  # serialisable as is


# --- signature ---


def test_signature_verifies_only_with_the_right_secret_and_body():
    body = b'{"event":"evidence.ready"}'
    header = report.sign(body, b"secret", 1761830000)
    assert header.startswith("t=1761830000,v1=")
    assert report.verify_signature(body, b"secret", header)
    assert not report.verify_signature(body, b"wrong", header)
    assert not report.verify_signature(body + b" ", b"secret", header)
    for bad in ("", "garbage", "t=1,v1=", "v1=abc"):
        assert not report.verify_signature(body, b"secret", bad)


# --- PDF ---


def photo(color=(180, 40, 40)):
    out = io.BytesIO()
    Image.new("RGB", (640, 480), color).save(out, "JPEG")
    return out.getvalue()


def test_latin1_replaces_typography_and_drops_the_rest():
    assert report.latin1("$1,350–$2,050 · “ok” … ✓ ✗ → x") == '$1,350-$2,050 . "ok" ... [ok] [x] -> x'
    assert report.latin1("café 日本") .encode("latin-1")


def test_pdf_renders_with_photos_and_boxes():
    data = report_pdf.render(build(), {"it_close": photo(), "it_wide": photo((40, 40, 180))})
    assert data.startswith(b"%PDF") and len(data) > 2000


def test_pdf_survives_missing_or_broken_photos_and_odd_text():
    r = build(proposal=proposal("adjuster_review", flags=[{**REVIEW_FLAG, "reason": "Story “doesn’t” fit – see 日本 ✓"}]))
    r["loss"]["description"] = "Line one\nline two — with emoji 🚗 and a very long word " + "x" * 300
    data = report_pdf.render(r, {"it_close": b"not an image"})
    assert data.startswith(b"%PDF")


def test_pdf_damage_list_names_every_photo_once_per_damage(monkeypatch):
    lines = []
    monkeypatch.setattr(report_pdf, "_p", lambda pdf, text, *a, **kw: lines.append(text))
    report_pdf.render(build(), {})
    listed = [t for t in lines if t[:3] in ("1. ", "2. ", "3. ")]
    assert listed[0].startswith("1. Close-up #1, Wide #1: Rear bumper")
    assert listed[1].startswith("2. Wide #2: Hood")
    assert not any(t.startswith("3. ") for t in listed)


# --- track 6: SAM 3.1 outlines on the damage photos ---

import base64  # noqa: E402

from evidence_server import outline  # noqa: E402


def _ring_overlay() -> str:
    mask = bytes(1 if 2 <= x <= 17 and 2 <= y <= 17 else 0 for y in range(20) for x in range(20))
    return outline.overlay_png(mask, 20, 20)


def outlined(sam_agrees="yes", regions=None, area=None):
    d = detail()
    d["damage"]["damages"][0]["outline"] = {
        "status": "done", "sam_agrees": sam_agrees, "concept": "damage", "error": None,
        "regions": regions if regions is not None else [{"box": [300, 500, 700, 650], "mask": "decoded",
                                                          "overlay": _ring_overlay()}],
        "area": area or {"photo_pct": 3.2, "panel_pct": 14.0, "basis": "mask", "panel": "bumper"},
        "call_id": "c1", "crop": [0, 0, 1, 1], "photo_size": [200, 100], "found": 1}
    return report.build(d, "Demo Mutual Insurance", FRIDAY, NY)


def test_report_damages_carry_the_outline_or_none():
    r = outlined()
    assert r["damages"][0]["outline"]["sam_agrees"] == "yes"
    assert r["damages"][0]["outline"]["regions"][0]["box"] == [300, 500, 700, 650]
    assert r["damages"][1]["outline"] is None


def test_photo_has_the_sam_outline_drawn_under_muses_box():
    r = outlined()
    drawn, w, h = report_pdf._drawn(r["photos"][0], photo(), r["damages"])
    img = Image.open(io.BytesIO(drawn)).convert("RGB")
    # The ring's left edge is mask column 2 of 20 across the region 300-700 (of 1000) of the width.
    edge = img.getpixel((int((0.3 + 0.4 * 2.5 / 20) * w), int(0.575 * h)))
    assert edge[2] > 180 and edge[0] < 90  # the outline colour, not the photo's red
    plain, _, _ = report_pdf._drawn(r["photos"][0], photo(), build()["damages"])
    assert plain != drawn


def test_undecoded_region_is_drawn_as_its_box():
    r = outlined(regions=[{"box": [300, 500, 700, 650], "mask": "not_decoded", "overlay": None}])
    drawn, w, h = report_pdf._drawn(r["photos"][0], photo(), r["damages"])
    pixel = Image.open(io.BytesIO(drawn)).convert("RGB").getpixel((int(0.5 * w), int(0.5 * h)))
    assert pixel[2] > 180 and pixel[0] < 90


def test_outline_notes_name_both_models_and_stay_factual():
    yes = report_pdf._outline_notes(outlined()["photos"][0], outlined()["damages"])
    assert yes == ["#1: outline covers about 3.2% of the photo, 14% of the bumper"]
    no = outlined("no", regions=[], area={"photo_pct": 20.0, "panel_pct": None, "basis": "box estimate"})
    assert report_pdf._outline_notes(no["photos"][0], no["damages"]) == [
        "#1: The outline model found nothing in this box."]
    missing = build()
    assert report_pdf._outline_notes(missing["photos"][0], missing["damages"]) == []  # T42: no SAM, no SAM note
    for text in yes + [outline.NOTE_DISAGREE, outline.NOTE_MISSING, outline.CAPTION]:
        assert not any(w in text.lower() for w in ("fake", "fraud", "manipulat", "tamper", "detect"))


def test_pdf_with_outlines_renders():
    data = report_pdf.render(outlined(), {"it_close": photo(), "it_wide": photo((40, 40, 180))})
    assert data.startswith(b"%PDF")
# --- track 5: evidence strength ---

EVIDENCE = {"band": "strong", "score_log10": -0.279, "weight_log10": 1.0,
            "contributions": [{"cause": "vehicle_identity", "label": "vehicle identity", "level": "conflict", "log10_lr": 0.5},
                              {"cause": "odometer", "label": "odometer", "level": "conflict", "log10_lr": 0.5}],
            "missing": [{"cause": "analysis_missing", "label": "automatic check missing", "codes": ["PHOTO_UNCHECKED"]}],
            "note": "Weights are assumptions, set in configuration."}


def test_report_carries_the_evidence_band_and_causes_but_no_number():
    r = build(proposal={**proposal(), "evidence": EVIDENCE})
    assert r["evidence"] == {"band": "strong", "causes": ["vehicle identity", "odometer"],
                             "missing": ["automatic check missing"], "note": "Weights are assumptions, set in configuration."}
    assert build()["evidence"] is None  # a proposal made before track 5


def test_pdf_evidence_line():
    r = build(proposal={**proposal(), "evidence": EVIDENCE})
    assert report_pdf.evidence_text(r) == ("Evidence strength: strong (vehicle identity, odometer). "
                                           "Not checked: automatic check missing.")
    assert report_pdf.evidence_text(build()) is None
    assert report_pdf.render(r, {}).startswith(b"%PDF")


# --- T42: SAM is off by default; no SAM wording where SAM did not run ---


def not_run_outline():
    d = detail()
    for dm in d["damage"]["damages"]:
        dm["outline"] = {"status": "not_run", "sam_agrees": "not_run", "regions": [], "error": "disabled",
                         "area": {"basis": "box estimate", "photo_pct": 2.0, "panel": None, "panel_pct": None}}
    return report.build(d, "Demo Mutual Insurance", FRIDAY, NY)


def test_no_sam_wording_when_sam_did_not_run():
    for r in (build(), not_run_outline()):
        photo0 = r["photos"][0]
        assert report_pdf._box_caption(photo0, r["damages"]) == "Yellow box: Muse Spark"
        assert report_pdf._outline_notes(photo0, r["damages"]) == []


def test_sam_wording_when_sam_ran():
    r = outlined()
    assert report_pdf._box_caption(r["photos"][0], r["damages"]) == "Yellow box: Muse Spark; blue outline: SAM 3.1"
