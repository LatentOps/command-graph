import pytest

from ordin import AgentGate, ExecutionContext, MCPAdapter, ToolCallAdapter


def test_generic_tool_call_stays_conservative():
    adapter = ToolCallAdapter(runtime="coding-agent")
    action = adapter.adapt("read_file", {"path": "README.md"}, action_id="tool-1")

    assert action.kind == "tool"
    assert action.operation == "call"
    assert action.parameters["runtime"] == "coding-agent"
    assert action.parameters["tool"] == "read_file"
    assert action.parameters["arguments"] == {"path": "README.md"}

    decision = AgentGate().evaluate_action(action)
    assert decision.disposition == "escalate"
    assert decision.review.uncertain is True


def test_explicit_shell_tool_mapping_reuses_shell_safety_engine():
    adapter = ToolCallAdapter(
        runtime="coding-agent",
        shell_tools=frozenset({"run_command"}),
    )

    safe = AgentGate().evaluate_tool(
        adapter,
        "run_command",
        {"command": "git status --short"},
    )
    dangerous = AgentGate().evaluate_tool(
        adapter,
        "run_command",
        {"command": "rm -rf /"},
    )

    assert safe.disposition == "execute"
    assert safe.review.action.kind == "shell"
    assert safe.review.adapter == "shell"
    assert safe.review.action.parameters["source_runtime"] == "coding-agent"
    assert safe.review.action.parameters["source_tool"] == "run_command"

    assert dangerous.disposition == "deny"
    assert dangerous.review.blocked is True


def test_mcp_unknown_tool_preserves_server_and_requires_approval():
    adapter = MCPAdapter(server="filesystem")
    decision = AgentGate().evaluate_mcp(
        adapter,
        "read_file",
        {"path": "/tmp/example.txt"},
        intent="inspect a local file",
        action_id="mcp-1",
    )

    assert decision.disposition == "escalate"
    review = decision.review
    assert review.action.kind == "mcp"
    assert review.action.action_id == "mcp-1"
    assert review.action.parameters["server"] == "filesystem"
    assert review.action.parameters["tool"] == "read_file"
    assert review.action.parameters["arguments"] == {"path": "/tmp/example.txt"}
    assert review.uncertain is True


def test_explicit_mcp_shell_tool_mapping_is_exact_name_only():
    adapter = MCPAdapter(
        server="local-shell",
        shell_tools=frozenset({"execute_command"}),
    )

    mapped = adapter.adapt("execute_command", {"command": "git status --short"})
    similar = adapter.adapt("execute_command_v2", {"command": "git status --short"})

    assert mapped.kind == "shell"
    assert mapped.parameters["source_runtime"] == "mcp"
    assert mapped.parameters["source_server"] == "local-shell"
    assert mapped.parameters["source_tool"] == "execute_command"
    assert similar.kind == "mcp"


def test_shell_tool_requires_configured_command_argument():
    adapter = ToolCallAdapter(
        runtime="agent",
        shell_tools=frozenset({"shell"}),
        command_argument="cmd",
    )

    with pytest.raises(ValueError, match="requires non-empty 'cmd' argument"):
        adapter.adapt("shell", {"command": "git status"})


def test_adapter_payload_limits_are_enforced_by_action_envelope():
    adapter = MCPAdapter(server="server")
    nested = {}
    cursor = nested
    for index in range(10):
        child = {}
        cursor[f"level{index}"] = child
        cursor = child

    with pytest.raises(ValueError, match="maximum nesting depth"):
        adapter.adapt("tool", nested)


def test_context_flows_through_adapter_and_agent_gate():
    context = ExecutionContext(
        cwd="/workspace/repo",
        repo_root="/workspace/repo",
        agent="coding-agent",
    )
    adapter = ToolCallAdapter(
        runtime="coding-agent",
        shell_tools=frozenset({"shell"}),
    )

    decision = AgentGate().evaluate_tool(
        adapter,
        "shell",
        {"command": "git status --short"},
        context=context,
    )

    assert decision.review.action.context == context
    assert decision.disposition == "execute"


def test_adapter_names_are_bounded():
    with pytest.raises(ValueError, match="runtime must be at most"):
        ToolCallAdapter(runtime="x" * 129)

    with pytest.raises(ValueError, match="MCP server must be at most"):
        MCPAdapter(server="x" * 257)


@pytest.mark.parametrize("padding", [" ", "\t", "\n", "\u00a0"])
@pytest.mark.parametrize(
    "adapter",
    [
        ToolCallAdapter(runtime="agent", shell_tools=frozenset({"shell"})),
        MCPAdapter(server="server", shell_tools=frozenset({"shell"})),
    ],
)
def test_tool_identity_whitespace_does_not_inherit_shell_mapping(adapter, padding):
    tool = f"{padding}shell{padding}"

    decision = AgentGate().evaluate_action(adapter.adapt(tool, {"command": "git status"}))

    assert decision.disposition == "escalate"
    assert decision.review.action.kind in {"tool", "mcp"}
    assert decision.review.action.parameters["tool"] == tool


def test_runtime_and_server_identity_whitespace_is_preserved():
    tool = ToolCallAdapter(runtime=" agent ").adapt("read_file")
    mcp = MCPAdapter(server=" server ").adapt("read_file")

    assert tool.parameters["runtime"] == " agent "
    assert mcp.parameters["server"] == " server "
