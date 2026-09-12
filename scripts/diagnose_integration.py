from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from ordin import ActionEnvelope, Ordin
from ordin.diagnostics import action_review_diagnostic, integration_health


def _load_action(path: Path | None) -> ActionEnvelope:
    if path is None:
        raw = sys.stdin.read()
        source = "stdin"
    else:
        raw = path.read_text(encoding="utf-8")
        source = str(path)
    if not raw.strip():
        raise ValueError(f"{source} action payload is empty")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid action JSON from {source}: {exc.msg}") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("action diagnostic input must be a JSON object")
    return ActionEnvelope.from_dict(payload)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Inspect Ordin integration health or a redacted action decision."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    health = subparsers.add_parser("health", help="Run first-party integration conformance checks.")
    health.add_argument("--json", action="store_true")

    action = subparsers.add_parser(
        "action", help="Review one action and print redacted diagnostics."
    )
    source = action.add_mutually_exclusive_group(required=True)
    source.add_argument("--file", type=Path)
    source.add_argument("--stdin", action="store_true")
    action.add_argument("--json", action="store_true")
    return parser


def _print_health(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(f"ok: {str(payload['ok']).lower()}")
    print(f"integrations: {', '.join(payload['integrations'])}")
    print(f"checks: {payload['passed']} / {payload['checks']}")
    if payload["errors"]:
        print("errors:")
        for error in payload["errors"]:
            print(f"- {error}")


def _print_action(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    action = payload["action"]
    decision = payload["decision"]
    contributors = payload["contributors"]
    print(f"action: {action['kind']}/{action['operation']}")
    if action["identity"]:
        identity = ", ".join(f"{key}={value}" for key, value in action["identity"].items())
        print(f"identity: {identity}")
    print(f"decision: {decision['decision']}")
    print(f"risk: {decision['risk']}")
    print(f"uncertain: {str(decision['uncertain']).lower()}")
    if decision["effects"]:
        print("effects: " + ", ".join(decision["effects"]))
    if decision["resource_types"]:
        print("resource_types: " + ", ".join(decision["resource_types"]))
    print(f"adapter: {contributors['adapter'] or 'none'}")
    if payload["remediation"]:
        print("remediation:")
        for item in payload["remediation"]:
            print(f"- {item['code']}: {item['message']}")


def main() -> int:
    args = _parser().parse_args()
    if args.command == "health":
        payload = integration_health()
        _print_health(payload, as_json=args.json)
        return 0 if payload["ok"] else 1

    try:
        action = _load_action(None if args.stdin else args.file)
        review = Ordin().review_action(action)
    except (OSError, ValueError) as exc:
        payload = {
            "schema_version": "ordin.integration_diagnostic_error.v1",
            "error": "invalid_action",
            "message": str(exc),
        }
        if args.json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(f"invalid action: {exc}")
        return 2

    payload = action_review_diagnostic(review)
    _print_action(payload, as_json=args.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
