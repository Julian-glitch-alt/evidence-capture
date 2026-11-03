"""E1 / E2 evaluation run: labelled photos and claims through the production code paths.

    # CI / offline: recorded answers, no API, no cost
    cd server && uv run python scripts/e1_eval.py eval/fixtures/labels.json --replay eval/fixtures/replay.json

    # Real model (LLAMA_API_KEY in the shell): photos are SENT TO META
    cd server && uv run python scripts/e1_eval.py ../data/e1/labels.json --live

Every model answer is cached in the database's `model_calls` table (key = model + prompt +
image), so a re-run with unchanged prompts costs nothing. Results: a markdown table on
stdout, plus report.md and result.json in --out (default server/eval/out/<run id>/).
Format of the labels: server/eval/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, date, datetime
from pathlib import Path

SERVER = Path(__file__).resolve().parents[1]
DEFAULT_DB = "postgresql://postgres:test@127.0.0.1:55432/claims_test"


def parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("labels", type=Path, help="labels.json")
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--replay", type=Path, help="recorded answers (replay.json); nothing is sent")
    mode.add_argument("--live", action="store_true",
                      help="call the real model (see --provider); the photos are sent to that host")
    p.add_argument("--provider", choices=("meta", "fireworks"), default=os.environ.get("MODEL_PROVIDER") or "meta",
                   help="with --live: Meta Model API (LLAMA_API_KEY) or Muse Spark Open on Fireworks "
                        "(FIREWORKS_API_KEY, zero data retention); default MODEL_PROVIDER, else meta")
    p.add_argument("--database-url", default=os.environ.get("EVAL_DATABASE_URL") or os.environ.get("DATABASE_URL")
                   or DEFAULT_DB, help="Postgres for the call cache and cost log (default: the local test database)")
    p.add_argument("--out", type=Path, help="output folder (default: server/eval/out/<run id>)")
    p.add_argument("--run-id", help="default: UTC timestamp")
    p.add_argument("--today", help="YYYY-MM-DD for the mileage check (default: labels 'today', else today)")
    p.add_argument("--day-budget", default="5", help="dollars for this run's UTC day, or 'off' (default 5)")
    p.add_argument("--claim-budget", default="0.25", help="dollars per photo / claim, or 'off' (default 0.25)")
    p.add_argument("--workers", type=int, default=4, help="photos read in parallel (default 4)")
    p.add_argument("--sam", action="store_true",
                   help="also outline every boxed damage with SAM 3.1 (agreement rate and SAM latency)")
    p.add_argument("--lane-rule", choices=("cost", "count"), default=os.environ.get("LANE_RULE") or "cost",
                   help="lane rule that drives each claim's lane; the other is reported beside it (default cost)")
    return p.parse_args(argv)


def _dollars(value: str) -> float | None:
    return None if value.strip().lower() == "off" else float(value)


def main(argv: list[str] | None = None) -> int:
    args = parse(argv)
    # Importing the package builds the web app, which needs a database URL (known issue).
    os.environ.setdefault("DATABASE_URL", args.database_url)
    from evidence_server import evaluation
    from evidence_server.db import Database
    from evidence_server.meta_client import (FIREWORKS_BASE_URL, FIREWORKS_MODELS, MetaClient, openai_transport,
                                             settings_from_env)

    try:
        labels = evaluation.load_labels(args.labels)
    except evaluation.LabelError as e:
        print(f"labels: {e}", file=sys.stderr)
        return 2
    models, provider = None, "meta"
    if args.replay is not None:
        transport = evaluation.ReplayTransport(labels, json.loads(args.replay.read_text()))
    else:
        provider = args.provider
        key_name = "FIREWORKS_API_KEY" if provider == "fireworks" else "LLAMA_API_KEY"
        key = os.environ.get(key_name)
        if not key:
            print(f"--live --provider {provider} needs {key_name} in the environment", file=sys.stderr)
            return 2
        cap = settings_from_env()["max_output_tokens"]
        if provider == "fireworks":  # the same host and settings as production with MODEL_PROVIDER=fireworks
            transport = openai_transport(key, base_url=FIREWORKS_BASE_URL, store=False, max_output_tokens=cap)
            models = FIREWORKS_MODELS
            print("Sending the labelled photos to Muse Spark Open on Fireworks (zero data retention).", file=sys.stderr)
        else:
            transport = openai_transport(key, max_output_tokens=cap)
            print("Sending the labelled photos to the Meta Model API (free credits: Meta may keep what is sent).",
                  file=sys.stderr)
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    db = Database(args.database_url)
    try:
        db.migrate()
        meta = MetaClient(db, transport, models=models, provider=provider, timeout_s=60 if provider == "fireworks" else 30,
                          claim_budget_usd=_dollars(args.claim_budget), day_budget_usd=_dollars(args.day_budget))
        today = date.fromisoformat(args.today) if args.today else None
        result = evaluation.run(labels, meta, run_id=run_id, now=datetime.now(UTC), today=today,
                                workers=args.workers, sam=args.sam, lane_rule=args.lane_rule)
    finally:
        db.close()
    result["transport"] = "replay" if args.replay is not None else f"live ({provider})"
    if models:
        result["model"] = models.get(result["model"], result["model"])
    report = evaluation.markdown(result)
    out = args.out or SERVER / "eval" / "out" / run_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.md").write_text(report)
    (out / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
    print(report)
    print(f"Wrote {out / 'report.md'} and {out / 'result.json'}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
