"""The AI summary of the report (T40, tasks/t40-design.md).

The model only summarises the report facts for the adjuster; code checks
every sentence before it reaches the report. A sentence reaches the report
only if it passes every check; otherwise the AI summary is dropped (status
`rejected` with reasons, or `failed`), and the report is still made.

What the checks prove, and what they do not: every sentence cites facts that
exist, every number in it is in the facts, it does not contradict the result
of a check it cites (polarity), any severity word matches a cited damage, and
it uses no accusation, decision or money wording. They do not prove the
sentence is a faithful summary; the status name "verified" is kept for the
stored data, and the caption shown to people is CAPTION.
"""

import json
import re

from .photo_prompts import MODEL

VERSION = "ai-summary/v3"
MODEL = MODEL
SUMMARY = "You summarise a US motor insurance claim for the adjuster"
# What the UI and the PDF print under the AI summary: what code checked, no more.
CAPTION = "Muse Spark · sentences checked for sources, numbers and wording"

MAX_WORDS = 90

# Accusation, decision and money words, matched as whole words (a trailing * allows any ending).
FORBIDDEN = (
    "fraud*", "deny", "denied", "denial", "reject*", "approve*", "approval",
    "staged", "staging", "not credible", "lying", "lied", "liar", "fake*", "fabricat*", "dishonest*",
    "pay", "paid", "pays", "paying", "payment*", "payout*", "settle*", "reimburs*", "compensat*", "reserve*",
)
# "Total loss" is allowed only in a sentence that cites the POSSIBLE_TOTAL_LOSS flag.
TOTAL_LOSS = re.compile(r"\btotal(?:led|ed)?[\s-]+loss\b|\btotall?ed\b")
TOTAL_LOSS_CODE = "POSSIBLE_TOTAL_LOSS"

# Polarity: a sentence that cites a check or flag that did not pass must not sound like a pass,
# and one that cites only passed checks must not sound like a failure. Negative phrases are
# removed before looking for positive words, so "does not match" never counts as "match".
# T42: a negation (not, never, cannot, can't, couldn't, unable to, no) with up to three
# words before a positive stem counts as negative ("could not be matched", "no match").
NEGATIVE = re.compile(
    r"\b(?:does|do|did|could|can|would)\s+not\s+(?:match|fit|agree|line up|correspond)\w*"
    r"|\b(?:doesn't|don't|didn't|couldn't|can't|won't)\s+(?:match|fit|agree|line up|correspond)\w*"
    r"|\bnot\s+(?:match\w*|consistent|confirmed|verified|passed|fit\w*)"
    r"|\bmismatch\w*|\bfail\w*|\binconsistent\b|\bcontradict\w*|\bdisagree\w*|\bconflict\w*"
    r"|\b(?:not|never|cannot|can['’]t|couldn['’]t|unable to|no)\b"
    r"(?:\s+\w+){0,3}?\s+(?:match|confirm|verif|consistent|fit|agree|correspond|pass)\w*"
)
POSITIVE = re.compile(
    r"\bmatch(?:es|ed|ing)?\b|\bpass(?:es|ed)?\b|\bconfirm(?:s|ed)?\b|\bverified\b"
    r"|\bconsistent\b|\bfits?\b|\bagrees?\b|\bcorrespond(?:s)?\b"
)
SEVERITY = re.compile(r"\b(minor|moderate|severe)\b")

_NUMBER = re.compile(r"\d[\d,]*")
_REF_TOKEN = re.compile(r"\[?\b(?:[FD]\d+|C:[\w.]+)\b\]?")
_HEX_ID = re.compile(r"\b[0-9a-f]{16,}\b")
_ID_KEYS = {"item_id", "key", "id", "sha256", "call_id", "outbox_id", "evidence_id"}


