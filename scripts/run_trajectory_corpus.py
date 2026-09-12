from __future__ import annotations

import argparse
import json
from pathlib import Path

from ordin.trajectory_corpus import load_agent_trajectories, run_agent_trajectory_corpus


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = ROOT / "benchmarks" / "agent_trajectories.jsonl"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Replay the deterministic Ordin real-agent trajectory corpus."
    )
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--json-out", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    trajectories = load_agent_trajectories(args.corpus)
    report = run_agent_trajectory_corpus(trajectories)
    payload = report.as_dict()

    print(f"Trajectories              {payload['trajectory_matches']} / {payload['trajectories']}")
    print(f"Steps                     {payload['decision_matches']} / {payload['steps']} decisions")
    print(f"Contextual detection      {payload['contextual_detection_rate']:.1%}")
    print("Integration sources       " + ", ".join(sorted(payload["source_coverage"])))
    print("Behavior classes          " + ", ".join(sorted(payload["behavior_coverage"])))
    if payload["domain_coverage"]:
        print("Infrastructure domains    " + ", ".join(sorted(payload["domain_coverage"])))

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
