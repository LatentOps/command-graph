import json
import subprocess
import sys

from ordin.agent import AgentGate
from ordin.api import Ordin
from ordin.mcp_proxy import (
    APPROVAL_REQUIRED_CODE,
    BLOCKED_CODE,
    MCPStdioSafetyProxy,
)
from ordin.tool_calls import ToolResourceBinding, ToolSemanticRule, ToolSemanticsRegistry


def _call(request_id=1, *, name="read_file", arguments=None):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {
            "name": name,
            "arguments": {} if arguments is None else arguments,
        },
    }


def _read_semantics(server="fixture"):
    return ToolSemanticsRegistry(
        registry_id="mcp-fixture",
        version="1",
        rules=(
            ToolSemanticRule(
                id="fixture-read",
                kind="mcp",
                server=server,
                tool="read_file",
                effects=("filesystem.read",),
                resources=(ToolResourceBinding(argument="path", type="path"),),
            ),
        ),
    )


def test_non_tool_protocol_messages_forward_unchanged():
    proxy = MCPStdioSafetyProxy(server_id="fixture")
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

    decision = proxy.process_client_message(message)

    assert decision.forward is True
    assert decision.response is None


def test_unknown_tool_requires_approval_without_reaching_upstream():
    proxy = MCPStdioSafetyProxy(server_id="fixture")

    decision = proxy.process_client_message(_call(name="future_tool"))

    assert decision.forward is False
    assert decision.response is not None
    assert decision.response["error"]["code"] == APPROVAL_REQUIRED_CODE
    assert decision.response["error"]["data"]["ordin"]["decision"] == "ask"
    assert proxy.pending_count == 0


def test_exact_server_tool_semantics_allow_and_bind_resource():
    gate = AgentGate(Ordin(tool_semantics=_read_semantics()))
    proxy = MCPStdioSafetyProxy(server_id="fixture", gate=gate)

    decision = proxy.process_client_message(_call(arguments={"path": "/tmp/a.txt"}))

    assert decision.forward is True
    assert decision.action_id is not None
    assert proxy.pending_count == 1


def test_server_identity_mismatch_loses_trusted_semantics():
    gate = AgentGate(Ordin(tool_semantics=_read_semantics(server="trusted")))
    proxy = MCPStdioSafetyProxy(server_id="mutated", gate=gate)

    decision = proxy.process_client_message(_call())

    assert decision.forward is False
    assert decision.response is not None
    assert decision.response["error"]["code"] == APPROVAL_REQUIRED_CODE
    assert decision.response["error"]["data"]["ordin"]["decision"] == "ask"


def test_tool_identity_whitespace_is_not_forwarded_with_trusted_semantics():
    gate = AgentGate(Ordin(tool_semantics=_read_semantics()))
    proxy = MCPStdioSafetyProxy(server_id="fixture", gate=gate)

    decision = proxy.process_client_message(_call(name=" read_file "))

    assert decision.forward is False
    assert decision.response["error"]["code"] == APPROVAL_REQUIRED_CODE
    assert proxy.pending_count == 0


def test_explicit_shell_block_never_reaches_upstream():
    proxy = MCPStdioSafetyProxy(
        server_id="fixture",
        shell_tools=frozenset({"execute_command"}),
    )

    decision = proxy.process_client_message(
        _call(name="execute_command", arguments={"command": "rm -rf /"})
    )

    assert decision.forward is False
    assert decision.response is not None
    assert decision.response["error"]["code"] == BLOCKED_CODE
    assert decision.response["error"]["data"]["ordin"]["decision"] == "block"


def test_duplicate_inflight_and_malformed_calls_fail_closed():
    gate = AgentGate(Ordin(tool_semantics=_read_semantics()))
    proxy = MCPStdioSafetyProxy(server_id="fixture", gate=gate)

    first = proxy.process_client_message(
        {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "read_file"}}
    )
    assert first.forward is True

    duplicate = proxy.process_client_message(_call(request_id=1))
    assert duplicate.forward is False
    assert duplicate.response is not None
    assert duplicate.response["error"]["code"] == -32600

    missing_name = proxy.process_client_message(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}}
    )
    assert missing_name.forward is False
    assert missing_name.response is not None
    assert missing_name.response["error"]["code"] == -32600