def facts(report: dict) -> dict:
    """The report facts the model may cite, keyed for references."""
    out: dict = {}
    for i, flag in enumerate(report.get("flags") or [], 1):
        out[f"F{i}"] = {
            "level": flag.get("level"),
            "code": flag.get("reason_code"),
            "reason": flag.get("reason"),
            "evidence": list(flag.get("evidence") or []),
        }
    damages = report["damage_list"] if "damage_list" in report else report.get("damages")
    for i, damage in enumerate(damages or [], 1):  # each physical damage once
        out[f"D{i}"] = {
            "area": damage.get("area"),
            "type": damage.get("damage_type"),
            "severity": damage.get("severity"),
            "age": damage.get("age"),
            "label": ", ".join(damage.get("labels") or []) or damage.get("label"),
        }
    for check in report.get("checks") or []:
        check_id = check.get("check_id")
        if check_id:
            out[f"C:{check_id}"] = {"result": check.get("result"), "reason": check.get("reason")}
    loss = report.get("loss") or {}
    out["S"] = loss.get("description")
    return out


def prompt(facts: dict) -> str:
    """The text-only model call: instructions plus the facts as JSON."""
    return (
        f"{SUMMARY} in at most 4 plain sentences and at most {MAX_WORDS} words in total for the claim file. "
        "Retell the claim in this order: first what the claimant says happened (cite the S fact); "
        "then what the photos show that fits (cite D or C: keys); "
        "then each flag in plain words, one sentence per flag (cite its F key); "
        "when there are no flags, one sentence saying every check passed (cite C: keys). "
        "Use only the facts below; plain words, no opinion on fraud, no money decisions. "
        "Every sentence must cite at least one fact key in refs. "
        'Return only one JSON object with exactly this key: "sentences" (array, 1-4 items), '
        'each {"text": the sentence, "refs": [fact keys it uses, e.g. "F1", "D1"]}.\n'
        f"<facts>\n{json.dumps(facts, sort_keys=True)}\n</facts>"
    )


def retry_prompt(facts: dict, previous_answer, reasons: list[str]) -> str:
    """A second try after a rejection: the same instructions and facts, then the
    rejected answer as JSON with the reasons, asking for a corrected answer."""
    return (
        prompt(facts)
        + "\nYour previous answer was rejected: check sources, numbers and wording.\n"
        f"<previous>\n{json.dumps(previous_answer, sort_keys=True)}\n</previous>\n"
        "Reasons:\n" + "".join(f"- {r}\n" for r in reasons)
        + 'Return a corrected answer in the same JSON shape ("sentences" only).'
    )


def verify(answer: dict | None, facts: dict) -> dict:
    """-> {"status": "verified"|"rejected", "sentences": [...], "reasons": [...]}.

    "verified" means every rule below passed, not that the summary is true (see CAPTION)."""
    facts = facts or {}
    reasons: list[str] = []
    sentences = answer.get("sentences") if isinstance(answer, dict) else None
    if not isinstance(sentences, list) or not 1 <= len(sentences) <= 4:
        return {"status": "rejected", "sentences": [], "reasons": ["shape: 1-4 sentences are required"]}
    cleaned: list = []
    for entry in sentences:
        if not isinstance(entry, dict):
            reasons.append("shape: every sentence needs text and refs")
            continue
        text = entry.get("text")
        refs = entry.get("refs")
        if not isinstance(text, str) or not text.strip():
            reasons.append("shape: every sentence needs text")
            continue
        if not isinstance(refs, list) or not refs or any(not isinstance(r, str) for r in refs):
            reasons.append("refs: every sentence must cite at least one fact key")
            continue
        unknown = [r for r in refs if r not in facts]
        if unknown:
            reasons.append(f"refs: unknown fact key {unknown[0]}")
            continue
        if len(text) > 300:
            reasons.append("long: a sentence is over 300 characters")
            continue
        problem = _wording(text, refs, facts) or _polarity(text, refs, facts) or _severity(text, refs, facts)
        if problem is not None:
            reasons.append(problem)
            continue
        cleaned.append({"text": text, "refs": list(refs)})
    if reasons:
        return {"status": "rejected", "sentences": [], "reasons": reasons}
    words = sum(len(s["text"].split()) for s in cleaned)
    if words > MAX_WORDS:
        return {"status": "rejected", "sentences": [],
                "reasons": [f"long: the summary is over {MAX_WORDS} words"]}
    allowed = fact_numbers(facts)
    for s in cleaned:
        for number in _numbers(_REF_TOKEN.sub(" ", s["text"])):
            if number not in allowed:
                return {"status": "rejected", "sentences": [],
                        "reasons": [f"number: {number} is not in the facts"]}
    return {"status": "verified", "sentences": cleaned, "reasons": []}


