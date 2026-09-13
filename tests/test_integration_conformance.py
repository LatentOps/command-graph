from __future__ import annotations

import json

from ordin.integration_conformance import (
    ConformanceCheck,
    IntegrationConformanceReport,
    run_integration_conformance,
)


def test_first_party_integrations_pass_shared_conformance():
    report = run_integration_conformance()

    assert report.failed == 0
    assert report.passed == len(report.checks)
    assert report.integrations == ("claude-code", "codex", "cursor", "mcp-http", "mcp-proxy")
    json.dumps(report.as_dict())


def test_conformance_includes_fail_closed_mutation_controls():
    report = run_integration_conformance()
    invariants = {(check.integration, check.invariant): check.passed for check in report.checks}

    assert invariants[("claude-code", "identity_mutation_fails_closed")]
    assert invariants[("mcp-proxy", "identity_mutation_fails_closed")]
    assert invariants[("claude-code", "malformed_input_fails_closed")]
    assert invariants[("mcp-proxy", "malformed_input_fails_closed")]
    assert invariants[("claude-code", "block_never_executes")]
    assert invariants[("mcp-proxy", "block_never_forwards")]


def test_conformance_covers_context_provenance_and_observations():
    report = run_integration_conformance()
    invariants = {(check.integration, check.invariant) for check in report.checks}

    assert ("claude-code", "context_and_resource_binding") in invariants
    assert ("claude-code", "known_read_decision_and_capabilities") in invariants
    assert ("claude-code", "observation_linkage_and_redaction") in invariants
    assert ("mcp-proxy", "context_capabilities_and_provenance") in invariants
    assert ("mcp-proxy", "observation_linkage_and_redaction") in invariants


def test_failed_check_is_machine_and_human_readable():
    report = IntegrationConformanceReport(
        checks=(
            ConformanceCheck(
                integration="fixture-adapter",
                invariant="identity_binding",
                passed=False,
                detail="runtime identity changed",
            ),
        )
    )

    assert report.failed == 1
    assert report.passed == 0
    assert report.errors() == ["fixture-adapter/identity_binding: runtime identity changed"]
    payload = report.as_dict()
    assert payload["schema_version"] == "ordin.integration_conformance_report.v1"
    assert payload["failed"] == 1
    assert payload["results"][0]["passed"] is False
