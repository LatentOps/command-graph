"""Bounded offline reproductions for the versioned Agent Safety Corpus.

These controls exercise protocol/state behavior that abstract trajectories cannot
encode. They execute no proposed tools; HTTP uses a local non-executing fixture.
"""

from __future__ import annotations

import http.client
import io
import json
import threading
from typing import Any
from unittest.mock import Mock, patch

from .action_policy import ActionPolicyCondition, ActionPolicyRule, ActionPolicySet
from .agent import AgentGate
from .api import Ordin
from .cursor import CursorIntegration, _identity
from .http_evaluation import create_http_fixture, run_http_transport_evaluation
from .integration_conformance import _contract_checks
from .mcp_http import MCPHTTPConfig, MCPHTTPServer, MCP_HTTP_PROTOCOL, _MCPHTTPHandler
from .mcp_proxy import APPROVAL_REQUIRED_CODE, BLOCKED_CODE, MCPStdioSafetyProxy
from .session import IntegrationSession
from .tool_calls import ToolResourceBinding, ToolSemanticRule, ToolSemanticsRegistry


def read_semantics() -> ToolSemanticsRegistry:
    return ToolSemanticsRegistry(
        "corpus-v1",
        "1",
        (
            ToolSemanticRule(
                id="read",
                kind="mcp",
                server="fixture",
                tool="read_file",
                effects=("filesystem.read",),
                resources=(ToolResourceBinding("path", "path"),),
            ),
        ),
    )


def call(
    request_id: int = 1, name: str = "read_file", arguments: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": {"path": "/workspace/file"} if arguments is None else arguments,
        },
    }


def proxy() -> MCPStdioSafetyProxy:
    return MCPStdioSafetyProxy(
        server_id="fixture",
        gate=AgentGate(Ordin(tool_semantics=read_semantics())),
        shell_tools=frozenset({"shell"}),
    )


def rejected_response() -> dict[str, bool]:
    adapter = proxy()
    decision = adapter.process_client_message(call())
    rejected = False
    try:
        adapter.observe_server_message({"jsonrpc": "2.0", "id": 1, "result": {"isError": "false"}})
    except ValueError:
        rejected = True
    reserved = adapter.pending_count == 1 and not adapter.process_client_message(call()).forward
    observation = adapter.observe_server_message({"jsonrpc": "2.0", "id": 1, "result": {}})
    return {
        "observation_linkage": rejected
        and reserved
        and observation is not None
        and observation.action_id == decision.action_id
        and adapter.pending_count == 0
    }


def history_pressure() -> dict[str, bool]:
    adapter = proxy()
    first = adapter.process_client_message(call())
    for number in range(2, 33):
        adapter.process_client_message(call(number, "untrusted"))
    before = adapter.session.snapshot()
    refused = not adapter.process_client_message(call(33)).forward
    preserved = adapter.session.snapshot() == before
    observation = adapter.observe_server_message({"jsonrpc": "2.0", "id": 1, "result": {}})
    return {
        "observation_linkage": first.forward
        and refused
        and preserved
        and observation is not None
        and observation.action_id == first.action_id
        and adapter.process_client_message(call(34)).forward
    }


def relative_context() -> dict[str, bool]:
    policy = ActionPolicySet(
        "corpus",
        "1",
        (
            ActionPolicyRule(
                id="unknown", decision="block", when=ActionPolicyCondition(repo_scope="unknown")
            ),
        ),
    )
    adapter = CursorIntegration(gate=AgentGate(Ordin(action_policy=policy)))
    payload = {
        "hook_event_name": "preToolUse",
        "conversation_id": "corpus",
        "generation_id": "turn",
        "cursor_version": "1.7.2",
        "tool_use_id": "context",
        "tool_name": "Shell",
        "tool_input": {"command": "git status"},
        "cwd": ".",
        "workspace_roots": ["."],
    }
    denied = adapter.review_pre_tool(payload).denied
    allowed = adapter.review_pre_tool(
        {**payload, "cwd": "/workspace", "workspace_roots": ["/workspace"]}
    ).may_execute
    return {"context_policy": denied and allowed}


