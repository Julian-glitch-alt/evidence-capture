"""The only door to Meta (T34, tasks/t34-design.md).

Same model + prompt + image bytes -> one real call (cache); every real call is
logged with tokens and cost. `read_json` never raises.

Call hygiene (track 1): transient errors (connection, rate limit, server error)
are retried a bounded number of times with exponential backoff; timeouts are not
retried by default (one already used `timeout_s`, and the photo check counts as
stale after 45 s). Output is capped with `max_output_tokens` in the transport.
Before a real call, the dollars already logged for the claim and for the UTC day
are compared with the caps; over a cap the call is refused with a "budget: ..."
error and a `budget` row (nothing is sent, nothing is spent).
"""

import base64
import hashlib
import json
import logging
import os
import random
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from .db import Database
from .policies import canonical

logger = logging.getLogger(__name__)

META_BASE_URL = "https://api.meta.ai/v1"
FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
FIREWORKS_MODELS = {"muse-spark-1.3": "accounts/fireworks/models/muse-spark-open"}
PRICES_PER_M = {
    "muse-spark-1.3": (1.25, 4.25),  # USD per million input / output tokens (S0 figure, unverified)
    "accounts/fireworks/models/muse-spark-open": (1.25, 4.25),
}
PROVIDERS = {
    "meta": {
        "provider": "meta",
        "model": "Muse Spark 1.3",
        "host": "Meta Model API",
        "retention": "Free credits: Meta may use what it receives",
    },
    "fireworks": {
        "provider": "fireworks",
        "model": "Muse Spark Open",
        "host": "Fireworks",
        "retention": "Zero data retention",
    },
}
SEGMENT_MODEL = "sam-3.1"  # reports 0 tokens and has no published price: its cost is logged as unknown

DEFAULT_MAX_OUTPUT_TOKENS = 4096  # reasoning + answer; the largest answer (12 damages) is well under this
DEFAULT_MAX_ATTEMPTS = 3          # 1 call + 2 retries, transient errors only
DEFAULT_BACKOFF_S = 0.5           # 0.5 s, then 1 s, plus up to 25% jitter
DEFAULT_CLAIM_BUDGET_USD = 1.00   # live probe: about $0.06 per claim; retakes and retries stay far below $1
DEFAULT_DAY_BUDGET_USD = 15.00    # the event credit is $150 for 10 days
DEFAULT_SAM_MAX_CALLS = 0         # T42: off by default (S0: about 2.6 s each, run in parallel)
UNKNOWN_CALL_COST_USD = 0.01      # counted against the caps when a call that may have spent has no cost


class TransientError(Exception):
    """A connection error, rate limit or server error: worth another try."""

Transport = Callable[[str, str, bytes | None, str | None, float], dict]
# transport(model, prompt, image, mime, timeout_s) -> {"text", "input_tokens" | None, "output_tokens" | None}
# image and mime are None for text-only calls; raises TimeoutError on timeout, any other exception on error


def openai_transport(
    api_key: str,
    base_url: str = META_BASE_URL,
    reasoning_effort: str = "low",
    max_output_tokens: int | None = DEFAULT_MAX_OUTPUT_TOKENS,
    store: bool | None = None,
) -> Transport:
    """Responses API through the `openai` SDK (as in server/scripts/s0_probe.py): one input_text + one
    input_image (base64 data URL), text={"format": {"type": "json_object"}}, max_retries=0 (MetaClient
    retries), the given timeout. openai.APITimeoutError -> TimeoutError; connection errors, 429 and
    5xx -> TransientError; an answer cut off by the output cap -> RuntimeError("incomplete: ...").
    SAM models ("sam-*", track 6) get the concept as input_text and the image, nothing else.

    Reasoning effort "low": the T34 probe (30 Oct) gave the same answers as the default with
    about 5x fewer output tokens; the default ran 38-56 s on some photos, past the 30 s timeout.

    max_output_tokens: not yet confirmed on Meta's endpoint. If the endpoint answers 400 naming
    the parameter, the call is repeated once without it and the cap stays off for this process
    (logged), so an unsupported cap never takes the product down.

    store: when not None, sent as the Responses API store flag on every create call."""
    from openai import APIConnectionError, APITimeoutError, BadRequestError, InternalServerError, OpenAI, RateLimitError

    client = OpenAI(api_key=api_key, base_url=base_url, max_retries=0)
    cap = {"tokens": max_output_tokens}

    def transport(model: str, prompt: str, image: bytes | None, mime: str | None, timeout_s: float) -> dict:
        content: list[dict] = [{"type": "input_text", "text": prompt}]
        if image is not None:
            data_url = f"data:{mime};base64," + base64.b64encode(image).decode()
            content.append({"type": "input_image", "image_url": data_url})
        args = {"model": model, "input": [{"role": "user", "content": content}], "timeout": timeout_s}
        if store is not None:
            args["store"] = store
        segmenting = model.startswith("sam-")
        if not segmenting:  # SAM takes the concept and the image only (as in s0_probe.py) and answers plain text
            args |= {"text": {"format": {"type": "json_object"}}, "reasoning": {"effort": reasoning_effort}}
        try:
            try:
                response = client.responses.create(
                    **args, **({"max_output_tokens": cap["tokens"]} if cap["tokens"] and not segmenting else {}))
            except BadRequestError as e:
                if not cap["tokens"] or "max_output_tokens" not in str(e):
                    raise
                logger.warning("max_output_tokens refused by the endpoint; cap off for this process: %s", e)
                cap["tokens"] = None
                response = client.responses.create(**args)
        except APITimeoutError as e:  # a subclass of APIConnectionError: keep it first
            raise TimeoutError(str(e)) from e
        except (APIConnectionError, RateLimitError, InternalServerError) as e:
            raise TransientError(f"{type(e).__name__}: {e}") from e
        if getattr(response, "status", None) == "incomplete":
            details = getattr(response, "incomplete_details", None)
            raise RuntimeError(f"incomplete: {getattr(details, 'reason', None) or 'unknown'}")
        usage = response.usage
        return {
            "text": response.output_text,
            "input_tokens": usage.input_tokens if usage is not None else None,
            "output_tokens": usage.output_tokens if usage is not None else None,
        }

    return transport


