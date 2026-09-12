from __future__ import annotations

import json
from pathlib import Path

from ordin.integration_evaluation import render_markdown_report, run_integration_evaluation


ROOT = Path(__file__).resolve().parents[1]


def _run():
    return run_integration_evaluation(
        safety_path=ROOT / "benchmarks" / "safety.jsonl",
        trajectory_path=ROOT / "benchmarks" / "agent_trajectories.jsonl",
        regression_path=ROOT / "benchmarks" / "failure_regressions.jsonl",
        revision="test-revision",
        repetitions=2,
    )


def test_real_agent_evaluation_passes_all_owned_gates():
    report = _run()

    assert report.regression_errors() == []
    assert report.integration_false_allows == 0
    assert report.integration_false_blocks == 0
    assert report.conformance.failed == 0
    assert report.identity_controls_detected == len(report.identity_controls)
    assert report.provenance_linkage_rate == 1.0
    assert report.observation_linkage_rate == 1.0
    assert report.friction_categories == []


def test_real_agent_evaluation_reports_required_scope_and_metrics():
    payload = _run().as_dict()

    assert payload["schema_version"] == "ordin.integration_evaluation.v1"
    assert payload["revision"] == "test-revision"
    assert payload["scope"]["integration_workloads"] >= 7
    assert payload["scope"]["trajectories"] > 0
    assert payload["scope"]["safety_cases"] >= 34
    assert payload["coverage"]["integrations"] == ["claude-code", "mcp-proxy"]
    assert payload["coverage"]["trajectory_action_kinds"]
    assert payload["coverage"]["safety_domains"]
    assert payload["latency_ms"]["core_review"]["p95"] >= 0.0
    assert payload["latency_ms"]["integration_boundary"]["p95"] >= 0.0
    assert payload["decisions"]["safety_critical_misses"] == 0
    assert payload["policy"]["accuracy_passed"] is True
    assert payload["errors"] == []
    json.dumps(payload)


def test_markdown_report_states_scope_metrics_and_limitations():
    markdown = render_markdown_report(_run())

    assert "# Ordin real-agent integration evaluation" in markdown
    assert "Safety-fixture critical misses: 0" in markdown
    assert "Core review p50 / p95 / p99" in markdown
    assert "Integration boundary p50 / p95 / p99" in markdown
    assert "not a claim of universal agent safety" in markdown
    assert "model inference" in markdown
