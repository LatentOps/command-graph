from __future__ import annotations

from pathlib import Path

from dataclasses import dataclass
from typing import Any, Callable

from .action import ActionEnvelope, ActionReview
from .agent import AgentDecision, AgentGate
from .api import Ordin
from .claude_code import ClaudeCodeIntegration
from .codex import CodexIntegration
from .http_evaluation import run_http_transport_evaluation
from .mcp_proxy import APPROVAL_REQUIRED_CODE, BLOCKED_CODE, MCPStdioSafetyProxy
from .mcp_contracts import MCPContractLock, semantics_binding_digest, tool_contract_digest
from .tool_calls import ToolResourceBinding, ToolSemanticRule, ToolSemanticsRegistry


CONFORMANCE_REPORT_SCHEMA_VERSION = "ordin.integration_conformance_report.v1"


@dataclass(frozen=True)
class ConformanceCheck:
    integration: str
    invariant: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "integration": self.integration,
            "invariant": self.invariant,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class IntegrationConformanceReport:
    checks: tuple[ConformanceCheck, ...]

    @property
    def passed(self) -> int:
        return sum(check.passed for check in self.checks)

    @property
    def failed(self) -> int:
        return len(self.checks) - self.passed

    @property
    def integrations(self) -> tuple[str, ...]:
        return tuple(sorted({check.integration for check in self.checks}))

    def errors(self) -> list[str]:
        return [
            f"{check.integration}/{check.invariant}: {check.detail}"
            for check in self.checks
            if not check.passed
        ]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CONFORMANCE_REPORT_SCHEMA_VERSION,
            "checks": len(self.checks),
            "passed": self.passed,
            "failed": self.failed,
            "integrations": list(self.integrations),
            "errors": self.errors(),
            "results": [check.as_dict() for check in self.checks],
        }


def _record(
    checks: list[ConformanceCheck],
    *,
    integration: str,
    invariant: str,
    assertion: Callable[[], bool],
    failure_detail: str,
) -> None:
    try:
        passed = bool(assertion())
        detail = "pass" if passed else failure_detail
    except Exception as exc:  # pragma: no cover - defensive adapter boundary
        passed = False
        detail = f"raised {type(exc).__name__}: {exc}"
    checks.append(
        ConformanceCheck(
            integration=integration,
            invariant=invariant,
            passed=passed,
            detail=detail,
        )
    )


def _action_review(decision: AgentDecision) -> ActionReview:
    review = decision.review
    if not isinstance(review, ActionReview):
        raise TypeError("generic integration conformance requires ActionReview")
    return review


def _claude_pre_payload(
    tool_name: str = "Read",
    tool_input: dict[str, Any] | None = None,
    *,
    tool_use_id: str = "conformance-tool-1",
) -> dict[str, Any]:
    return {
        "session_id": "conformance-session",
        "transcript_path": "/tmp/conformance-transcript.jsonl",
        "cwd": "/workspace/conformance",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": (
            {"file_path": "/workspace/conformance/README.md"} if tool_input is None else tool_input
        ),
        "tool_use_id": tool_use_id,
    }


