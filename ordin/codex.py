"""Codex lifecycle translation; execution and hook trust belong to Codex."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .action import ActionEnvelope, ActionHistory
from .action_policy import load_action_policy
from .adapters import MCPAdapter, ToolCallAdapter
from .agent import AgentDecision, AgentGate
from .api import Ordin
from .audit import JsonlAuditSink
from .claude_code import _append_private_jsonl, _required_text
from .context import ExecutionContext
from .execution import ActionObservation, ObservationHistory
from .mcp_proxy import _parse_jsonrpc_line
from .policy import ReviewPolicy, validate_fail_threshold
from .session import IntegrationSession, SessionIdentity, SqliteSessionStore
from .tool_calls import (
    ToolResourceBinding,
    ToolSemanticRule,
    ToolSemanticsRegistry,
    load_tool_semantics,
)


CODEX_RUNTIME = "codex"
CODEX_MCP_MAP_SCHEMA_VERSION = "ordin.codex_mcp_map.v1"
MAX_CODEX_INPUT_BYTES = 1_048_576
MAX_PATCH_PATHS = 64


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


def parse_patch_targets(command: str) -> dict[str, str]:
    """Extract exact patch targets without retaining source lines or executing it."""
    if not isinstance(command, str) or len(command.encode()) > MAX_CODEX_INPUT_BYTES:
        raise ValueError("patch input is invalid or exceeds its byte limit")
    lines = command.splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise ValueError("unsupported apply_patch framing")
    paths: list[str] = []
    operation = None
    can_move = False
    for line in lines[1:-1]:
        header = next(
            (
                prefix
                for prefix in (
                    "*** Add File: ",
                    "*** Update File: ",
                    "*** Delete File: ",
                    "*** Move to: ",
                )
                if line.startswith(prefix)
            ),
            None,
        )
        if header is not None:
            if header == "*** Move to: " and not can_move:
                raise ValueError("patch move must immediately follow an update header")
            path = _required_text(line[len(header) :], name="patch path")
            if any(character in path for character in ("\0", "\r", "\n")):
                raise ValueError("patch path contains control characters")
            if path not in paths:
                if len(paths) >= MAX_PATCH_PATHS:
                    raise ValueError("patch has too many resource targets")
                paths.append(path)
            can_move = header == "*** Update File: "
            operation = "update" if header == "*** Move to: " else header
        else:
            can_move = False
            if operation is None or operation == "*** Delete File: ":
                raise ValueError("unexpected patch content")
            if operation == "*** Add File: " and not line.startswith("+"):
                raise ValueError("invalid patch addition")
            if operation != "*** Add File: " and not (
                line.startswith((" ", "+", "-", "@@")) or line == "*** End of File"
            ):
                raise ValueError("unsupported patch hunk")
    if not paths:
        raise ValueError("patch contains no file operation")
    return {str(index): path for index, path in enumerate(paths)}


def load_codex_mcp_map(path: str | Path) -> Mapping[str, tuple[str, str]]:
    from .mcp_contracts import load_contract_json
    from .schema import validate_named_schema

    payload = load_contract_json(path)
    if validate_named_schema("codex_mcp_map", dict(payload)):
        raise ValueError("invalid Codex MCP mapping schema")
    result: dict[str, tuple[str, str]] = {}
    for item in payload["tools"]:
        name = item["hook_name"]
        if name in result:
            raise ValueError("duplicate Codex MCP hook identity")
        result[name] = (item["server"], item["tool"])
    return MappingProxyType(result)


def build_codex_integration(
    *,
    semantics_path: str | Path | None = None,
    policy_path: str | Path | None = None,
    mcp_map_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    fail_on: str = "warn",
) -> CodexIntegration:
    mapping = load_codex_mcp_map(mcp_map_path) if mcp_map_path else {}
    # apply_patch can write or delete any listed target. These are conservative
    # tool capabilities, not claims about effects observed after execution.
    rules = [
        ToolSemanticRule(
            id="codex-patch",
            kind="tool",
            runtime=CODEX_RUNTIME,
            tool="apply_patch",
            effects=("filesystem.write", "filesystem.delete"),
            resources=tuple(
                ToolResourceBinding(argument=f"paths.{index}", type="path")
                for index in range(MAX_PATCH_PATHS)
            ),
        )
    ]
    if semantics_path:
        extra = load_tool_semantics(semantics_path).registry.rules
        rules.extend(extra)
    semantics = ToolSemanticsRegistry("ordin.codex", _digest(dict(mapping)), tuple(rules))
    gate = AgentGate(
        Ordin(
            policy=ReviewPolicy(fail_on=validate_fail_threshold(fail_on)),
            tool_semantics=semantics,
            action_policy=load_action_policy(policy_path) if policy_path else None,
            audit=JsonlAuditSink(audit_path) if audit_path else None,
        )
    )
    return CodexIntegration(gate=gate, mcp_map=mapping)


def _default_gate() -> AgentGate:
    return build_codex_integration().gate


@dataclass(frozen=True)
class CodexIntegration:
    gate: AgentGate = field(default_factory=_default_gate)
    mcp_map: Mapping[str, tuple[str, str]] = field(default_factory=dict)
    session: IntegrationSession | None = None

    def __post_init__(self) -> None:
        if len(self.mcp_map) > 256:
            raise ValueError("too many Codex MCP mappings")
        for name, identity in self.mcp_map.items():
            if not isinstance(name, str) or not name.startswith("mcp__") or len(name) > 256:
                raise ValueError("Codex MCP mappings require exact mcp__ hook names")
            if not isinstance(identity, tuple) or len(identity) != 2:
                raise ValueError("Codex MCP mapping requires exact server and tool")
            MCPAdapter(server=identity[0]).adapt(identity[1], {})
        object.__setattr__(self, "mcp_map", MappingProxyType(dict(self.mcp_map)))

    def adapt(self, payload: Mapping[str, Any], *, permission: bool = False) -> ActionEnvelope:
        if not isinstance(payload, Mapping):
            raise ValueError("Codex hook input must be an object")
        expected = "PermissionRequest" if permission else "PreToolUse"
        if payload.get("hook_event_name") != expected:
            raise ValueError(f"expected {expected} Codex hook")
        session_id = _required_text(payload.get("session_id"), name="session_id")
        turn_id = _required_text(payload.get("turn_id"), name="turn_id")
        tool = _required_text(payload.get("tool_name"), name="tool_name", maximum=256)
        cwd = _required_text(payload.get("cwd"), name="cwd")
        mode = _required_text(payload.get("permission_mode"), name="permission_mode")
        arguments = payload.get("tool_input")
        if not isinstance(arguments, Mapping):
            raise ValueError("Codex tool_input must be an object")
        call_id = (
            _digest([tool, arguments])
            if permission
            else _required_text(payload.get("tool_use_id"), name="tool_use_id")
        )
        action_id = "codex:" + _digest([session_id, turn_id, call_id, tool])
        context = ExecutionContext(cwd=cwd, agent=CODEX_RUNTIME)
        if self.session is not None:
            self.session.require_identity(SessionIdentity(CODEX_RUNTIME, session_id))
            if self.session.gate is not self.gate:
                raise ValueError("Codex integration must use its session's gate")
        if tool == "apply_patch":
            command = _required_text(
                arguments.get("command"), name="patch command", maximum=MAX_CODEX_INPUT_BYTES
            )
            paths = parse_patch_targets(command)
            normalized = {
                "paths": paths,
                "patch_sha256": hashlib.sha256(command.encode()).hexdigest(),
            }
            action = ToolCallAdapter(runtime=CODEX_RUNTIME).adapt(
                tool, normalized, context=context, action_id=action_id
            )
        elif tool in self.mcp_map:
            server, name = self.mcp_map[tool]
            action = MCPAdapter(server=server).adapt(
                name, arguments, context=context, action_id=action_id
            )
        else:
            action = ToolCallAdapter(runtime=CODEX_RUNTIME, shell_tools=frozenset({"Bash"})).adapt(
                tool, arguments, context=context, action_id=action_id
            )
        metadata = {
            "runtime": CODEX_RUNTIME,
            "hook_event": expected,
            "permission_mode": mode,
            "session_id_sha256": _digest(session_id),
            "turn_id_sha256": _digest(turn_id),
            "tool_use_id_sha256": _digest(call_id),
        }
        if payload.get("agent_id") is not None:
            metadata["agent_id_sha256"] = _digest(
                _required_text(payload["agent_id"], name="agent_id")
            )
        return ActionEnvelope(
            kind=action.kind,
            operation=action.operation,
            parameters={**action.parameters, "integration": metadata},
            context=context,
            action_id=action_id,
        )

    def review_pre_tool(self, payload: Mapping[str, Any]) -> AgentDecision:
        action = self.adapt(payload)
        return self.session.evaluate(action) if self.session else self.gate.evaluate_action(action)

    def pre_tool_output(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            decision = self.review_pre_tool(payload)
            return _pre_output("allow" if decision.may_execute else "deny", _reason(decision))
        except (ValueError, OSError) as exc:
            return _pre_output("deny", f"Ordin rejected Codex hook input: {exc}")

    def permission_output(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        action = self.adapt(payload, permission=True)
        snapshot = self.session.snapshot() if self.session else None
        decision = self.gate.evaluate_action(
            action,
            history=ActionHistory.from_dict(snapshot["history"]) if snapshot else None,
            observations=ObservationHistory.from_dict(snapshot["observations"])
            if snapshot
            else None,
        )
        # Preserve the host's normal approval prompt. Ordin does not approve
        # broader sandbox permissions on the strength of command semantics.
        return _permission_deny(_reason(decision)) if decision.denied else {}

    def observation_from_hook(
        self, payload: Mapping[str, Any], *, observed_effects: tuple[str, ...] = ()
    ) -> ActionObservation:
        if payload.get("hook_event_name") != "PostToolUse":
            raise ValueError("expected Codex PostToolUse hook")
        if "tool_response" not in payload:
            raise ValueError("Codex PostToolUse requires tool_response")
        # Reconstruct only the normalized action identity; source/output text is
        # never copied into the observation or into the retained patch action.
        action = self.adapt({**payload, "hook_event_name": "PreToolUse"})
        response = payload.get("tool_response")
        exit_code = None
        status = "reported"
        if isinstance(response, Mapping):
            candidate = response.get("exit_code")
            if candidate is not None:
                if isinstance(candidate, bool) or not isinstance(candidate, int):
                    raise ValueError("Codex result exit_code must be an integer")
                exit_code = candidate
            elif "content" in response and isinstance(response.get("content"), list):
                exit_code = 1 if response.get("isError") is True else 0
        if exit_code is not None:
            status = "success" if exit_code == 0 else "failure"
        assert action.action_id is not None
        observation = ActionObservation(
            action_id=action.action_id,
            exit_code=exit_code,
            effects=observed_effects,
            metadata={"runtime": CODEX_RUNTIME, "tool": payload["tool_name"], "status": status},
        )
        if self.session:
            self.session.observe(observation)
        return observation


def _reason(decision: AgentDecision) -> str:
    review = decision.review
    reason = f"Ordin {review.decision}/{review.risk}"
    if decision.requires_approval:
        reason += ": review required; this Codex pre-tool hook cannot open an approval prompt"
    elif getattr(review, "reasons", None):
        reason += ": " + review.reasons[0]
    return reason[:2048]


def _pre_output(permission: str, reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": permission,
            "permissionDecisionReason": reason[:2048],
        }
    }


def _permission_deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "deny", "message": reason[:2048]},
        }
    }


def _configured() -> CodexIntegration:
    return build_codex_integration(
        semantics_path=os.environ.get("ORDIN_CODEX_SEMANTICS") or None,
        policy_path=os.environ.get("ORDIN_CODEX_POLICY") or None,
        mcp_map_path=os.environ.get("ORDIN_CODEX_MCP_MAP") or None,
        audit_path=os.environ.get("ORDIN_CODEX_AUDIT") or None,
        fail_on=os.environ.get("ORDIN_CODEX_FAIL_ON", "warn"),
    )


def _run(
    mode: str, payload: Mapping[str, Any], integration: CodexIntegration
) -> dict[str, Any] | None:
    if mode == "pre":
        decision = integration.review_pre_tool(payload)
        return _pre_output("allow" if decision.may_execute else "deny", _reason(decision))
    if mode == "permission":
        return integration.permission_output(payload)
    if mode == "post":
        observation = integration.observation_from_hook(payload)
        if path := os.environ.get("ORDIN_CODEX_OBSERVATIONS"):
            _append_private_jsonl(path, observation.as_dict())
    elif mode == "session-end" and integration.session:
        integration.session.end()
    return None


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args in (["--help"], ["-h"]):
        print(
            "usage: ordin-codex-hook {pre|permission|post|session-start|session-reset|session-end|doctor}\n       ordin-codex-hook install PATH"
        )
        return 0
    if len(args) == 2 and args[0] == "install":
        try:
            install_hooks(Path(args[1]))
            print("Installed Ordin hooks. Review and trust them in Codex /hooks before use.")
            return 0
        except (OSError, ValueError) as exc:
            print(f"Ordin hook installation failed: {exc}", file=sys.stderr)
            return 2
    modes = {"pre", "permission", "post", "session-start", "session-reset", "session-end"}
    if len(args) != 1 or args[0] not in modes | {"doctor"}:
        print(
            "usage: ordin-codex-hook {pre|permission|post|session-start|session-reset|session-end|doctor}",
            file=sys.stderr,
        )
        return 2
    mode = args[0]
    try:
        integration = _configured()
        if mode == "doctor":
            print(
                json.dumps(
                    {
                        "ok": True,
                        "runtime": CODEX_RUNTIME,
                        "mcp_mappings": len(integration.mcp_map),
                        "state_enabled": bool(os.environ.get("ORDIN_CODEX_STATE")),
                        "hook_trust": "verify in Codex /hooks",
                        "pre_tool_ask_supported": False,
                        "hosted_tools_covered": False,
                    }
                )
            )
            return 0
        raw = sys.stdin.read(MAX_CODEX_INPUT_BYTES + 1).encode()
        if len(raw) > MAX_CODEX_INPUT_BYTES:
            raise ValueError("Codex hook input exceeds byte limit")
        payload = _parse_jsonrpc_line(raw)
        expected = {
            "pre": "PreToolUse",
            "permission": "PermissionRequest",
            "post": "PostToolUse",
            "session-start": "SessionStart",
            "session-reset": "SessionStart",
            "session-end": "SessionEnd",
        }[mode]
        if payload.get("hook_event_name") != expected:
            raise ValueError(f"expected Codex {expected} hook")
        state_path = os.environ.get("ORDIN_CODEX_STATE")
        if state_path:
            identity = SessionIdentity(
                CODEX_RUNTIME, _required_text(payload.get("session_id"), name="session_id")
            )
            with SqliteSessionStore(state_path).transaction(
                identity,
                integration.gate,
                create=mode in {"session-start", "session-reset"},
                reset=mode == "session-reset",
            ) as session:
                active = CodexIntegration(
                    gate=integration.gate, mcp_map=integration.mcp_map, session=session
                )
                output = _run(mode, payload, active)
        else:
            output = _run(mode, payload, integration)
        if output is not None:
            print(json.dumps(output, sort_keys=True, separators=(",", ":")))
        return 0
    except (OSError, ValueError, RecursionError) as exc:
        reason = f"Ordin Codex integration unavailable: {exc}"[:2048]
        if mode in {"pre", "permission"}:
            print(
                json.dumps(
                    _pre_output("deny", reason) if mode == "pre" else _permission_deny(reason)
                )
            )
            return 0
        print(reason, file=sys.stderr)
        return 2


def install_hooks(target: Path) -> None:
    """Write a new native hook config; never overwrite an existing hook layer."""
    if os.name != "posix":
        raise ValueError(
            "hook installation currently supports POSIX shells; use a Linux environment"
        )
    plugin = Path(__file__).resolve().parent / "plugin_assets" / "ordin"
    payload = json.loads((plugin / "hooks/hooks.json").read_text(encoding="utf-8"))
    phases = {
        "SessionStart": "session-start",
        "PreToolUse": "pre",
        "PermissionRequest": "permission",
        "PostToolUse": "post",
        "SessionEnd": "session-end",
    }
    for event, groups in payload["hooks"].items():
        for group in groups:
            for handler in group["hooks"]:
                handler["command"] = (
                    f"{shlex.quote(sys.executable)} {shlex.quote(str(plugin / 'scripts/hook.py'))} {phases[event]} || exit 2"
                )
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    raise SystemExit(main())
