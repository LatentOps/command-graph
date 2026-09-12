from __future__ import annotations

import argparse
import json
from pathlib import Path

from ordin.integration_conformance import run_integration_conformance


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the shared Ordin adapter integration conformance suite."
    )
    parser.add_argument("--json-out", type=Path)
    return parser


def main() -> int:
    args = _parser().parse_args()
    report = run_integration_conformance()
    payload = report.as_dict()

    print(f"Conformance checks        {payload['passed']} / {payload['checks']}")
    print("Integrations              " + ", ".join(payload["integrations"]))

    if args.json_out is not None:
        args.json_out.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    errors = report.errors()
    if errors:
        print("\nConformance errors:")
        for error in errors:
            print(f"- {error}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
