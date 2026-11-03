"""Paired comparison of two E1 runs on the same labelled photos (two prompt versions).

    cd server && uv run python scripts/e1_compare.py eval/out/<run A>/result.json eval/out/<run B>/result.json

Per field (plate, VIN, odometer): photos right in both, only in A, only in B, in neither,
and the exact McNemar p-value on the discordant pairs. "B is better" is a claim only when
only_b is well above only_a AND p is small; a higher rate alone is not evidence.
Pure: reads the two result.json files, sends nothing, needs no database.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("a", type=Path, help="result.json of run A (the baseline)")
    p.add_argument("b", type=Path, help="result.json of run B (the candidate)")
    args = p.parse_args(argv)
    from evidence_server.evaluation import _ratio, compare_reads

    a, b = json.loads(args.a.read_text()), json.loads(args.b.read_text())
    print(f"A = {a['run_id']} ({a['prompts']['photo']}), B = {b['run_id']} ({b['prompts']['photo']})\n")
    print("| Field | Pairs | Both right | Only A | Only B | Neither | A exact | B exact | McNemar p |")
    print("|---|---|---|---|---|---|---|---|---|")
    for field, m in compare_reads(a, b).items():
        print(f"| {field} | {m['pairs']} | {m['both_right']} | {m['only_a']} | {m['only_b']} | {m['both_wrong']} | "
              f"{_ratio(m['a']['hits'], m['a']['of'])} | {_ratio(m['b']['hits'], m['b']['of'])} | {m['p_value']:.3f} |")
    return 0


if __name__ == "__main__":
    sys.exit(main())
