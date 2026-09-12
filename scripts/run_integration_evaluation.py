from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ordin.integration_evaluation import render_markdown_report, run_integration_evaluation


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SAFETY = ROOT / "benchmarks" / "safety.jsonl"
DEFAULT_TRAJECTORIES = ROOT / "benchmarks" / "agent_trajectories.jsonl"
DEFAULT_REGRESSIONS = ROOT / "benchmarks" / "failure_regressions.jsonl"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the reproducible Ordin real-agent integration and safety evaluation."
    )
    parser.add_argument("--safety", type=Path, default=DEFAULT_SAFETY)
    parser.add_argument("--trajectories", type=Path, default=DEFAULT_TRAJECTORIES)
    parser.add_argument("--regressions", type=Path, default=DEFAULT_REGRESSIONS)
    parser.add_argument("--repetitions", type=int, default=20)
    parser.add_argument("--revision", default=os.environ.get("GITHUB_SHA", "working-tree"))
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = run_integration_evaluation(
        safety_path=args.safety,
        trajectory_path=args.trajectories,
        regression_path=args.regressions,
        revision=args.revision,
        repetitions=args.repetitions,
    )
    payload = report.as_dict()
    markdown = render_markdown_report(report)

    print(markdown)
    if args.json_out is not None:
        args.json_out.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.markdown_out is not None:
        args.markdown_out.write_text(markdown, encoding="utf-8")

    return 1 if report.regression_errors() else 0


if __name__ == "__main__":
    raise SystemExit(main())
