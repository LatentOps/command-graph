from __future__ import annotations

import argparse

from ordin import AgentGate, ExecutionContext


def main() -> int:
    parser = argparse.ArgumentParser(description="Fail CI unless Ordin permits a proposed command.")
    parser.add_argument("command", help="Command text to review; the example never executes it")
    args = parser.parse_args()

    result = AgentGate().evaluate(
        args.command,
        intent="CI pre-execution safety check",
        context=ExecutionContext(cwd=".", agent="ci-starter"),
    )
    print(f"decision={result.review.decision} risk={result.review.risk}")
    for reason in result.review.reasons:
        print(f"- {reason}")
    return 0 if result.may_execute else 1


if __name__ == "__main__":
    raise SystemExit(main())
