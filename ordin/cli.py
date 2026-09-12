from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence

from .action import ActionEnvelope, ActionHistory, review_action
from .action_policy import load_action_policy
from .context import ExecutionContext, ReviewRequest
from .data import data_health, find_command
from .enforcement import enforcement_exit_code
from .graph import build_effect_graph
from .indexer import DEFAULT_INDEX_PATH, build_index, build_index_from_lines
from .packs import pack_list_payload
from .review import review_command
from .risk import check_command
from .schema import validate_named_schema
from .search import search
from .shell_integration import render_shell_init
from .temporal import load_temporal_policy


def print_search(query: str, limit: int, as_json: bool = False) -> None:
    results = search(query, limit=limit)
    if as_json:
        print(json.dumps([result.as_dict() for result in results], indent=2))
        return

    if not results:
        print("No command matches found.")
        return

    for result in results:
        print(result.command)
        print(f"  summary: {result.summary}")
        print(f"  why: {result.why}")
        if result.example:
            print(f"  example: {result.example}")
        if result.suggested_commands:
            print("  suggested:")
            for suggestion in result.suggested_commands:
                print(f"  - {suggestion['command']}")
                if suggestion.get("description"):
                    print(f"    {suggestion['description']}")
        print(f"  risk: {result.risk}")
        print()


def print_explain(command_name: str, as_json: bool = False) -> int:
    command = find_command(command_name)
    if command is None:
        if as_json:
            print(json.dumps({"error": "command_not_found", "command": command_name}, indent=2))
        else:
            print(f'No command card found for "{command_name}".')
        return 1

    if as_json:
        print(json.dumps(command, indent=2))
        return 0

    print(command["command"])
    print(f"  summary: {command['summary']}")
    print(f"  default_risk: {command.get('default_risk', 'unknown')}")
    if command.get("effects"):
        print("  effects:")
        for effect in command["effects"]:
            effect_name = effect.get("effect") if isinstance(effect, dict) else effect
            print(f"  - {effect_name}")
    if command.get("intents"):
        print("  intents:")
        for intent in command["intents"]:
            print(f"  - {intent}")
    if command.get("examples"):
        print("  examples:")
        for example in command["examples"]:
            print(f"  - {example['command']}")
            print(f"    {example['explanation']}")
    return 0


def print_graph(as_json: bool = False) -> int:
    graph = build_effect_graph()
    payload = graph.as_dict()
    if as_json:
        print(json.dumps(payload, indent=2))
        return 0

    node_types: dict[str, int] = {}
    for node in graph.nodes.values():
        node_types[node.type] = node_types.get(node.type, 0) + 1

    print(f"schema_version: {payload['schema_version']}")
    print(f"nodes: {len(graph.nodes)}")
    print(f"edges: {len(graph.edges)}")
    for node_type in sorted(node_types):
        print(f"{node_type}_nodes: {node_types[node_type]}")
    return 0


def print_packs(as_json: bool = False) -> int:
    payload = pack_list_payload()
    if as_json:
        print(json.dumps(payload, indent=2))
        return 0
    if not payload["packs"]:
        print("No command packs discovered.")
        return 0
    for pack in payload["packs"]:
        state = "loaded" if pack["loaded"] else "disabled"
        default = ", default" if pack["enabled_by_default"] else ""
        print(f"{pack['name']} {pack['version']} ({state}{default})")
        print(f"  {pack['description']}")
        print(f"  commands: {pack['command_count']}")
        print(f"  risk_rule_files: {pack['risk_rule_file_count']}")
        print(f"  analyzers: {', '.join(pack['analyzers']) or 'none'}")
    return 0


def _add_context_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--cwd")
    parser.add_argument("--shell")
    parser.add_argument("--euid", type=int)
    parser.add_argument("--repo-root")
    parser.add_argument("--agent")
    parser.add_argument(
        "--context-json",
        help="JSON object containing execution context fields.",
    )
    interactive_group = parser.add_mutually_exclusive_group()
    interactive_group.add_argument(
        "--interactive",
        dest="context_interactive",
        action="store_true",
    )
    interactive_group.add_argument(
        "--non-interactive",
        dest="context_interactive",
        action="store_false",
    )
    parser.set_defaults(context_interactive=None)


