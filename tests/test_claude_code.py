import io
import json
import sys

from ordin.action import ActionEnvelope
from ordin.claude_code import (
    CLAUDE_CODE_AUDIT_ENV,
    CLAUDE_CODE_OBSERVATIONS_ENV,
    ClaudeCodeIntegration,
    main,
)


def _pre_payload(
    tool_name="Read",
    tool_input=None,
    *,
    tool_use_id="toolu_1",
):
    if tool_input is None:
        tool_input = {"file_path": "/workspace/repo/README.md"}
    return {
        "session_id": "session-1",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/workspace/repo",
        "permission_mode": "default",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
    }


def _post_payload(
    event="PostToolUse",
    *,
    tool_name="Read",
    tool_use_id="toolu_1",
):
    payload = {
        "session_id": "session-1",
        "transcript_path": "/tmp/transcript.jsonl",
        "cwd": "/workspace/repo",
        "permission_mode": "default",
        "hook_event_name": event,
        "tool_name": tool_name,
        "tool_input": {"file_path": "/workspace/repo/README.md"},
        "tool_use_id": tool_use_id,
    }
    if event == "PostToolUse":
        payload["tool_response"] = {"content": "sensitive source text"}
    else:
        payload["error"] = "Exit code 17\nsensitive stderr"
        payload["is_interrupt"] = False
    return payload


def test_claude_code_allows_known_read_only_tool_with_exact_identity():
    integration = ClaudeCodeIntegration()

    decision = integration.review_pre_tool(_pre_payload())
    output = integration.pre_tool_output(_pre_payload())

    assert decision.disposition == "execute"
    assert decision.review.allowed is True
    assert decision.review.effects == ["filesystem.read"]
    assert decision.review.resources[0].value == "/workspace/repo/README.md"
    assert decision.review.action.parameters["runtime"] == "claude-code"
    assert decision.review.action.parameters["tool"] == "Read"
    assert output["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_claude_code_escalates_mutation_and_unknown_tools():
    integration = ClaudeCodeIntegration()

    write = integration.pre_tool_output(
        _pre_payload("Write", {"file_path": "/workspace/repo/out.txt", "content": "x"})
    )
    unknown = integration.pre_tool_output(_pre_payload("FutureTool", {"value": "x"}))

    assert write["hookSpecificOutput"]["permissionDecision"] == "ask"
    assert unknown["hookSpecificOutput"]["permissionDecision"] == "ask"


def test_claude_code_denies_blocked_shell_action():
    integration = ClaudeCodeIntegration()

    output = integration.pre_tool_output(_pre_payload("Bash", {"command": "rm -rf /"}))

    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_claude_code_malformed_input_fails_closed():
    payload = _pre_payload()
    payload.pop("tool_use_id")

    output = ClaudeCodeIntegration().pre_tool_output(payload)

    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "malformed" in output["hookSpecificOutput"]["permissionDecisionReason"].lower()


def test_claude_code_runtime_identity_mutation_loses_trusted_semantics():
    integration = ClaudeCodeIntegration()
    decision = integration.review_pre_tool(_pre_payload())
    action = decision.review.action
    parameters = dict(action.parameters)
    parameters["runtime"] = "claude-code-mutated"
    mutated = ActionEnvelope(
        kind=action.kind,
        operation=action.operation,
        parameters=parameters,
        context=action.context,
        action_id=action.action_id,
    )

    result = integration.gate.evaluate_action(mutated)

    assert result.disposition == "escalate"
    assert result.review.uncertain is True
    assert result.review.adapter is None


def test_claude_code_post_observation_links_to_review_and_redacts_output():
    integration = ClaudeCodeIntegration()
    decision = integration.review_pre_tool(_pre_payload())
    observation = integration.observation_from_hook(_post_payload())

    assert observation.action_id == decision.review.action.action_id
    assert observation.exit_code == 0
    assert "tool_response" not in observation.metadata
    assert decision.review.provenance is not None
    assert any(
        record.action_id == observation.action_id for record in decision.review.provenance.records
    )


def test_claude_code_failure_observation_parses_exit_code_without_error_text():
    observation = ClaudeCodeIntegration().observation_from_hook(_post_payload("PostToolUseFailure"))

    assert observation.exit_code == 17
    assert observation.metadata["status"] == "failure"
    assert "error" not in observation.metadata


def test_claude_code_cli_persists_only_explicit_local_evidence(
    tmp_path,
    monkeypatch,
    capsys,
):
    audit_path = tmp_path / "audit.jsonl"
    observation_path = tmp_path / "observations.jsonl"
    monkeypatch.setenv(CLAUDE_CODE_AUDIT_ENV, str(audit_path))
    monkeypatch.setenv(CLAUDE_CODE_OBSERVATIONS_ENV, str(observation_path))

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_pre_payload())))
    assert main(["pre"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["hookSpecificOutput"]["permissionDecision"] == "allow"

    audit_event = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit_event["action_kind"] == "tool"
    assert all(record["action_id"] is None for record in audit_event["provenance"]["records"])

    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(_post_payload())))
    assert main(["post"]) == 0
    assert capsys.readouterr().out == ""
    observation = json.loads(observation_path.read_text(encoding="utf-8"))
    assert observation["schema_version"] == "ordin.action_observation.v1"
    assert observation["metadata"]["status"] == "success"
    assert "tool_response" not in observation["metadata"]