def _claude_checks() -> list[ConformanceCheck]:
    integration_name = "claude-code"
    checks: list[ConformanceCheck] = []
    integration = ClaudeCodeIntegration()
    payload = _claude_pre_payload()
    decision = integration.review_pre_tool(payload)
    review = _action_review(decision)
    action = review.action

    _record(
        checks,
        integration=integration_name,
        invariant="action_identity_and_arguments",
        assertion=lambda: (
            action.kind == "tool"
            and action.operation == "call"
            and action.parameters["runtime"] == "claude-code"
            and action.parameters["tool"] == "Read"
            and action.parameters["arguments"] == {"file_path": "/workspace/conformance/README.md"}
        ),
        failure_detail="normalized action changed runtime, tool, or arguments",
    )
    _record(
        checks,
        integration=integration_name,
        invariant="context_and_resource_binding",
        assertion=lambda: (
            action.context is not None
            and action.context.cwd == "/workspace/conformance"
            and action.context.agent == "claude-code"
            and review.resources[0].value == "/workspace/conformance/README.md"
        ),
        failure_detail="execution context or structured resource was not preserved",
    )
    _record(
        checks,
        integration=integration_name,
        invariant="known_read_decision_and_capabilities",
        assertion=lambda: (
            decision.disposition == "execute"
            and review.capabilities is not None
            and review.capabilities.filesystem == "read"
            and review.provenance is not None
        ),
        failure_detail="known read did not preserve execute/capability/provenance contract",
    )

    write = integration.pre_tool_output(
        _claude_pre_payload(
            "Write",
            {"file_path": "/workspace/conformance/out.txt", "content": "fixture"},
            tool_use_id="conformance-tool-write",
        )
    )
    _record(
        checks,
        integration=integration_name,
        invariant="mutation_escalates",
        assertion=lambda: write["hookSpecificOutput"]["permissionDecision"] == "ask",
        failure_detail="write mutation did not escalate",
    )

    blocked = integration.pre_tool_output(
        _claude_pre_payload(
            "Bash",
            {"command": "rm -rf /"},
            tool_use_id="conformance-tool-block",
        )
    )
    _record(
        checks,
        integration=integration_name,
        invariant="block_never_executes",
        assertion=lambda: blocked["hookSpecificOutput"]["permissionDecision"] == "deny",
        failure_detail="blocked shell action was not denied",
    )

    malformed = _claude_pre_payload(tool_use_id="conformance-tool-malformed")
    malformed.pop("tool_use_id")
    malformed_output = integration.pre_tool_output(malformed)
    _record(
        checks,
        integration=integration_name,
        invariant="malformed_input_fails_closed",
        assertion=lambda: malformed_output["hookSpecificOutput"]["permissionDecision"] == "deny",
        failure_detail="malformed hook input did not fail closed",
    )

    mutated_parameters = dict(action.parameters)
    mutated_parameters["runtime"] = "claude-code-mutated"
    mutated_action = ActionEnvelope(
        kind=action.kind,
        operation=action.operation,
        parameters=mutated_parameters,
        intent=action.intent,
        context=action.context,
        action_id=action.action_id,
    )
    mutated = integration.gate.evaluate_action(mutated_action)
    mutated_review = _action_review(mutated)
    _record(
        checks,
        integration=integration_name,
        invariant="identity_mutation_fails_closed",
        assertion=lambda: (
            mutated.disposition == "escalate"
            and mutated_review.uncertain
            and mutated_review.adapter is None
        ),
        failure_detail="mutated runtime retained trusted semantics",
    )

    observation = integration.observation_from_hook(
        {
            **payload,
            "hook_event_name": "PostToolUse",
            "tool_response": {"content": "sensitive fixture output"},
        }
    )
    _record(
        checks,
        integration=integration_name,
        invariant="observation_linkage_and_redaction",
        assertion=lambda: (
            observation.action_id == action.action_id
            and observation.exit_code == 0
            and "tool_response" not in observation.metadata
            and "sensitive fixture output" not in str(observation.as_dict())
        ),
        failure_detail="post-action observation lost linkage or retained tool output",
    )
    return checks


def _mcp_semantics(server: str = "fixture-server") -> ToolSemanticsRegistry:
    return ToolSemanticsRegistry(
        registry_id="ordin.conformance.mcp",
        version="1",
        rules=(
            ToolSemanticRule(
                id="conformance-read",
                kind="mcp",
                server=server,
                tool="read_file",
                effects=("filesystem.read",),
                resources=(ToolResourceBinding(argument="path", type="path"),),
            ),
        ),
    )


def _mcp_call(
    request_id: int | str,
    *,
    name: str = "read_file",
    arguments: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": {} if arguments is None else arguments,
        },
    }