def _add_enforcement_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--enforce",
        action="store_true",
        help="Return a non-zero exit code for decisions at or above the threshold.",
    )
    parser.add_argument(
        "--fail-on",
        choices=["warn", "ask", "block"],
        help="Enforcement threshold; supplying this option implies enforcement.",
    )


def _context_from_args(args: argparse.Namespace) -> ExecutionContext | None:
    payload: dict = {}
    raw_context = getattr(args, "context_json", None)
    if raw_context:
        try:
            parsed = json.loads(raw_context)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid --context-json: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("--context-json must decode to a JSON object")
        payload.update(parsed)

    overrides = {
        "cwd": getattr(args, "cwd", None),
        "shell": getattr(args, "shell", None),
        "euid": getattr(args, "euid", None),
        "repo_root": getattr(args, "repo_root", None),
        "agent": getattr(args, "agent", None),
        "interactive": getattr(args, "context_interactive", None),
    }
    for key, value in overrides.items():
        if value is not None:
            payload[key] = value

    if not payload:
        return None
    return ExecutionContext.from_dict(payload)


def _context_args_present(args: argparse.Namespace) -> bool:
    return any(
        value is not None
        for value in (
            getattr(args, "cwd", None),
            getattr(args, "shell", None),
            getattr(args, "euid", None),
            getattr(args, "repo_root", None),
            getattr(args, "agent", None),
            getattr(args, "context_json", None),
            getattr(args, "context_interactive", None),
        )
    )


