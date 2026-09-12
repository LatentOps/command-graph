from __future__ import annotations

import json

from ordin import ActionEnvelope, Ordin, action_review_diagnostic, integration_health


def test_action_diagnostic_explains_decision_without_parameter_values():
    secret = "do-not-leak-this-value"
    action = ActionEnvelope.shell(
        f"printf '{secret}' > ./out.txt",
        action_id="diagnostic-shell",
    )
    review = Ordin().review_action(action)

    diagnostic = action_review_diagnostic(review)
    rendered = json.dumps(diagnostic, sort_keys=True)

    assert diagnostic["schema_version"] == "ordin.integration_diagnostic.v1"
    assert diagnostic["action"]["kind"] == "shell"
    assert diagnostic["action"]["operation"] == "execute"
    assert diagnostic["action"]["parameter_keys"] == ["command"]
    assert diagnostic["redaction"] == {
        "raw_parameters_included": False,
        "resource_values_included": False,
        "caller_literals_scrubbed_from_prose": True,
    }
    assert secret not in rendered
    assert "parameters" not in diagnostic["action"]


def test_unknown_tool_identity_gets_safe_remediation_and_redacts_resource_values():
    private_path = "/private/workspace/secret.txt"
    action = ActionEnvelope(
        kind="tool",
        operation="call",
        parameters={
            "runtime": "unknown-runtime",
            "tool": "Read",
            "arguments": {"file_path": private_path},
        },
    )
    review = Ordin().review_action(action)

    diagnostic = action_review_diagnostic(review)
    remediation_codes = {item["code"] for item in diagnostic["remediation"]}
    rendered = json.dumps(diagnostic, sort_keys=True)

    assert diagnostic["decision"]["uncertain"] is True
    assert diagnostic["contributors"]["adapter"] is None
    assert "uncertain_semantics" in remediation_codes
    assert "untrusted_tool_identity" in remediation_codes
    assert private_path not in rendered


def test_blocked_action_remediation_never_suggests_execution():
    review = Ordin().review_action(ActionEnvelope.shell("rm -rf /"))

    diagnostic = action_review_diagnostic(review)
    messages = " ".join(item["message"] for item in diagnostic["remediation"])

    assert diagnostic["decision"]["decision"] == "block"
    assert "Do not execute" in messages
    assert "bypassing" in messages


def test_integration_health_is_machine_readable_and_green():
    payload = integration_health()

    assert payload["schema_version"] == "ordin.integration_health.v1"
    assert payload["ok"] is True
    assert payload["failed"] == 0
    assert payload["passed"] == payload["checks"]
    assert payload["integrations"] == ["claude-code", "codex", "mcp-http", "mcp-proxy"]
    json.dumps(payload)