def _mcp_checks() -> list[ConformanceCheck]:
    integration_name = "mcp-proxy"
    checks: list[ConformanceCheck] = []
    gate = AgentGate(Ordin(tool_semantics=_mcp_semantics()))
    proxy = MCPStdioSafetyProxy(server_id="fixture-server", gate=gate)

    adapted = proxy.adapter.adapt(
        "read_file",
        {"path": "/workspace/conformance/README.md", "encoding": "utf-8"},
        context=proxy.context,
        action_id="mcp-conformance-adapted",
    )
    adapted_decision = gate.evaluate_action(adapted)
    adapted_review = _action_review(adapted_decision)
    _record(
        checks,
        integration=integration_name,
        invariant="action_identity_arguments_and_resources",
        assertion=lambda: (
            adapted.kind == "mcp"
            and adapted.operation == "call"
            and adapted.parameters["server"] == "fixture-server"
            and adapted.parameters["tool"] == "read_file"
            and adapted.parameters["arguments"]
            == {"path": "/workspace/conformance/README.md", "encoding": "utf-8"}
            and adapted_review.resources[0].value == "/workspace/conformance/README.md"
        ),
        failure_detail="MCP normalization changed server, tool, arguments, or resource",
    )
    _record(
        checks,
        integration=integration_name,
        invariant="context_capabilities_and_provenance",
        assertion=lambda: (
            adapted.context is not None
            and adapted.context.agent == "mcp-proxy:fixture-server"
            and adapted_review.capabilities is not None
            and adapted_review.capabilities.filesystem == "read"
            and adapted_review.provenance is not None
        ),
        failure_detail="MCP review lost context, capability, or provenance",
    )

    discovery = proxy.process_client_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
    )
    _record(
        checks,
        integration=integration_name,
        invariant="non_effecting_protocol_forwarding",
        assertion=lambda: discovery.forward and discovery.response is None,
        failure_detail="non-effecting MCP protocol message was not forwarded",
    )

    allowed = proxy.process_client_message(
        _mcp_call(2, arguments={"path": "/workspace/conformance/README.md"})
    )
    _record(
        checks,
        integration=integration_name,
        invariant="known_tool_allows",
        assertion=lambda: (
            allowed.forward and allowed.action_id is not None and proxy.pending_count == 1
        ),
        failure_detail="known MCP tool did not reach caller-owned execution boundary",
    )
    observation = proxy.observe_server_message(
        {
            "jsonrpc": "2.0",
            "id": 2,
            "result": {"content": [{"type": "text", "text": "sensitive fixture output"}]},
        }
    )
    _record(
        checks,
        integration=integration_name,
        invariant="observation_linkage_and_redaction",
        assertion=lambda: (
            observation is not None
            and observation.action_id == allowed.action_id
            and observation.exit_code == 0
            and "sensitive fixture output" not in str(observation.as_dict())
            and proxy.pending_count == 0
        ),
        failure_detail="MCP observation lost linkage, redaction, or pending settlement",
    )

    unknown = proxy.process_client_message(_mcp_call(3, name="future_tool"))
    _record(
        checks,
        integration=integration_name,
        invariant="unknown_tool_escalates",
        assertion=lambda: (
            not unknown.forward
            and unknown.response is not None
            and unknown.response["error"]["code"] == APPROVAL_REQUIRED_CODE
        ),
        failure_detail="unknown MCP tool did not require approval",
    )

    mismatched_gate = AgentGate(Ordin(tool_semantics=_mcp_semantics("trusted-server")))
    mismatched_proxy = MCPStdioSafetyProxy(server_id="mutated-server", gate=mismatched_gate)
    mismatch = mismatched_proxy.process_client_message(_mcp_call(4))
    _record(
        checks,
        integration=integration_name,
        invariant="identity_mutation_fails_closed",
        assertion=lambda: (
            not mismatch.forward
            and mismatch.response is not None
            and mismatch.response["error"]["code"] == APPROVAL_REQUIRED_CODE
        ),
        failure_detail="mutated MCP server identity retained trusted semantics",
    )

    shell_proxy = MCPStdioSafetyProxy(
        server_id="fixture-server",
        shell_tools=frozenset({"execute_command"}),
    )
    blocked = shell_proxy.process_client_message(
        _mcp_call(5, name="execute_command", arguments={"command": "rm -rf /"})
    )
    _record(
        checks,
        integration=integration_name,
        invariant="block_never_forwards",
        assertion=lambda: (
            not blocked.forward
            and blocked.response is not None
            and blocked.response["error"]["code"] == BLOCKED_CODE
            and shell_proxy.pending_count == 0
        ),
        failure_detail="blocked MCP shell tool reached upstream execution boundary",
    )

    malformed = proxy.process_client_message(
        {"jsonrpc": "2.0", "id": 6, "method": "tools/call", "params": {}}
    )
    _record(
        checks,
        integration=integration_name,
        invariant="malformed_input_fails_closed",
        assertion=lambda: (
            not malformed.forward
            and malformed.response is not None
            and malformed.response["error"]["code"] == -32600
        ),
        failure_detail="malformed MCP tool call did not fail closed",
    )
    return checks


