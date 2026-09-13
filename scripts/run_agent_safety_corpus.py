"""Run every v1 corpus case offline and write new JSON/Markdown reports."""

import argparse
import json
from pathlib import Path

from ordin.agent_safety_corpus import evaluate, render_markdown


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--corpus",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "benchmarks/agent-safety-corpus/v1/cases.json",
    )
    parser.add_argument("--revision", required=True)
    parser.add_argument(
        "--output", required=True, type=Path, help="New directory; refuses overwrite"
    )
    args = parser.parse_args()
    report = evaluate(args.corpus, revision=args.revision)
    args.output.mkdir(mode=0o700, parents=True, exist_ok=False)
    (args.output / "report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    (args.output / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(
        json.dumps(
            {"ok": report["ok"], "cases": report["scope"]["cases"], "errors": report["errors"]}
        )
    )
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
