"""T42: a rejected AI summary is written again once, with the reasons it was rejected; never more."""

from conftest import GOOD_SUMMARY, SUMMARY
from test_loss_notice import detail
from test_review_flow import ready

BAD = {"sentences": [{"text": "Looks like fraud.", "refs": ["D1"]}]}


def summary_calls(model):
    return [c for c in model.calls if c["slot"] == "summary"]


def test_a_rejected_summary_is_retried_once_with_the_reasons(client, model, clock):
    model.set("summary", BAD, GOOD_SUMMARY)
    d = detail(client, ready(client, clock))
    assert d["ai_summary"]["status"] == "verified"
    assert d["ai_summary"]["attempts"] == 2
    first, second = summary_calls(model)
    assert second["prompt"].startswith(SUMMARY) and second["prompt"] != first["prompt"]
    assert "Looks like fraud." in second["prompt"]  # the rejected answer is shown back
    assert "wording" in second["prompt"]  # with the reason it failed
    assert d["report"] is not None


def test_a_second_rejection_stops(client, model, clock):
    model.set("summary", BAD, BAD, GOOD_SUMMARY)
    d = detail(client, ready(client, clock))
    assert d["ai_summary"]["status"] == "rejected" and d["ai_summary"]["attempts"] == 2
    assert len(summary_calls(model)) == 2
    assert d["report"] is not None


def test_a_verified_first_answer_is_one_call(client, model, clock):
    d = detail(client, ready(client, clock))
    assert d["ai_summary"]["status"] == "verified" and d["ai_summary"]["attempts"] == 1
    assert len(summary_calls(model)) == 1


def test_a_failed_model_call_is_not_retried(client, model, clock):
    model.set("summary", RuntimeError("Meta is down"), GOOD_SUMMARY)
    d = detail(client, ready(client, clock))
    assert d["ai_summary"]["status"] == "failed"
    assert len(summary_calls(model)) == 1
