from __future__ import annotations

import argparse
import json
from pathlib import Path

from ordin.runtime_evaluation import (
    DEFAULT_REPETITIONS,
    render_runtime_integration_markdown,
    run_runtime_integration_evaluation,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run executable-boundary coding-agent and MCP integration evaluation."
    )
    parser.add_argument("--revision", required=True)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    parser.add_argument("--repetitions", type=int, default=DEFAULT_REPETITIONS)
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = run_runtime_integration_evaluation(
        revision=args.revision,
        repo_root=args.repo_root,
        repetitions=args.repetitions,
        timeout_seconds=args.timeout_seconds,
    )
    payload = report.as_dict()
    markdown = render_runtime_integration_markdown(report)

    if args.json_out is not None:
        args.json_out.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.markdown_out is not None:
        args.markdown_out.write_text(markdown, encoding="utf-8")

    print(markdown)
    if report.errors:
        print("Errors:")
        for error in report.errors:
            print(f"- {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
