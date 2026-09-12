from __future__ import annotations

import argparse
import json
from pathlib import Path

from ordin.regression_replay import load_failure_regressions, run_failure_regressions


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "benchmarks" / "failure_regressions.jsonl"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay promoted Ordin integration and safety regressions."
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--case", dest="case_id")
    parser.add_argument("--json-out", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    cases = load_failure_regressions(args.corpus, case_id=args.case_id)
    report = run_failure_regressions(cases)
    payload = report.as_dict()

    print(f"Regression cases          {payload['matches']} / {payload['cases']}")
    print(f"Critical misses           {payload['critical_misses']}")
    print("Failure classes           " + ", ".join(sorted(payload["failure_class_coverage"])))

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
