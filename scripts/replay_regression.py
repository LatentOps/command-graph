from __future__ import annotations

import argparse
import json
from pathlib import Path

from ordin.regression_promotion import load_regression_replays, run_regression_replays


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "benchmarks" / "regressions.jsonl"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay promoted Ordin integration and safety regressions."
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--id", dest="replay_id", help="Replay one promoted regression by id")
    parser.add_argument("--json-out", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    replays = load_regression_replays(args.corpus)
    report = run_regression_replays(replays, replay_id=args.replay_id)
    payload = report.as_dict()

    print(f"Regressions               {payload['passed']} / {payload['cases']}")
    print(f"Critical false allows     {payload['critical_false_allows']}")
    print("Failure classes           " + ", ".join(sorted(payload["failure_class_coverage"])))
    for result in payload["results"]:
        print(
            f"- {result['id']}: {'PASS' if result['passed'] else 'FAIL'} "
            f"({result['failure_class']}, {result['latency_ms']:.2f}ms)"
        )

    if args.json_out is not None:
        args.json_out.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    errors = report.regression_errors()
    if errors:
        print("\nRegression errors:")
        for error in errors:
            print(f"- {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