def fact_numbers(facts: dict) -> set[int]:
    """Every number written in the fact values, plus the counts of flags and damages.

    Keys, item ids, hashes and other long hex strings are left out, so their digits
    never let an invented number through."""
    found: set[int] = set()

    def walk(value, key=None) -> None:
        if key in _ID_KEYS:
            return
        if isinstance(value, dict):
            for k, v in value.items():
                walk(v, k)
        elif isinstance(value, list):
            for v in value:
                walk(v, key)
        elif isinstance(value, bool) or value is None:
            return
        elif isinstance(value, (int, float)):
            if float(value).is_integer():
                found.add(int(value))
        elif isinstance(value, str):
            found.update(_numbers(_HEX_ID.sub(" ", value)))

    walk(facts)
    found.add(sum(1 for k in facts if k.startswith("F")))
    found.add(sum(1 for k in facts if k.startswith("D")))
    return found


def _numbers(text: str) -> list[int]:
    out = []
    for match in _NUMBER.findall(text):
        digits = match.replace(",", "")
        if digits:
            out.append(int(digits))
    return out


def _wording(text: str, refs: list[str], facts: dict) -> str | None:
    """Accusation, decision and money words; "total loss" only when the total-loss flag is cited."""
    lowered = text.lower()
    for word in FORBIDDEN:
        stem = re.escape(word.rstrip("*")).replace("\\ ", r"\s+")
        pattern = rf"\b{stem}\w*" if word.endswith("*") else rf"\b{stem}\b"
        if re.search(pattern, lowered):
            return f"forbidden: {word.rstrip('*')}"
    if TOTAL_LOSS.search(lowered):
        cited = any((facts.get(r) or {}).get("code") == TOTAL_LOSS_CODE
                    for r in refs if r.startswith("F") and isinstance(facts.get(r), dict))
        if not cited:
            return "forbidden: total loss without citing the possible total loss flag"
    return None


def _cited_results(refs: list[str], facts: dict) -> list[str]:
    """"pass" or "not_pass" per cited check or flag; damages and the statement carry no result."""
    out = []
    for r in refs:
        fact = facts.get(r)
        if not isinstance(fact, dict):
            continue
        if r.startswith("C:") and fact.get("result") in ("pass", "fail", "unclear"):
            out.append("pass" if fact["result"] == "pass" else "not_pass")
        elif r.startswith("F"):
            out.append("not_pass")  # a flag exists only because something did not pass
    return out


def _polarity(text: str, refs: list[str], facts: dict) -> str | None:
    """The sentence must not flip the result of a check or flag it cites."""
    results = _cited_results(refs, facts)
    if not results:
        return None
    lowered = text.lower()
    negative = NEGATIVE.search(lowered)
    positive = POSITIVE.search(NEGATIVE.sub(" ", lowered))
    if "not_pass" in results and positive:
        return f"polarity: says '{positive.group(0)}' but cites a check or flag that did not pass"
    if "not_pass" not in results and negative:
        return f"polarity: says '{negative.group(0)}' but every cited check passed"
    return None


def _severity(text: str, refs: list[str], facts: dict) -> str | None:
    """A severity word must be the severity of a cited damage (no invented severity)."""
    said = set(SEVERITY.findall(text.lower()))
    if not said:
        return None
    known = {(facts.get(r) or {}).get("severity") for r in refs
             if r.startswith("D") and isinstance(facts.get(r), dict)}
    extra = sorted(said - known)
    if extra:
        return f"severity: '{extra[0]}' is not the severity of a cited damage"
    return None
