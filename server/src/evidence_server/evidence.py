"""Weight of evidence per root cause and the expected-cost test for SIU screening (track 5,
docs/explorations/evidence-math.md).

Pure: no database, no clock, no network, no model. Every weight comes from
reference/evidence_weights.json and is an assumption until labelled outcomes of SIU
referrals replace it. Nothing here is a probability of fraud and nothing is shown as
one: people see a band ("Evidence strength: moderate") and the causes behind it.

Score, in log10 odds: prior log-odds + the sum of log10 likelihood ratios over DISTINCT
root causes, one per cause however many rows report it. Adding them assumes the causes
are independent of each other once we know whether the claim needs investigation. That
is only defensible because proposal.ROOT_CAUSES already folds rows that report one fact
from several places (form VIN, photo VIN and plate against the same policy) into one cause.
A cause whose check could not run adds nothing and is listed as missing; an absent check
never counts as clean evidence either (no negative weights).
"""

import json
import logging
import math
from pathlib import Path

VERSION = "evidence/v1"
LEVELS = ("conflict", "review")
NOTE = "Weights are assumptions, set in configuration."

log = logging.getLogger(__name__)
_warned: set[str] = set()


def load_weights(path: Path | None = None) -> dict:
    """The weights file next to this package (or `path`)."""
    path = path or Path(__file__).parent / "reference" / "evidence_weights.json"
    return json.loads(path.read_text())


WEIGHTS = load_weights()


def score(causes: list[dict], note_causes: list[dict] = (), weights: dict = WEIGHTS) -> dict:
    """causes / note_causes = proposal.causes(...) of the flags / notes -> the evidence block.

    {score_log10, weight_log10, prior_log10, band, contributions: [{cause, label, level, log10_lr,
    source, model_only}], neutral: [labels], missing: [{cause, label, codes}], prior_note, note}"""
    table = weights["causes"]
    cap = weights["model_only_cap"]["log10_lr"]
    model_left = cap
    contributions, neutral, missing = [], [], []
    for c in causes:
        cfg = table.get(c["cause"])
        if cfg is None:
            cfg = weights["default"]
            if c["cause"] not in _warned:
                _warned.add(c["cause"])
                log.warning("evidence: no weight for cause %r (codes %s); using the default", c["cause"],
                            ", ".join(c.get("codes") or []) or "-")
        if cfg.get("missing"):
            missing.append({"cause": c["cause"], "label": c["label"], "codes": list(c.get("codes") or [])})
            continue
        value = float(cfg[c["level"]])
        if c["model_only"]:
            value = min(value, model_left)
            model_left -= value
        if value <= 0:
            neutral.append(c["label"])
            continue
        contributions.append({"cause": c["cause"], "label": c["label"], "level": c["level"],
                              "log10_lr": round(value, 3), "source": cfg.get("source", "assumed"),
                              "model_only": c["model_only"]})
    for c in note_causes:  # a note (the licence not sent to the model) is a check that did not run
        if (table.get(c["cause"]) or {}).get("missing") and all(m["cause"] != c["cause"] for m in missing):
            missing.append({"cause": c["cause"], "label": c["label"], "codes": list(c.get("codes") or [])})
    weight = round(sum(x["log10_lr"] for x in contributions), 3)
    prior = weights["prior"]
    prior_log10 = math.log10(prior["p"] / (1 - prior["p"]))
    return {
        "version": VERSION,
        "score_log10": round(prior_log10 + weight, 3),
        "weight_log10": weight,
        "prior_log10": round(prior_log10, 3),
        "band": band(weight, weights),
        "contributions": contributions,
        "neutral": neutral,
        "missing": missing,
        "prior_note": f"Prior: about 1 in {round(1 / prior['p'])} claims needs investigation ({prior['source']}).",
        "note": NOTE,
    }


def band(weight_log10: float, weights: dict = WEIGHTS) -> str:
    """Evidence alone (no prior) -> "low" | "moderate" | "strong"."""
    cuts = weights["bands"]
    if weight_log10 >= cuts["strong"]:
        return "strong"
    if weight_log10 >= cuts["moderate"]:
        return "moderate"
    return "low"


def siu_test(evidence: dict, claim_value: int | None, weights: dict = WEIGHTS) -> dict:
    """Is SIU screening worth its cost on this claim?

    Suggest it when  P * value * recovery_rate  >  siu_review_cost + (1 - P) * honest_friction_cost,
    i.e. when the claim value is above the break-even value
    (siu_review_cost + (1 - P) * honest_friction_cost) / (P * recovery_rate),
    where P = odds / (1 + odds) and odds = 10 ** score_log10. P stays inside this function: only the
    break-even value (dollars) leaves it. Eligible only with at least one code-verified conflict
    (never model-only evidence alone, never a claim with no conflict at all)."""
    params = {k: v["value"] for k, v in weights["cost"].items()}
    eligible = any(c["level"] == "conflict" and not c["model_only"] for c in evidence["contributions"])
    odds = 10 ** evidence["score_log10"]
    p = odds / (1 + odds)
    break_even = (params["siu_review_cost"] + (1 - p) * params["honest_friction_cost"]) / (p * params["recovery_rate"])
    return {
        "eligible": eligible,
        "claim_value": claim_value,
        "break_even_value": _round50(break_even),
        "suggest": eligible and claim_value is not None and claim_value > break_even,
        "parameters": params,
    }


def _round50(value: float) -> int:
    return int(math.floor(value / 50 + 0.5) * 50)