def test_tool_result_creates_linked_redacted_observation(tmp_path):
    observations = tmp_path / "observations.jsonl"
    gate = AgentGate(Ordin(tool_semantics=_read_semantics()))
    proxy = MCPStdioSafetyProxy(
        server_id="fixture",
        gate=gate,
        observations_path=observations,
    )
    decision = proxy.process_client_message(
        _call(request_id="req-1", arguments={"path": "/secret"})
    )
    assert decision.forward is True

    observation = proxy.observe_server_message(
        {
            "jsonrpc": "2.0",
            "id": "req-1",
            "result": {"content": [{"type": "text", "text": "sensitive output"}]},
        }
    )

    assert observation is not None
    assert observation.action_id == decision.action_id
    assert observation.exit_code == 0
    assert observation.metadata["status"] == "success"
    assert "content" not in observation.metadata
    persisted = json.loads(observations.read_text(encoding="utf-8"))
    assert persisted["action_id"] == decision.action_id
    assert "sensitive output" not in observations.read_text(encoding="utf-8")
    assert proxy.pending_count == 0


def test_tool_and_protocol_errors_are_observed_without_error_payload():
    gate = AgentGate(Ordin(tool_semantics=_read_semantics()))
    proxy = MCPStdioSafetyProxy(server_id="fixture", gate=gate)

    first = proxy.process_client_message(_call(request_id=1))
    assert first.forward is True
    tool_error = proxy.observe_server_message(
        {"jsonrpc": "2.0", "id": 1, "result": {"isError": True, "content": ["secret"]}}
    )
    assert tool_error is not None
    assert tool_error.exit_code == 1
    assert tool_error.metadata["status"] == "tool_error"

    second = proxy.process_client_message(_call(request_id=2))
    assert second.forward is True
    protocol_error = proxy.observe_server_message(
        {"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "secret"}}
    )
    assert protocol_error is not None
    assert protocol_error.exit_code is None
    assert protocol_error.metadata["status"] == "protocol_error"
    assert "error" not in protocol_error.metadata


def test_multi_round_results_are_not_misreported_as_completed_success():
    gate = AgentGate(Ordin(tool_semantics=_read_semantics()))
    proxy = MCPStdioSafetyProxy(server_id="fixture", gate=gate)

    task_decision = proxy.process_client_message(_call(request_id=3))
    assert task_decision.forward is True
    task_observation = proxy.observe_server_message(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "result": {"resultType": "task", "taskId": "opaque-task", "status": "working"},
        }
    )
    assert task_observation is not None
    assert task_observation.exit_code is None
    assert task_observation.metadata["status"] == "task_accepted"
    assert task_observation.metadata["result_type"] == "task"

    input_decision = proxy.process_client_message(_call(request_id=4))
    assert input_decision.forward is True
    input_observation = proxy.observe_server_message(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "result": {"resultType": "input_required", "prompt": "sensitive prompt"},
        }
    )
    assert input_observation is not None
    assert input_observation.exit_code is None
    assert input_observation.metadata["status"] == "input_required"
    assert input_observation.metadata["result_type"] == "input_required"
    assert "prompt" not in input_observation.metadata


def test_stdio_proxy_end_to_end_forwards_discovery_and_allowed_call(tmp_path):
    semantics = tmp_path / "semantics.json"
    semantics.write_text(
        json.dumps(
            {
                "schema_version": "ordin.tool_semantics.v1",
                "registry_id": "fixture",
                "version": "1",
                "rules": [
                    {
                        "id": "read",
                        "kind": "mcp",
                        "server": "fixture",
                        "tool": "read_file",
                        "effects": ["filesystem.read"],
                        "resources": [{"argument": "path", "type": "path"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    observations = tmp_path / "observations.jsonl"
    server_code = r"""
import json
import sys
for line in sys.stdin:
    message = json.loads(line)
    method = message.get("method")
    if method == "tools/list":
        result = {"tools": [{"name": "read_file", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "fixture-secret"}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "result": result}), flush=True)
"""
    messages = (
        "\n".join(
            json.dumps(item)
            for item in [
                {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                _call(request_id=2, arguments={"path": "/tmp/example"}),
            ]
        )
        + "\n"
    )

    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ordin.mcp_proxy",
            "--server-id",
            "fixture",
            "--semantics",
            str(semantics),
            "--observations",
            str(observations),
            "--",
            sys.executable,
            "-c",
            server_code,
        ],
        input=messages,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    output = [json.loads(line) for line in completed.stdout.splitlines()]
    assert output[0]["id"] == 1
    assert output[0]["result"]["tools"][0]["name"] == "read_file"
    assert output[1]["id"] == 2
    assert output[1]["result"]["content"][0]["text"] == "fixture-secret"
    saved = json.loads(observations.read_text(encoding="utf-8"))
    assert saved["metadata"]["tool"] == "read_file"
    assert "fixture-secret" not in observations.read_text(encoding="utf-8")
