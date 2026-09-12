"""Local capture inspection and explicit regression promotion."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .action import ActionEnvelope
from .agent import AgentGate
from .api import Ordin
from .execution import ActionObservation
from .mcp_contracts import load_contract_json
from .session import IntegrationSession, SessionIdentity, configuration_digest
from .tool_calls import load_tool_semantics
from .trace_capture import TraceAuditSink, TraceRecorder, read_capture
from .trace_replay import (
    promote_candidate,
    replay_candidate,
    replay_integration_candidate,
    sanitize_capture,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ordin trace",
        description="Opt-in local action capture and reviewed regression promotion.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    record = commands.add_parser(
        "record", help="Review normalized action/observation JSON offline; never execute tools"
    )
    record.add_argument(
        "input",
        help="JSON object with an events array of existing action and observation contracts",
    )
    record.add_argument("--capture", required=True)
    record.add_argument("--session", required=True)
    record.add_argument("--semantics")
    record.add_argument(
        "--raw-local", action="store_true", help="Retain raw normalized actions; unsafe to share"
    )
    inspect = commands.add_parser(
        "inspect", help="Summarize sessions and decision friction without raw values"
    )
    inspect.add_argument("capture")
    sanitize = commands.add_parser(
        "sanitize", help="Print a candidate for review; no benchmark writes"
    )
    sanitize.add_argument("capture")
    sanitize.add_argument("--session-key")
    sanitize.add_argument("--start", type=int, default=1)
    sanitize.add_argument("--end", type=int)
    sanitize.add_argument("--expected", required=True, choices=("allow", "warn", "ask", "block"))
    sanitize.add_argument("--category")
    sanitize.add_argument("--output", help="New candidate JSON file; existing files are refused")
    replay = commands.add_parser(
        "replay", help="Replay a reviewed candidate without any tool execution"
    )
    replay.add_argument("candidate")
    replay.add_argument("--integration", action="store_true")
    promote = commands.add_parser(
        "promote", help="Validate and append one reviewed fixture to an explicit destination"
    )
    promote.add_argument("candidate")
    promote.add_argument(
        "--target", required=True, choices=("trajectory", "failure", "extended", "conformance")
    )
    promote.add_argument("--output", required=True)
    for command in (record, inspect, sanitize, replay, promote):
        command.add_argument(
            "--json", action="store_true", help="Pretty-print machine-readable JSON"
        )
    args = parser.parse_args(argv)
    try:
        output: dict[str, Any]
        if args.command == "record":
            payload = load_contract_json(args.input)
            events = payload.get("events")
            if (
                set(payload) != {"events"}
                or not isinstance(events, list)
                or not 1 <= len(events) <= 64
            ):
                raise ValueError("record input requires 1 to 64 normalized events")
            ordin = Ordin(
                tool_semantics=load_tool_semantics(args.semantics) if args.semantics else None
            )
            recorder = TraceRecorder(
                args.capture,
                integration="python",
                raw_local=args.raw_local,
                session_id=args.session,
                config_digest=configuration_digest(AgentGate(ordin)),
            )
            from dataclasses import replace

            gate = AgentGate(replace(ordin, audit=TraceAuditSink(recorder)))
            session = IntegrationSession(SessionIdentity("python", args.session), gate)
            for event in events:
                if not isinstance(event, dict):
                    raise ValueError("trace events must be action/observation objects")
                if event.get("schema_version") == "ordin.action_observation.v1":
                    observation = ActionObservation.from_dict(event)
                    session.observe(observation)
                    recorder.record_observation(observation)
                else:
                    action = ActionEnvelope.from_dict(event)
                    # Direct capture identity comes from --session, never input metadata.
                    action = replace(
                        action,
                        parameters={
                            k: v for k, v in action.parameters.items() if k != "integration"
                        },
                    )
                    session.evaluate(action)
            output = {"ok": True, "recorded": len(events), "unsafe_to_share": args.raw_local}
        elif args.command == "inspect":
            capture = read_capture(args.capture)
            output = {
                "ok": True,
                "mode": capture["mode"],
                "unsafe_to_share": capture["unsafe_to_share"],
                "events": [
                    {
                        key: event[key]
                        for key in (
                            "sequence",
                            "session_key",
                            "action_key",
                            "event",
                            "integration",
                            "decision",
                            "disposition",
                        )
                        if key in event
                    }
                    for event in capture["events"]
                ],
            }
            output["candidate_ends"] = [
                event["sequence"]
                for event in capture["events"]
                if event.get("decision") in {"warn", "ask", "block"}
            ]
        elif args.command == "sanitize":
            output = sanitize_capture(
                args.capture,
                expected=args.expected,
                category=args.category,
                session_key=args.session_key,
                start=args.start,
                end=args.end,
            )
            if args.output:
                with Path(args.output).open("x", encoding="utf-8") as handle:
                    handle.write(json.dumps(output, indent=2, sort_keys=True) + "\n")
        elif args.command == "replay":
            candidate = load_contract_json(args.candidate)
            output = (
                replay_integration_candidate(candidate)
                if args.integration
                else replay_candidate(candidate)
            )
        else:
            output = promote_candidate(load_contract_json(args.candidate), args.target, args.output)
        print(json.dumps(output, indent=2 if args.json else None, sort_keys=True))
        return 0 if output.get("ok", True) else 1
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        print(json.dumps({"ok": False, "error": "invalid_trace", "message": str(exc)}))
        return 2