def _input_error(message: str, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": "invalid_review_request", "message": message}, indent=2))
    else:
        print(f"invalid review request: {message}")
    return 2


def _history_error(exc: ValueError, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": "invalid_action_history", "message": str(exc)}, indent=2))
    else:
        print(f"invalid action history: {exc}")
    return 2


def _temporal_policy_error(exc: ValueError, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": "invalid_temporal_policy", "message": str(exc)}, indent=2))
    else:
        print(f"invalid temporal policy: {exc}")
    return 2


def _policy_error(exc: ValueError, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": "invalid_policy", "message": str(exc)}, indent=2))
    else:
        print(f"invalid policy: {exc}")
    return 2


def _context_error(exc: ValueError, as_json: bool) -> int:
    if as_json:
        print(json.dumps({"error": "invalid_context", "message": str(exc)}, indent=2))
    else:
        print(f"invalid context: {exc}")
    return 2


def _read_stdin_review_request() -> ReviewRequest:
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("stdin review request is empty")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON on stdin: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("stdin review request must be a JSON object")
    schema_errors = validate_named_schema("review_request", payload)
    if schema_errors:
        raise ValueError("schema validation failed: " + "; ".join(schema_errors))
    return ReviewRequest.from_dict(payload)


def _read_stdin_action_envelope() -> ActionEnvelope:
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("stdin action envelope is empty")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON on stdin: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("stdin action envelope must be a JSON object")
    schema_errors = validate_named_schema("action_envelope", payload)
    if schema_errors:
        raise ValueError("schema validation failed: " + "; ".join(schema_errors))
    return ActionEnvelope.from_dict(payload)


def _read_action_history_file(path: str) -> ActionHistory:
    history_path = Path(path)
    try:
        payload = json.loads(history_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"cannot read action history {history_path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid action history JSON: {exc.msg}") from exc
    if not isinstance(payload, dict):
        raise ValueError("action history file must contain a JSON object")
    errors = validate_named_schema("action_history", payload)
    if errors:
        raise ValueError("action history schema validation failed: " + "; ".join(errors))
    return ActionHistory.from_dict(payload)


def _review_exit(args: argparse.Namespace, decision: str) -> int:
    return enforcement_exit_code(
        decision,
        enforce=getattr(args, "enforce", False),
        fail_on=getattr(args, "fail_on", None),
    )


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args and raw_args[0] == "contracts":
        from .contracts_cli import main as contracts_main

        return contracts_main(raw_args[1:])
    parser = argparse.ArgumentParser(
        prog="ordin",
        description="Intent-aware command discovery and safety checks.",
    )
    subparsers = parser.add_subparsers(dest="command_name", required=True)
    subparsers.add_parser("contracts", help="Validate and compare reviewed MCP contract pins")

    search_parser = subparsers.add_parser("search", help="Search commands by intent.")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=5)
    search_parser.add_argument("--json", action="store_true")

    explain_parser = subparsers.add_parser("explain", help="Show a command card.")
    explain_parser.add_argument("command")
    explain_parser.add_argument("--json", action="store_true")

    graph_parser = subparsers.add_parser(
        "graph",
        help="Inspect the typed command/effect graph.",
    )
    graph_parser.add_argument("--json", action="store_true")

    packs_parser = subparsers.add_parser(
        "packs",
        help="Inspect discovered and loaded command packs.",
    )
    packs_parser.add_argument("--json", action="store_true")

    shell_init_parser = subparsers.add_parser(
        "shell-init",
        help="Print opt-in interactive shell integration.",
    )
    shell_init_parser.add_argument("shell", choices=["bash", "zsh"])

    check_parser = subparsers.add_parser("check", help="Review a command for risk.")
    check_parser.add_argument("command")
    check_parser.add_argument("--json", action="store_true")
    _add_context_arguments(check_parser)
    _add_enforcement_arguments(check_parser)

    review_parser = subparsers.add_parser(
        "review",
        help="Review a command with optional user intent.",
    )
    review_source = review_parser.add_mutually_exclusive_group(required=True)
    review_source.add_argument("--command")
    review_source.add_argument(
        "--stdin",
        action="store_true",
        help="Read a ordin.review_request.v1 object from stdin.",
    )
    review_parser.add_argument("--intent")
    review_parser.add_argument("--json", action="store_true")
    _add_context_arguments(review_parser)
    _add_enforcement_arguments(review_parser)

    action_parser = subparsers.add_parser(
        "action",
        help="Review a versioned generic action envelope from stdin.",
    )
    action_parser.add_argument(
        "--stdin",
        action="store_true",
        required=True,
        help="Read an ordin.action_envelope.v1 object from stdin.",
    )
    action_parser.add_argument("--json", action="store_true")
    action_parser.add_argument(
        "--policy",
        help="Explicit local ordin.policy_set.v1 JSON file to apply.",
    )
    action_parser.add_argument(
        "--history",
        help="Explicit local ordin.action_history.v1 JSON file.",
    )
    action_parser.add_argument(
        "--temporal-policy",
        help="Explicit local ordin.temporal_policy_set.v1 JSON file; defaults to bundled rules.",
    )
    _add_enforcement_arguments(action_parser)

    policy_parser = subparsers.add_parser(
        "policy",
        help="Validate local declarative action policies.",
    )
    policy_subparsers = policy_parser.add_subparsers(dest="policy_command", required=True)
    policy_validate_parser = policy_subparsers.add_parser(
        "validate", help="Validate and compile a local policy file."
    )
    policy_validate_parser.add_argument("path")
    policy_validate_parser.add_argument("--json", action="store_true")

    temporal_parser = subparsers.add_parser(
        "temporal",
        help="Validate bounded temporal action policies.",
    )
    temporal_subparsers = temporal_parser.add_subparsers(dest="temporal_command", required=True)
    temporal_validate_parser = temporal_subparsers.add_parser(
        "validate", help="Validate and compile a temporal policy file."
    )
    temporal_validate_parser.add_argument("path")
    temporal_validate_parser.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="Check local Ordin data.")
    doctor_parser.add_argument("--json", action="store_true")

    index_parser = subparsers.add_parser(
        "index",
        help="Build a local command index from apropos or man -k output.",
    )
    index_parser.add_argument("--source", choices=["apropos", "man-k"], default="apropos")
    index_parser.add_argument("--query", default=".")
    index_parser.add_argument("--input", help="Read saved apropos/man -k output from a file.")
    index_parser.add_argument("--output", default=str(DEFAULT_INDEX_PATH))
    index_parser.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)

    if args.command_name == "search":
        print_search(args.query, args.limit, as_json=args.json)
        return 0

    if args.command_name == "explain":
        return print_explain(args.command, as_json=args.json)

    if args.command_name == "graph":
        return print_graph(as_json=args.json)

    if args.command_name == "packs":
        return print_packs(as_json=args.json)

    if args.command_name == "shell-init":
        print(render_shell_init(args.shell), end="")
        return 0

    if args.command_name == "check":
        try:
            context = _context_from_args(args)
        except ValueError as exc:
            return _context_error(exc, as_json=args.json)
        risk_review = check_command(args.command, context=context)
        if args.json:
            print(json.dumps(risk_review.as_dict(), indent=2))
        else:
            print(f"decision: {risk_review.decision}")
            print(f"risk: {risk_review.risk}")
            for reason in risk_review.reasons:
                print(f"- {reason}")
            if risk_review.safer_next_step:
                print(f"safer_next_step: {risk_review.safer_next_step}")
        return _review_exit(args, risk_review.decision)

    if args.command_name == "review":
        if args.stdin:
            if args.intent is not None or _context_args_present(args):
                return _input_error(
                    "--stdin cannot be combined with --intent or context flags",
                    as_json=args.json,
                )
            try:
                request = _read_stdin_review_request()
            except ValueError as exc:
                return _input_error(str(exc), as_json=args.json)
            command = request.command
            intent = request.intent
            context = request.context
            trace = request.trace
        else:
            try:
                context = _context_from_args(args)
            except ValueError as exc:
                return _context_error(exc, as_json=args.json)
            command = args.command
            intent = args.intent
            trace = None

        command_review = review_command(
            command,
            intent=intent,
            context=context,
            trace=trace,
        )
        if args.json:
            print(json.dumps(command_review.as_dict(), indent=2))
        else:
            print(f"decision: {command_review.decision}")
            print(f"risk: {command_review.risk}")
            for reason in command_review.reasons:
                print(f"- {reason}")
            if command_review.related_commands:
                print(f"related_commands: {', '.join(command_review.related_commands)}")
            if command_review.trajectory_categories:
                print("trajectory_categories: " + ", ".join(command_review.trajectory_categories))
            if command_review.safer_next_step:
                print(f"safer_next_step: {command_review.safer_next_step}")
        return _review_exit(args, command_review.decision)

    if args.command_name == "policy":
        if args.policy_command == "validate":
            try:
                compiled_policy = load_action_policy(args.path)
            except ValueError as exc:
                return _policy_error(exc, as_json=args.json)
            payload = {
                "schema_version": compiled_policy.policy.schema_version,
                "policy_id": compiled_policy.policy.policy_id,
                "version": compiled_policy.policy.version,
                "digest": compiled_policy.digest,
                "rule_count": len(compiled_policy.policy.rules),
            }
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(f"policy_id: {payload['policy_id']}")
                print(f"version: {payload['version']}")
                print(f"digest: {payload['digest']}")
                print(f"rules: {payload['rule_count']}")
            return 0

    if args.command_name == "temporal":
        if args.temporal_command == "validate":
            try:
                compiled_temporal = load_temporal_policy(args.path)
            except ValueError as exc:
                return _temporal_policy_error(exc, as_json=args.json)
            payload = {
                "schema_version": compiled_temporal.policy.schema_version,
                "policy_id": compiled_temporal.policy.policy_id,
                "version": compiled_temporal.policy.version,
                "rule_count": len(compiled_temporal.policy.rules),
            }
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(f"policy_id: {payload['policy_id']}")
                print(f"version: {payload['version']}")
                print(f"rules: {payload['rule_count']}")
            return 0

    if args.command_name == "action":
        try:
            action = _read_stdin_action_envelope()
        except ValueError as exc:
            return _input_error(str(exc), as_json=args.json)
        history = None
        if args.history:
            try:
                history = _read_action_history_file(args.history)
            except ValueError as exc:
                return _history_error(exc, as_json=args.json)
        temporal_policy = None
        if args.temporal_policy:
            try:
                temporal_policy = load_temporal_policy(args.temporal_policy)
            except ValueError as exc:
                return _temporal_policy_error(exc, as_json=args.json)
        action_review = review_action(action, history=history, temporal_policy=temporal_policy)
        if args.policy:
            try:
                action_policy = load_action_policy(args.policy)
            except ValueError as exc:
                return _policy_error(exc, as_json=args.json)
            action_review = action_policy.apply(action_review)
        if args.json:
            print(json.dumps(action_review.as_dict(), indent=2))
        else:
            print(f"decision: {action_review.decision}")
            print(f"risk: {action_review.risk}")
            for reason in action_review.reasons:
                print(f"- {reason}")
            if action_review.effects:
                print("effects: " + ", ".join(action_review.effects))
            if action_review.policy_matches:
                print(
                    "policy_matches: "
                    + ", ".join(item["rule_id"] for item in action_review.policy_matches)
                )
            if action_review.safer_next_step:
                print(f"safer_next_step: {action_review.safer_next_step}")
        return _review_exit(args, action_review.decision)

    if args.command_name == "doctor":
        health = data_health()
        if args.json:
            print(json.dumps(health, indent=2))
        else:
            print(f"commands: {health['command_count']}")
            print(f"risk_rules: {health['risk_rule_count']}")
            print(f"effects: {health['effect_count']}")
            print(f"schemas: {health.get('schema_count', 0)}")
            print(
                f"packs: {health.get('loaded_pack_count', 0)}/{health.get('pack_count', 0)} loaded"
            )
            if health.get("loaded_packs"):
                print(f"loaded_packs: {', '.join(health['loaded_packs'])}")
            print(f"pack_errors: {len(health.get('pack_errors', []))}")
            for error in health.get("pack_errors", []):
                print(f"- pack: {error}")
            print(f"schema_errors: {len(health.get('schema_errors', []))}")
            for error in health.get("schema_errors", []):
                print(f"- schema: {error}")
            print(f"risk_rule_errors: {len(health.get('risk_rule_errors', []))}")
            for error in health.get("risk_rule_errors", []):
                print(f"- risk_rule: {error}")
            print(f"template_errors: {len(health.get('template_errors', []))}")
            for error in health.get("template_errors", []):
                print(f"- template: {error}")
            print(f"resource_parity_errors: {len(health.get('resource_parity_errors', []))}")
            for error in health.get("resource_parity_errors", []):
                print(f"- resource: {error}")
            print(f"graph_nodes: {health['graph_node_count']}")
            print(f"graph_edges: {health['graph_edge_count']}")
            print(f"graph_errors: {len(health['graph_errors'])}")
            for error in health["graph_errors"]:
                print(f"- graph: {error}")
            print(f"missing_schema: {len(health['missing_schema'])}")
            print(f"duplicate_commands: {len(health['duplicate_commands'])}")
            print(f"ok: {str(health['ok']).lower()}")
        return 0 if health["ok"] else 1

    if args.command_name == "index":
        output = Path(args.output)
        try:
            if args.input:
                with Path(args.input).open("r", encoding="utf-8") as handle:
                    result = build_index_from_lines(
                        handle.read().splitlines(),
                        path=output,
                        source="file",
                    )
            else:
                result = build_index(path=output, source=args.source, query=args.query)
        except (OSError, RuntimeError, ValueError) as exc:
            payload = {"error": "index_failed", "message": str(exc)}
            if args.json:
                print(json.dumps(payload, indent=2))
            else:
                print(f"index failed: {exc}")
            return 1

        if args.json:
            print(json.dumps(result.as_dict(), indent=2))
        else:
            print(f"indexed: {result.entry_count}")
            print(f"skipped: {result.skipped_count}")
            print(f"path: {result.path}")
        return 0

    return 1