def settings_from_env(environ: Mapping[str, str] | None = None) -> dict:
    """MetaClient and transport settings from the environment; a bad value fails at startup.

    META_MAX_OUTPUT_TOKENS (int, or "off"), META_MAX_ATTEMPTS (int >= 1),
    META_BUDGET_CLAIM_USD and META_BUDGET_DAY_USD (dollars, or "off"), META_SAM_MAX_CALLS
    (SAM outline calls per claim, int >= 0; 0 or "off" skips the outline step),
    MODEL_PROVIDER ("meta" or "fireworks", empty is meta), MODEL_TIMEOUT_S (seconds >= 5;
    30 for Meta unless set, 60 for Fireworks unless set)."""
    env = os.environ if environ is None else environ

    def number(name: str, default, cast, minimum):
        raw = (env.get(name) or "").strip()
        if not raw:
            return default
        if raw.lower() == "off":
            if name == "META_MAX_ATTEMPTS":
                raise ValueError(f"{name} cannot be off")
            return None
        value = cast(raw)
        if value < minimum:
            raise ValueError(f"{name} must be at least {minimum}")
        return value

    provider_raw = (env.get("MODEL_PROVIDER") or "").strip()
    if not provider_raw:
        provider = "meta"
    elif provider_raw.lower() in ("meta", "fireworks"):
        provider = provider_raw.lower()
    else:
        raise ValueError("MODEL_PROVIDER must be meta or fireworks")
    timeout_raw = (env.get("MODEL_TIMEOUT_S") or "").strip()
    if not timeout_raw:
        timeout_s: int | float = 30 if provider == "meta" else 60
    else:
        try:
            timeout_value = float(timeout_raw)
        except ValueError:
            raise ValueError("MODEL_TIMEOUT_S must be a number of 5 or more") from None
        if not timeout_value >= 5:
            raise ValueError("MODEL_TIMEOUT_S must be a number of 5 or more")
        timeout_s = int(timeout_value) if timeout_value.is_integer() else timeout_value

    return {
        "max_output_tokens": number("META_MAX_OUTPUT_TOKENS", DEFAULT_MAX_OUTPUT_TOKENS, int, 1),
        "max_attempts": number("META_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS, int, 1),
        "claim_budget_usd": number("META_BUDGET_CLAIM_USD", DEFAULT_CLAIM_BUDGET_USD, float, 0),
        "day_budget_usd": number("META_BUDGET_DAY_USD", DEFAULT_DAY_BUDGET_USD, float, 0),
        "sam_max_calls": number("META_SAM_MAX_CALLS", DEFAULT_SAM_MAX_CALLS, int, 0),
        "provider": provider,
        "timeout_s": timeout_s,
    }


def usage(conn, claim_id: str) -> dict:
    """The claim's AI usage from the cost log: real calls (cache hits cost nothing and add no row),
    failures, calls refused by the budget cap, dollars and model seconds."""
    rows = conn.execute(
        "SELECT status, cost_usd, latency_ms FROM model_calls WHERE claim_id=%s", (claim_id,)
    ).fetchall()
    sent = [r for r in rows if r["status"] != "budget"]
    return {
        "calls": len(sent),
        "failed": sum(1 for r in sent if r["status"] != "ok"),
        "budget_blocked": len(rows) - len(sent),
        "cost_usd": round(sum(r["cost_usd"] or 0 for r in sent), 4),
        "cost_unknown_calls": sum(1 for r in sent if r["cost_usd"] is None and r["status"] in _MAY_SPEND),
        "seconds": round(sum(r["latency_ms"] or 0 for r in sent) / 1000, 1),
    }


_MAY_SPEND = ("ok", "bad_json", "timeout")


class _Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class MetaClient:
    def __init__(
        self,
        db: Database,
        transport: Transport | None,
        *,
        max_concurrent: int = 8,
        timeout_s: float = 30.0,
        clock=None,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_s: float = DEFAULT_BACKOFF_S,
        retry_timeouts: bool = False,
        claim_budget_usd: float | None = DEFAULT_CLAIM_BUDGET_USD,
        day_budget_usd: float | None = DEFAULT_DAY_BUDGET_USD,
        sleep: Callable[[float], None] = time.sleep,
        models: Mapping[str, str] | None = None,
        provider: str = "meta",
    ):
        self.db = db
        self.transport = transport
        self.timeout_s = timeout_s
        self.models = dict(models) if models else None
        self.provider = provider
        self.clock = clock or _Clock()
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self.max_attempts = max(1, int(max_attempts))
        self.backoff_s = backoff_s
        self.retry_timeouts = retry_timeouts
        self.claim_budget_usd = claim_budget_usd
        self.day_budget_usd = day_budget_usd
        self._sleep = sleep

    def read_json(
        self,
        *,
        purpose: str,
        model: str,
        prompt: str,
        image: bytes | None = None,
        mime: str | None = None,
        claim_id: str | None = None,
        item_id: str | None = None,
        timeout_s: float | None = None,
        max_attempts: int | None = None,
    ) -> dict:
        """-> {"ok": bool, "json": dict | None, "error": str | None, "cached": bool, "call_id": str | None}"""
        return self._call(purpose, model, prompt, image, mime, claim_id, item_id, _parse_object,
                          timeout_s=timeout_s, max_attempts=max_attempts)

    def segment(
        self,
        image: bytes,
        concept: str,
        *,
        purpose: str,
        claim_id: str | None = None,
        item_id: str | None = None,
        mime: str = "image/jpeg",
        model: str = SEGMENT_MODEL,
    ) -> dict:
        """SAM 3.1 on one image with a text concept (track 6), with the same cache, cost log, retries and
        budget caps as read_json. -> {"ok", "text": str | None, "error", "cached", "call_id"}
        `text` is SAM's raw output_text ("" when it found nothing), kept in model_calls as
        {"output_text": ...}. SAM reports no tokens: the cost is logged as unknown and counted
        against the caps like any unknown-cost call."""
        result = self._call(purpose, model, concept, image, mime, claim_id, item_id, _wrap_text)
        obj = result.pop("json")
        return {**result, "text": obj.get("output_text") if isinstance(obj, dict) else None}

    def _call(self, purpose, model, prompt, image, mime, claim_id, item_id, parse,
              timeout_s: float | None = None, max_attempts: int | None = None) -> dict:
        """The cached, logged, retried, budget-capped call behind read_json and segment; never raises.
        parse(text) -> the dict stored and returned as "json", or None for an unusable answer.
        The model is mapped to the host's name first; the cache key, the model_calls
        row, the price and the transport all use the mapped name."""
        failed = {"ok": False, "json": None, "error": None, "cached": False, "call_id": None}
        if self.models:
            model = self.models.get(model, model)
        if self.transport is None:
            return {**failed, "error": "model_not_configured"}
        try:
            key = hashlib.sha256(canonical({
                "model": model,
                "prompt": prompt,
                "mime": mime if mime is not None else "",
                "image_sha256": hashlib.sha256(image if image is not None else b"").hexdigest(),
            }).encode()).hexdigest()
            with self.db.tx() as conn:
                hit = conn.execute(
                    "SELECT id, response_json FROM model_calls WHERE cache_key=%s AND status='ok' LIMIT 1",
                    (key,),
                ).fetchone()
            if hit is not None:
                return {"ok": True, "json": json.loads(hit["response_json"]), "error": None, "cached": True, "call_id": hit["id"]}
            blocked = self._over_budget(claim_id)
            if blocked is not None:
                self._log(key, purpose, model, claim_id, item_id, "budget", None, blocked, None, None, None, 0, 0)
                return {**failed, "error": blocked}
            timeout = self.timeout_s if timeout_s is None else timeout_s
            limit = self.max_attempts if max_attempts is None else max(1, max_attempts)
            start = time.monotonic()
            attempts = 0
            while True:
                attempts += 1
                if not self._sem.acquire(timeout=timeout):
                    if attempts == 1:
                        return {**failed, "error": "busy"}
                    status, error, obj, in_t, out_t = "error", "busy", None, None, None
                    break
                try:
                    status, error, obj, in_t, out_t, transient = self._attempt(model, prompt, image, mime, parse, timeout)
                finally:
                    self._sem.release()
                if not transient or attempts >= limit:
                    break
                delay = self.backoff_s * (2 ** (attempts - 1))
                self._sleep(delay + random.uniform(0, delay / 4))
            latency_ms = max(0, round((time.monotonic() - start) * 1000))
            cost = None
            if in_t is not None and out_t is not None and model in PRICES_PER_M:
                buy, sell = PRICES_PER_M[model]
                cost = in_t * buy / 1e6 + out_t * sell / 1e6
            call_id = self._log(key, purpose, model, claim_id, item_id, status, obj, error,
                                in_t, out_t, cost, latency_ms, attempts)
            if status == "ok":
                return {"ok": True, "json": obj, "error": None, "cached": False, "call_id": call_id}
            return {**failed, "error": error}
        except Exception as e:
            return {**failed, "error": f"{type(e).__name__}: {e}"[:300]}

    def _attempt(self, model: str, prompt: str, image: bytes | None, mime: str | None, parse,
                 timeout: float) -> tuple:
        """One transport call -> (status, error, obj, input_tokens, output_tokens, worth_retrying)."""
        try:
            res = self.transport(model, prompt, image, mime, timeout)
        except TimeoutError:
            return "timeout", "timeout", None, None, None, self.retry_timeouts
        except TransientError as e:
            return "error", f"{e}"[:300], None, None, None, True
        except Exception as e:
            return "error", f"{type(e).__name__}: {e}"[:300], None, None, None, False
        in_t = res.get("input_tokens") if isinstance(res, dict) else None
        out_t = res.get("output_tokens") if isinstance(res, dict) else None
        obj = parse(res.get("text") if isinstance(res, dict) else None)
        if obj is None:
            return "bad_json", "bad_json", None, in_t, out_t, False
        return "ok", None, obj, in_t, out_t, False

    def _over_budget(self, claim_id: str | None) -> str | None:
        """"budget: ..." when the claim's or the day's logged dollars reached a cap, else None."""
        if self.claim_budget_usd is None and self.day_budget_usd is None:
            return None
        spend = ("COALESCE(SUM(COALESCE(cost_usd, CASE WHEN status IN ('ok', 'bad_json', 'timeout')"
                 " THEN %s ELSE 0 END)), 0) AS spent")
        with self.db.tx() as conn:
            if self.claim_budget_usd is not None and claim_id is not None:
                spent = conn.execute(f"SELECT {spend} FROM model_calls WHERE claim_id=%s",
                                     (UNKNOWN_CALL_COST_USD, claim_id)).fetchone()["spent"]
                if spent >= self.claim_budget_usd:
                    return f"budget: claim cap ${self.claim_budget_usd:.2f} reached (${spent:.4f} spent)"
            if self.day_budget_usd is not None:
                day = self.clock.now().astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
                spent = conn.execute(
                    f"SELECT {spend} FROM model_calls WHERE created_at >= %s AND created_at < %s",
                    (UNKNOWN_CALL_COST_USD, day.isoformat(), (day + timedelta(days=1)).isoformat()),
                ).fetchone()["spent"]
                if spent >= self.day_budget_usd:
                    return f"budget: daily cap ${self.day_budget_usd:.2f} reached (${spent:.4f} spent today, UTC)"
        return None

    def _log(self, key, purpose, model, claim_id, item_id, status, obj, error,
             in_t, out_t, cost, latency_ms, attempts) -> str:
        call_id = uuid.uuid4().hex
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO model_calls (id, cache_key, purpose, model, claim_id, item_id, status,"
                " response_json, error, input_tokens, output_tokens, cost_usd, latency_ms, created_at, attempts)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    call_id, key, purpose, model, claim_id, item_id, status,
                    canonical(obj) if status == "ok" else None, error,
                    in_t, out_t, cost, latency_ms, self.clock.now().astimezone(UTC).isoformat(), attempts,
                ),
            )
        return call_id


def _strip_fence(text) -> str:
    """A model may wrap the JSON in ```json ... ```; anything else passes through stripped."""
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    if not stripped.startswith("```"):
        return stripped
    lines = stripped.splitlines()[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _wrap_text(text) -> dict | None:
    """A SAM answer is plain text, not JSON: any string (even "") is usable."""
    return {"output_text": text} if isinstance(text, str) else None


def _parse_object(text) -> dict | None:
    """The JSON object in the text, or None for anything else."""
    try:
        obj = json.loads(_strip_fence(text))
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None