def buffered_deadline() -> dict[str, bool]:
    handler = object.__new__(_MCPHTTPHandler)
    handler._upstream_timed_out = threading.Event()
    handler._upstream_expires_at = 10.0
    handler.wfile = io.BytesIO()
    response = Mock()
    response.readline.return_value = b"retry: 1000\n\n"
    rejected = False
    with patch("ordin.mcp_http.time.monotonic", side_effect=[9.0, 10.0]):
        try:
            handler._stream(response, Mock(), request_id=1)
        except TimeoutError:
            rejected = True
    return {"deadline": rejected and handler.wfile.getvalue() == b""}


def failed_http_session() -> dict[str, bool]:
    upstream, state = create_http_fixture(delay=1.0)
    server = MCPHTTPServer(
        MCPHTTPConfig(
            "fixture", f"http://127.0.0.1:{upstream.server_port}/mcp", port=0, timeout=0.3
        ),
        gate=AgentGate(Ordin(tool_semantics=read_semantics())),
    )
    threads = []
    for item in (upstream, server):
        thread = threading.Thread(
            target=item.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        threads.append(thread)

    def post(payload: dict[str, Any], token: str | None = None) -> tuple[int, str | None]:
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "MCP-Protocol-Version": MCP_HTTP_PROTOCOL,
        }
        if token:
            headers["MCP-Session-Id"] = token
        try:
            connection.request("POST", "/mcp", body=json.dumps(payload), headers=headers)
            response = connection.getresponse()
            response.read()
            return response.status, response.getheader("MCP-Session-Id")
        finally:
            connection.close()

    try:
        status, token = post(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {"protocolVersion": MCP_HTTP_PROTOCOL},
            }
        )
        first, _ = post(call(), token)
        second, _ = post(call(2), token)
        return {
            "session_isolation": status == 200
            and token is not None
            and first == 504
            and second == 409
        }
    finally:
        for item, thread in zip((server, upstream), reversed(threads)):
            item.shutdown()
            thread.join(timeout=2)
            item.server_close()


def contract_identity() -> dict[str, bool]:
    changed = MCPStdioSafetyProxy(
        server_id="other", gate=AgentGate(Ordin(tool_semantics=read_semantics()))
    )
    return {
        "contract_drift": all(check.passed for check in _contract_checks()),
        "identity_isolation": not changed.process_client_message(call()).forward,
    }


def benign_parity() -> dict[str, bool]:
    adapter = proxy()
    read = adapter.process_client_message(call())
    unknown = adapter.process_client_message(call(2, "unknown", {}))
    blocked = adapter.process_client_message(call(3, "shell", {"command": "rm -rf /"}))
    http = run_http_transport_evaluation(repetitions=1)
    return {
        "transport_parity": read.forward
        and unknown.response is not None
        and blocked.response is not None
        and unknown.response["error"]["code"] == APPROVAL_REQUIRED_CODE
        and blocked.response["error"]["code"] == BLOCKED_CODE
        and not http.errors
    }


def child_isolation() -> dict[str, bool]:
    base = CursorIntegration()
    payload = {
        "hook_event_name": "preToolUse",
        "conversation_id": "corpus",
        "generation_id": "turn",
        "cursor_version": "1.7.2",
        "tool_use_id": "read",
        "tool_name": "Read",
        "tool_input": {"path": "/workspace/file"},
        "cwd": "/workspace",
        "subagent_id": "one",
        "parent_conversation_id": "corpus",
    }
    adapter = CursorIntegration(
        gate=base.gate, session=IntegrationSession(_identity(payload), base.gate)
    )
    decision = adapter.review_pre_tool(payload)
    return {
        "identity_isolation": decision.may_execute
        and adapter.pre_tool_output({**payload, "subagent_id": "two"})["permission"] == "deny"
    }


CONTROLS = {
    "rejected_response": rejected_response,
    "history_pressure": history_pressure,
    "relative_context": relative_context,
    "buffered_deadline": buffered_deadline,
    "failed_http_session": failed_http_session,
    "contract_identity": contract_identity,
    "benign_parity": benign_parity,
    "child_isolation": child_isolation,
}
