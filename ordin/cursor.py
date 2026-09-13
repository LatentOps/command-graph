"""Cursor command hooks over Ordin's shared action and session boundary."""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from . import __version__
from .action import ActionEnvelope
from .action_policy import load_action_policy
from .adapters import MCPAdapter, ToolCallAdapter
from .agent import AgentDecision, AgentGate
from .api import Ordin
from .audit import JsonlAuditSink
from .context import ExecutionContext
from .execution import ActionObservation
from .mcp_proxy import _parse_jsonrpc_line
from .policy import ReviewPolicy, validate_fail_threshold
from .session import IntegrationSession, SessionIdentity, SqliteSessionStore
from .tool_calls import (
    ToolResourceBinding,
    ToolSemanticRule,
    ToolSemanticsRegistry,
    load_tool_semantics,
)
from .trace_capture import TraceRecorder, attach_trace, digest, raw_capture_flag


CURSOR_RUNTIME = "cursor"
CURSOR_CONTRACT = "2026-09-12"
MAX_HOOK_BYTES = 1_048_576
_BUILTINS = {"Shell", "Read", "Write", "StrReplace", "Delete", "Task"}


def _text(value: Any, name: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{name} requires bounded non-empty text")
    return value


def _identity(payload: Mapping[str, Any]) -> SessionIdentity:
    conversation = _text(payload.get("conversation_id"), "conversation_id")
    if payload.get("session_id") is not None and payload["session_id"] != conversation:
        raise ValueError("Cursor session and conversation identities disagree")
    child = payload.get("subagent_id")
    if child is not None:
        child = _text(child, "subagent_id")
    parent = payload.get("parent_conversation_id")
    if parent is not None:
        _text(parent, "parent_conversation_id")
        if child is None:
            raise ValueError("subagent context requires an explicit subagent identity")
    return SessionIdentity(CURSOR_RUNTIME, digest([conversation, child, parent]))


def load_cursor_mcp_map(path: str | Path) -> Mapping[str, tuple[str, str]]:
    from ._json_contracts import load_configuration
    from .schema import validate_named_schema

    payload = load_configuration(path, label="Cursor MCP mapping", maximum=MAX_HOOK_BYTES)
    if validate_named_schema("cursor_mcp_map", payload):
        raise ValueError("invalid Cursor MCP mapping schema")
    result = {}
    for entry in payload["tools"]:
        alias = _text(entry["hook_name"], "MCP alias", 256)
        if alias in result or alias in _BUILTINS:
            raise ValueError("Cursor MCP aliases must be unique and cannot shadow built-ins")
        MCPAdapter(server=entry["server"]).adapt(entry["tool"], {})
        result[alias] = (entry["server"], entry["tool"])
    return MappingProxyType(result)


def build_cursor_integration(
    *,
    semantics_path: str | Path | None = None,
    policy_path: str | Path | None = None,
    mcp_map_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    trace_path: str | Path | None = None,
    raw_local: bool = False,
    fail_on: str = "warn",
) -> CursorIntegration:
    aliases = load_cursor_mcp_map(mcp_map_path) if mcp_map_path else {}
    rules = [
        ToolSemanticRule(
            id="cursor-" + name.lower(),
            kind="tool",
            runtime=CURSOR_RUNTIME,
            tool=name,
            effects=effects,
            resources=(ToolResourceBinding("path", "path"),),
        )
        for name, effects in [
            ("Read", ("filesystem.read",)),
            ("Write", ("filesystem.write",)),
            ("StrReplace", ("filesystem.write",)),
            ("Delete", ("filesystem.delete",)),
        ]
    ]
    if semantics_path:
        rules.extend(load_tool_semantics(semantics_path).registry.rules)
    registry = ToolSemanticsRegistry("ordin.cursor", "1", tuple(rules))
    ordin = Ordin(
        tool_semantics=registry,
        action_policy=load_action_policy(policy_path) if policy_path else None,
        policy=ReviewPolicy(validate_fail_threshold(fail_on)),
        audit=JsonlAuditSink(audit_path) if audit_path else None,
    )
    ordin, trace = attach_trace(
        ordin,
        trace_path,
        integration=CURSOR_RUNTIME,
        raw_local=raw_local,
        configuration={"aliases": dict(aliases), "contract": CURSOR_CONTRACT},
    )
    return CursorIntegration(gate=AgentGate(ordin), mcp_map=aliases, trace=trace)


def _default_gate() -> AgentGate:
    return build_cursor_integration().gate


@dataclass(frozen=True)
class CursorIntegration:
    gate: AgentGate = field(default_factory=_default_gate)
    mcp_map: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    session: IntegrationSession | None = None
    trace: TraceRecorder | None = None

    def __post_init__(self) -> None:
        if len(self.mcp_map) > 256:
            raise ValueError("too many Cursor MCP aliases")
        for alias, identity in self.mcp_map.items():
            _text(alias, "MCP alias", 256)
            if alias in _BUILTINS or not isinstance(identity, tuple) or len(identity) != 2:
                raise ValueError("Cursor mapping requires distinct exact server/tool identities")
            MCPAdapter(server=identity[0]).adapt(identity[1], {})
        object.__setattr__(self, "mcp_map", MappingProxyType(dict(self.mcp_map)))

    def adapt(self, payload: Mapping[str, Any]) -> ActionEnvelope:
        if payload.get("hook_event_name") != "preToolUse":
            raise ValueError("expected Cursor preToolUse")
        identity = _identity(payload)
        _text(payload.get("generation_id"), "generation_id")
        version = _text(payload.get("cursor_version"), "cursor_version", 256)
        tool = _text(payload.get("tool_name"), "tool_name", 256)
        tool_id = _text(payload.get("tool_use_id"), "tool_use_id")
        arguments = payload.get("tool_input")
        if not isinstance(arguments, Mapping):
            raise ValueError("Cursor tool_input must be an object")
        cwd = _text(payload.get("cwd"), "cwd")
        roots = payload.get("workspace_roots", [])
        if (
            not isinstance(roots, list)
            or len(roots) > 32
            or any(not isinstance(root, str) for root in roots)
        ):
            raise ValueError("Cursor workspace_roots must be a bounded string array")
        repo_root = roots[0] if len(roots) == 1 else None
        if tool == "Shell" and "working_directory" in arguments:
            cwd = _text(arguments["working_directory"], "working_directory")
        context = ExecutionContext(cwd=cwd, repo_root=repo_root, agent=CURSOR_RUNTIME)
        action_id = "cursor:" + digest([identity.key, payload["generation_id"], tool_id, tool])
        if tool in {"Read", "Write", "StrReplace", "Delete"}:
            paths = [arguments[name] for name in ("path", "file_path") if name in arguments]
            if (
                not paths
                or any(not isinstance(path, str) or not path for path in paths)
                or len(set(paths)) != 1
            ):
                raise ValueError("Cursor file tools require one unambiguous path")
            # File contents and patch text are unnecessary for static file effects.
            arguments = {"path": paths[0], "input_digest": digest(dict(arguments))}
        elif tool == "Task":
            arguments = {"input_digest": digest(dict(arguments))}
        if tool in self.mcp_map:
            server, name = self.mcp_map[tool]
            observed_server = payload.get("mcp_server_name")
            if observed_server is not None and observed_server != server:
                raise ValueError("Cursor MCP server identity differs from reviewed mapping")
            action = MCPAdapter(server=server).adapt(
                name, arguments, context=context, action_id=action_id
            )
        else:
            if payload.get("mcp_server_name") is not None:
                raise ValueError("Cursor MCP calls require an explicit alias mapping")
            action = ToolCallAdapter(
                runtime=CURSOR_RUNTIME, shell_tools=frozenset({"Shell"})
            ).adapt(tool, arguments, context=context, action_id=action_id)
        return replace(
            action,
            parameters={
                **action.parameters,
                "integration": {
                    "runtime": CURSOR_RUNTIME,
                    "session_id_sha256": identity.key,
                    "tool_use_id_sha256": digest(tool_id),
                    "host_version_sha256": digest(version),
                },
            },
        )

    def review_pre_tool(self, payload: Mapping[str, Any]) -> AgentDecision:
        if self.session is not None:
            self.session.require_identity(_identity(payload))
            if self.session.gate is not self.gate:
                raise ValueError("Cursor integration must use its session gate")
            return self.session.evaluate(self.adapt(payload))
        return self.gate.evaluate_action(self.adapt(payload))

    def pre_tool_output(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            decision = self.review_pre_tool(payload)
            return {
                "permission": "allow" if decision.may_execute else "deny",
                "user_message": f"Ordin {decision.review.decision}/{decision.review.risk}",
            }
        except (ValueError, OSError, TypeError, RecursionError):
            return {
                "permission": "deny",
                "user_message": "Ordin rejected invalid or unavailable Cursor review input",
            }

    def observation_from_hook(
        self, payload: Mapping[str, Any], *, observed_effects: tuple[str, ...] = ()
    ) -> ActionObservation | None:
        event = payload.get("hook_event_name")
        if event not in {"postToolUse", "postToolUseFailure"}:
            raise ValueError("expected Cursor post-tool event")
        identity = _identity(payload)
        if self.session is not None:
            self.session.require_identity(identity)
            if self.session.gate is not self.gate:
                raise ValueError("Cursor integration must use its session gate")
        action = self.adapt({**payload, "hook_event_name": "preToolUse"})
        exit_code = None
        status = "reported"
        if event == "postToolUseFailure":
            failure = payload.get("failure_type")
            if failure not in {"timeout", "error", "permission_denied"}:
                raise ValueError("unknown Cursor failure type")
            if failure == "permission_denied":
                return None
            status = "failure"
        else:
            output = payload.get("tool_output")
            if not isinstance(output, str):
                raise ValueError("Cursor tool_output must be JSON-encoded text")
            value = _parse_jsonrpc_line(output.encode())
            candidate = value.get("exitCode")
            if candidate is not None:
                if isinstance(candidate, bool) or not isinstance(candidate, int):
                    raise ValueError("Cursor exitCode must be integer")
                exit_code = candidate
                status = "success" if candidate == 0 else "failure"
        assert action.action_id is not None
        observation = ActionObservation(
            action.action_id,
            exit_code=exit_code,
            effects=observed_effects,
            metadata={"runtime": CURSOR_RUNTIME, "status": status},
        )
        if self.trace is not None:
            self.trace.record_observation(observation, session_key=identity.key)
        if self.session is not None:
            self.session.observe(observation)
        return observation

    def review_mcp(self, payload: Mapping[str, Any]) -> AgentDecision:
        """Legacy specific MCP hooks lack a unique pre/post ID; review statelessly."""
        if payload.get("hook_event_name") != "beforeMCPExecution" or self.session or self.trace:
            raise ValueError("specific MCP hooks require the stateless, non-capturing profile")
        _identity(payload)
        arguments = payload.get("tool_input")
        if not isinstance(arguments, str):
            raise ValueError("Cursor MCP arguments must be JSON text")
        action = MCPAdapter(
            server=_text(payload.get("mcp_server_name"), "mcp_server_name", 256)
        ).adapt(
            _text(payload.get("tool_name"), "tool_name", 256),
            _parse_jsonrpc_line(arguments.encode()),
        )
        return self.gate.evaluate_action(action)

    def review_subagent(self, payload: Mapping[str, Any]) -> AgentDecision:
        if payload.get("hook_event_name") != "subagentStart":
            raise ValueError("expected Cursor subagentStart")
        child = _text(payload.get("subagent_id"), "subagent_id")
        parent = _text(payload.get("parent_conversation_id"), "parent_conversation_id")
        if payload.get("conversation_id") != parent:
            raise ValueError("ambiguous Cursor parent conversation")
        call = _text(payload.get("tool_call_id"), "tool_call_id")
        identity = _identity(
            {
                key: value
                for key, value in payload.items()
                if key not in {"subagent_id", "parent_conversation_id"}
            }
        )
        action = ActionEnvelope(
            kind="tool",
            operation="call",
            action_id="cursor:" + digest([identity.key, call, child, "subagentStart"]),
            parameters={
                "runtime": CURSOR_RUNTIME,
                "tool": "Task",
                "arguments": {
                    "subagent_type": _text(payload.get("subagent_type"), "subagent_type"),
                    "subagent_id_digest": digest(child),
                },
                "integration": {"runtime": CURSOR_RUNTIME, "session_id_sha256": identity.key},
            },
            context=ExecutionContext(agent=CURSOR_RUNTIME),
        )
        if self.session:
            self.session.require_identity(identity)
            if self.session.gate is not self.gate:
                raise ValueError("Cursor integration must use its session gate")
            return self.session.evaluate(action)
        return self.gate.evaluate_action(action)


def _configured() -> CursorIntegration:
    return build_cursor_integration(
        semantics_path=os.environ.get("ORDIN_CURSOR_SEMANTICS") or None,
        policy_path=os.environ.get("ORDIN_CURSOR_POLICY") or None,
        mcp_map_path=os.environ.get("ORDIN_CURSOR_MCP_MAP") or None,
        audit_path=os.environ.get("ORDIN_CURSOR_AUDIT") or None,
        trace_path=os.environ.get("ORDIN_CURSOR_TRACE") or None,
        raw_local=raw_capture_flag(os.environ.get("ORDIN_CURSOR_TRACE_RAW", "0")),
        fail_on=os.environ.get("ORDIN_CURSOR_FAIL_ON", "warn"),
    )


def install_hooks(path: Path) -> None:
    if os.name != "posix":
        raise ValueError("Cursor hook installation requires POSIX shells")
    executable = shlex.quote(sys.executable)
    hooks = {
        event: [{"command": f"{executable} -I -m ordin.cursor {mode} || exit 2", "timeout": 30}]
        for event, mode in [
            ("preToolUse", "pre"),
            ("postToolUse", "post"),
            ("postToolUseFailure", "post-failure"),
            ("sessionStart", "session-start"),
            ("sessionEnd", "session-end"),
            ("subagentStart", "subagent-start"),
            ("subagentStop", "subagent-stop"),
        ]
    }
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "hooks": hooks}, handle, indent=2)
        handle.write("\n")


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--help"], ["-h"]):
        print(
            "usage: ordin-cursor-hook {pre|post|post-failure|mcp-pre|session-start|session-reset|session-end|subagent-start|subagent-stop|doctor} | install PATH"
        )
        return 0
    mode = args[0] if args else ""
    try:
        if len(args) == 2 and mode == "install":
            install_hooks(Path(args[1]))
            print(json.dumps({"ok": True, "host_trust_required": True}))
            return 0
        integration = _configured()
        if args == ["doctor"]:
            print(
                json.dumps(
                    {
                        "ok": True,
                        "runtime": CURSOR_RUNTIME,
                        "ordin_version": __version__,
                        "contract": CURSOR_CONTRACT,
                        "state_enabled": bool(os.environ.get("ORDIN_CURSOR_STATE")),
                        "mcp_aliases": len(integration.mcp_map),
                        "pre_tool_ask_enforced": False,
                        "host_enablement_verified": False,
                    }
                )
            )
            return 0
        events = {
            "pre": "preToolUse",
            "post": "postToolUse",
            "post-failure": "postToolUseFailure",
            "mcp-pre": "beforeMCPExecution",
            "session-start": "sessionStart",
            "session-reset": "sessionStart",
            "session-end": "sessionEnd",
            "subagent-start": "subagentStart",
            "subagent-stop": "subagentStop",
        }
        if len(args) != 1 or mode not in events:
            raise ValueError("invalid Cursor hook mode")
        raw = sys.stdin.read(MAX_HOOK_BYTES + 1).encode()
        if len(raw) > MAX_HOOK_BYTES:
            raise ValueError("Cursor hook exceeds byte limit")
        payload = _parse_jsonrpc_line(raw)
        if payload.get("hook_event_name") != events[mode]:
            raise ValueError("Cursor hook event/mode mismatch")
        identity = _identity(payload)
        if mode == "subagent-start":
            identity = _identity(
                {
                    key: value
                    for key, value in payload.items()
                    if key not in {"subagent_id", "parent_conversation_id"}
                }
            )
        if mode == "subagent-stop" and not payload.get("subagent_id"):
            raise ValueError("subagent cleanup requires an explicit child identity")
        output: dict[str, Any] = {}

        def run(active: CursorIntegration) -> None:
            nonlocal output
            if mode in {"pre", "mcp-pre", "subagent-start"}:
                decision = (
                    active.review_pre_tool(payload)
                    if mode == "pre"
                    else active.review_mcp(payload)
                    if mode == "mcp-pre"
                    else active.review_subagent(payload)
                )
                output = {
                    "permission": "allow" if decision.may_execute else "deny",
                    "user_message": f"Ordin {decision.review.decision}/{decision.review.risk}",
                }
            elif mode in {"post", "post-failure"}:
                active.observation_from_hook(payload)
            elif mode in {"session-end", "subagent-stop"} and active.session:
                active.session.end()
            if mode.startswith("session-") and active.trace:
                active.trace.record_boundary(identity.key)

        state_path = os.environ.get("ORDIN_CURSOR_STATE")
        if state_path:
            with SqliteSessionStore(state_path).transaction(
                identity,
                integration.gate,
                create=mode in {"session-start", "session-reset"},
                reset=mode == "session-reset",
            ) as session:
                run(replace(integration, session=session))
        else:
            run(integration)
        if mode == "subagent-start" and output.get("permission") == "allow" and state_path:
            with SqliteSessionStore(state_path).transaction(
                _identity(payload), integration.gate, create=True
            ):
                pass
        print(json.dumps(output, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, RecursionError):
        if mode in {"pre", "mcp-pre", "subagent-start"}:
            print(
                json.dumps(
                    {
                        "permission": "deny",
                        "user_message": "Ordin Cursor review unavailable; check configuration and session state",
                    }
                )
            )
        else:
            print(
                "ordin-cursor-hook: invalid input or unavailable configuration/state",
                file=sys.stderr,
            )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