def run_integration_conformance(
    *, capture_path: str | Path | None = None
) -> IntegrationConformanceReport:
    captured: list[ConformanceCheck] = []
    if capture_path is not None:
        from .trace_replay import load_capture_conformance

        for result in load_capture_conformance(capture_path):
            captured.append(
                ConformanceCheck(
                    result["integration"],
                    result["id"],
                    result["ok"],
                    "pass" if result["ok"] else "; ".join(result["errors"]),
                )
            )
    return IntegrationConformanceReport(
        checks=tuple(
            [
                *_claude_checks(),
                *_mcp_checks(),
                *_contract_checks(),
                *_codex_checks(),
                *_http_checks(),
                *captured,
            ]
        ),
    )


def _contract_checks() -> list[ConformanceCheck]:
    semantics = _mcp_semantics("fixture-server")
    tool = {
        "name": "read_file",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
    lock = MCPContractLock(
        semantics_binding_digest(semantics),
        {("fixture-server", "read_file"): tool_contract_digest(tool)},
    )
    proxy = MCPStdioSafetyProxy(
        server_id="fixture-server",
        gate=AgentGate(Ordin(tool_semantics=semantics)),
        contract_lock=lock,
    )
    checks: list[ConformanceCheck] = []
    for index, candidate in enumerate(
        (
            tool,
            {
                **tool,
                "inputSchema": {"type": "object", "properties": {"path": {"type": "integer"}}},
            },
        )
    ):
        proxy.process_client_message({"jsonrpc": "2.0", "id": 100 + index, "method": "tools/list"})
        proxy.observe_server_message(
            {"jsonrpc": "2.0", "id": 100 + index, "result": {"tools": [candidate]}}
        )
        result = proxy.process_client_message(
            _mcp_call(200 + index, arguments={"path": "/workspace/README.md"})
        )
        passed = (
            result.forward
            if index == 0
            else not result.forward
            and result.response is not None
            and result.response["error"]["code"] == APPROVAL_REQUIRED_CODE
        )
        checks.append(
            ConformanceCheck(
                "mcp-proxy",
                "contract_matched" if index == 0 else "contract_drift_fails_closed",
                passed,
                "pass" if passed else "contract verification did not govern execution",
            )
        )
        if result.forward:
            proxy.observe_server_message({"jsonrpc": "2.0", "id": 200 + index, "result": {}})
    return checks


def _codex_checks() -> list[ConformanceCheck]:
    integration = CodexIntegration()
    checks: list[ConformanceCheck] = []
    for name, tool, arguments, expected in (
        ("benign_read", "Bash", {"command": "git status --short"}, "allow"),
        ("destructive_block", "Bash", {"command": "rm -rf /"}, "deny"),
        ("unknown_tool_denied", "future_tool", {}, "deny"),
        (
            "patch_review",
            "apply_patch",
            {"command": "*** Begin Patch\n*** Add File: out.txt\n+fixture\n*** End Patch"},
            "deny",
        ),
    ):
        payload = {
            "hook_event_name": "PreToolUse",
            "session_id": "conformance",
            "turn_id": "turn",
            "tool_use_id": name,
            "tool_name": tool,
            "tool_input": arguments,
            "cwd": "/workspace",
            "permission_mode": "default",
        }
        output = integration.pre_tool_output(payload)
        passed = output["hookSpecificOutput"]["permissionDecision"] == expected
        checks.append(
            ConformanceCheck(
                "codex", name, passed, "pass" if passed else "Codex decision mapping failed"
            )
        )
    return checks


def _http_checks() -> list[ConformanceCheck]:
    report = run_http_transport_evaluation()
    return [
        ConformanceCheck(
            "mcp-http",
            check["id"],
            check["passed"],
            "pass" if check["passed"] else "HTTP transport control failed",
        )
        for check in report.checks
    ]
