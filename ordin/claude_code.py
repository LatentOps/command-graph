from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from .action import ActionEnvelope
from .adapters import ToolCallAdapter
from .agent import AgentDecision, AgentGate
from .api import Ordin
from .audit import JsonlAuditSink
from .context import ExecutionContext
from .execution import ActionObservation
from .tool_calls import ToolResourceBinding, ToolSemanticRule, ToolSemanticsRegistry


CLAUDE_CODE_RUNTIME = "claude-code"
CLAUDE_CODE_AUDIT_ENV = "ORDIN_CLAUDE_AUDIT"
CLAUDE_CODE_OBSERVATIONS_ENV = "ORDIN_CLAUDE_OBSERVATIONS"
MAX_HOOK_INPUT_BYTES = 1_048_576
MAX_LOCAL_EVENT_BYTES = 1_048_576
MAX_HOOK_TEXT_LENGTH = 4096
_EXIT_CODE_PATTERN = re.compile(r"^Exit code (-?\d+)(?:\s|$)")


def _required_text(value: Any, *, name: str, maximum: int = MAX_HOOK_TEXT_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    # Preserve exact tool and event identities, including correlation inputs.
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return value


def _optional_text(value: Any, *, name: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, name=name)


def _identity_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _action_id(*, session_id: str, tool_use_id: str, tool_name: str) -> str:
    material = f"{session_id}\0{tool_use_id}\0{tool_name}".encode("utf-8")
    return "claude-code:" + hashlib.sha256(material).hexdigest()


@lru_cache(maxsize=1)
def claude_code_tool_semantics() -> ToolSemanticsRegistry:
    """Trusted semantics for stable Claude Code built-in tool identities.

    Bash is intentionally excluded because it is normalized through Ordin's
    shell parser instead of static tool semantics. Unlisted tools remain
    generic calls and fail closed to ``ask``.
    """

    runtime = CLAUDE_CODE_RUNTIME
    return ToolSemanticsRegistry(
        registry_id="ordin.claude_code.builtins",
        version="1",
        rules=(
            ToolSemanticRule(
                id="claude-read",
                kind="tool",
                runtime=runtime,
                tool="Read",
                effects=("filesystem.read",),
                resources=(ToolResourceBinding(argument="file_path", type="path"),),
            ),
            ToolSemanticRule(
                id="claude-glob",
                kind="tool",
                runtime=runtime,
                tool="Glob",
                effects=("filesystem.metadata_read",),
                resources=(ToolResourceBinding(argument="path", type="path"),),
            ),
            ToolSemanticRule(
                id="claude-grep",
                kind="tool",
                runtime=runtime,
                tool="Grep",
                effects=("filesystem.read",),
                resources=(ToolResourceBinding(argument="path", type="path"),),
            ),
            ToolSemanticRule(
                id="claude-write",
                kind="tool",
                runtime=runtime,
                tool="Write",
                effects=("filesystem.write",),
                resources=(ToolResourceBinding(argument="file_path", type="path"),),
            ),
            ToolSemanticRule(
                id="claude-edit",
                kind="tool",
                runtime=runtime,
                tool="Edit",
                effects=("filesystem.write",),
                resources=(ToolResourceBinding(argument="file_path", type="path"),),
            ),
            ToolSemanticRule(
                id="claude-notebook-edit",
                kind="tool",
                runtime=runtime,
                tool="NotebookEdit",
                effects=("filesystem.write",),
                resources=(ToolResourceBinding(argument="notebook_path", type="path"),),
            ),
            ToolSemanticRule(
                id="claude-web-fetch",
                kind="tool",
                runtime=runtime,
                tool="WebFetch",
                effects=("network.download",),
                resources=(ToolResourceBinding(argument="url", type="url"),),
            ),
            ToolSemanticRule(
                id="claude-web-search",
                kind="tool",
                runtime=runtime,
                tool="WebSearch",
                effects=("network.connect",),
            ),
        ),
    )


def _default_adapter() -> ToolCallAdapter:
    return ToolCallAdapter(
        runtime=CLAUDE_CODE_RUNTIME,
        shell_tools=frozenset({"Bash"}),
    )


def _default_gate() -> AgentGate:
    return AgentGate(Ordin(tool_semantics=claude_code_tool_semantics()))


def build_claude_code_integration(
    *,
    audit_path: str | Path | None = None,
) -> "ClaudeCodeIntegration":
    audit = JsonlAuditSink(audit_path) if audit_path is not None else None
    ordin = Ordin(tool_semantics=claude_code_tool_semantics(), audit=audit)
    return ClaudeCodeIntegration(gate=AgentGate(ordin))


@dataclass(frozen=True)
class ClaudeCodeIntegration:
    """Translate Claude Code hook events into Ordin review and evidence types.

    This class never executes tools. Claude Code retains ownership of tool
    execution, permissions, credentials, retries, and user approval UI.
    """

    gate: AgentGate = field(default_factory=_default_gate)
    adapter: ToolCallAdapter = field(default_factory=_default_adapter)

    def review_pre_tool(self, payload: Mapping[str, Any]) -> AgentDecision:
        """Review one Claude Code ``PreToolUse`` event."""

        event = _required_text(payload.get("hook_event_name"), name="hook_event_name")
        if event != "PreToolUse":
            raise ValueError("expected a PreToolUse hook event")

        session_id = _required_text(payload.get("session_id"), name="session_id")
        tool_use_id = _required_text(payload.get("tool_use_id"), name="tool_use_id")
        tool_name = _required_text(payload.get("tool_name"), name="tool_name")
        cwd = _required_text(payload.get("cwd"), name="cwd")
        permission_mode = _required_text(
            payload.get("permission_mode"),
            name="permission_mode",
        )
        tool_input = payload.get("tool_input")
        if not isinstance(tool_input, Mapping):
            raise ValueError("tool_input must be a JSON object")

        agent_type = _optional_text(payload.get("agent_type"), name="agent_type")
        agent_id = _optional_text(payload.get("agent_id"), name="agent_id")
        agent = CLAUDE_CODE_RUNTIME if agent_type is None else f"{CLAUDE_CODE_RUNTIME}:{agent_type}"
        context = ExecutionContext(cwd=cwd, agent=agent)
        action_id = _action_id(
            session_id=session_id,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
        )
        action = self.adapter.adapt(
            tool_name,
            tool_input,
            context=context,
            action_id=action_id,
        )

        integration_metadata: dict[str, Any] = {
            "runtime": CLAUDE_CODE_RUNTIME,
            "hook_event": event,
            "permission_mode": permission_mode,
            "session_id_sha256": _identity_digest(session_id),
            "tool_use_id_sha256": _identity_digest(tool_use_id),
        }
        if agent_type is not None:
            integration_metadata["agent_type"] = agent_type
        if agent_id is not None:
            integration_metadata["agent_id_sha256"] = _identity_digest(agent_id)

        parameters = dict(action.parameters)
        parameters["integration"] = integration_metadata
        bound_action = ActionEnvelope(
            kind=action.kind,
            operation=action.operation,
            parameters=parameters,
            intent=action.intent,
            context=action.context,
            action_id=action.action_id,
        )
        return self.gate.evaluate_action(bound_action)

    def pre_tool_output(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Return Claude Code's structured ``PreToolUse`` permission output.

        Malformed hook input fails closed to ``deny`` rather than falling back
        to Claude Code execution without Ordin review.
        """

        try:
            decision = self.review_pre_tool(payload)
        except ValueError as exc:
            return _pre_tool_permission(
                "deny",
                f"Ordin rejected malformed Claude Code hook input: {exc}",
            )

        permission = {
            "execute": "allow",
            "escalate": "ask",
            "deny": "deny",
        }[decision.disposition]
        return _pre_tool_permission(permission, _decision_reason(decision))

    def observation_from_hook(self, payload: Mapping[str, Any]) -> ActionObservation:
        """Convert successful or failed post-tool hooks into redacted evidence."""

        event = _required_text(payload.get("hook_event_name"), name="hook_event_name")
        if event not in {"PostToolUse", "PostToolUseFailure"}:
            raise ValueError("expected PostToolUse or PostToolUseFailure hook event")

        session_id = _required_text(payload.get("session_id"), name="session_id")
        tool_use_id = _required_text(payload.get("tool_use_id"), name="tool_use_id")
        tool_name = _required_text(payload.get("tool_name"), name="tool_name")
        permission_mode = _required_text(
            payload.get("permission_mode"),
            name="permission_mode",
        )
        action_id = _action_id(
            session_id=session_id,
            tool_use_id=tool_use_id,
            tool_name=tool_name,
        )

        metadata: dict[str, Any] = {
            "runtime": CLAUDE_CODE_RUNTIME,
            "hook_event": event,
            "tool": tool_name,
            "permission_mode": permission_mode,
            "status": "success" if event == "PostToolUse" else "failure",
        }
        duration_ms = payload.get("duration_ms")
        if duration_ms is not None:
            if isinstance(duration_ms, bool) or not isinstance(duration_ms, int) or duration_ms < 0:
                raise ValueError("duration_ms must be a non-negative integer or null")
            metadata["duration_ms"] = duration_ms
        is_interrupt = payload.get("is_interrupt")
        if is_interrupt is not None:
            if not isinstance(is_interrupt, bool):
                raise ValueError("is_interrupt must be boolean or null")
            metadata["is_interrupt"] = is_interrupt

        exit_code: int | None = 0
        if event == "PostToolUseFailure":
            exit_code = _failure_exit_code(payload.get("error"))

        # Do not copy tool_response or error text into durable evidence. Both can
        # contain source code, secrets, command output, or other sensitive data.
        return ActionObservation(
            action_id=action_id,
            exit_code=exit_code,
            metadata=metadata,
        )


def _decision_reason(decision: AgentDecision) -> str:
    review = decision.review
    headline = f"Ordin {review.decision}/{review.risk}"
    reasons = getattr(review, "reasons", None)
    if isinstance(reasons, list) and reasons:
        headline += f": {reasons[0]}"
    safer = getattr(review, "safer_next_step", None)
    if decision.disposition != "execute" and isinstance(safer, str) and safer:
        headline += f". {safer}"
    return headline[:MAX_HOOK_TEXT_LENGTH]


def _pre_tool_permission(permission: str, reason: str) -> dict[str, Any]:
    if permission not in {"allow", "ask", "deny"}:
        raise ValueError(f"unsupported Claude Code permission decision: {permission!r}")
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": permission,
            "permissionDecisionReason": reason[:MAX_HOOK_TEXT_LENGTH],
        }
    }


def _failure_exit_code(error: Any) -> int | None:
    if not isinstance(error, str) or not error:
        return None
    first_line = error.splitlines()[0] if error else ""
    match = _EXIT_CODE_PATTERN.match(first_line)
    if match is None:
        return None
    try:
        return int(match.group(1))
    except ValueError:
        return None


def _read_hook_payload() -> dict[str, Any]:
    text = sys.stdin.read(MAX_HOOK_INPUT_BYTES + 1)
    if len(text.encode("utf-8")) > MAX_HOOK_INPUT_BYTES:
        raise ValueError(f"hook input exceeds maximum size {MAX_HOOK_INPUT_BYTES} bytes")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"hook input is invalid JSON at line {exc.lineno} column {exc.colno}"
        ) from exc
    if not isinstance(payload, dict):
        raise ValueError("hook input must be a JSON object")
    return payload


def _append_private_jsonl(path: str | Path, payload: Mapping[str, Any]) -> None:
    line = (
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if len(line) > MAX_LOCAL_EVENT_BYTES:
        raise ValueError(f"local event exceeds maximum size {MAX_LOCAL_EVENT_BYTES} bytes")
    target = Path(path)
    if not target.parent.exists():
        raise ValueError(f"local evidence directory does not exist: {target.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    fd = os.open(target, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("local evidence append made no progress")
            view = view[written:]
    finally:
        os.close(fd)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0] not in {"pre", "post", "post-failure"}:
        print(
            "usage: ordin-claude-hook {pre|post|post-failure}",
            file=sys.stderr,
        )
        return 2
    mode = args[0]

    try:
        payload = _read_hook_payload()
    except ValueError as exc:
        if mode == "pre":
            print(json.dumps(_pre_tool_permission("deny", f"Ordin hook input error: {exc}")))
            return 0
        print(f"ordin-claude-hook: {exc}", file=sys.stderr)
        return 1

    if mode == "pre":
        audit_path = os.environ.get(CLAUDE_CODE_AUDIT_ENV) or None
        try:
            integration = build_claude_code_integration(audit_path=audit_path)
            output = integration.pre_tool_output(payload)
        except (OSError, ValueError) as exc:
            output = _pre_tool_permission("deny", f"Ordin integration error: {exc}")
        print(json.dumps(output, sort_keys=True, separators=(",", ":")))
        return 0

    expected_event = "PostToolUse" if mode == "post" else "PostToolUseFailure"
    if payload.get("hook_event_name") != expected_event:
        print(
            f"ordin-claude-hook: expected {expected_event} hook input",
            file=sys.stderr,
        )
        return 1

    try:
        observation = ClaudeCodeIntegration().observation_from_hook(payload)
        observation_path = os.environ.get(CLAUDE_CODE_OBSERVATIONS_ENV)
        if observation_path:
            _append_private_jsonl(observation_path, observation.as_dict())
    except (OSError, ValueError) as exc:
        print(f"ordin-claude-hook: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
