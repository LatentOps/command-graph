from __future__ import annotations

import json
from pathlib import Path

import pytest

from ordin.runtime_evaluation import (
    render_runtime_integration_markdown,
    run_runtime_integration_evaluation,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_runtime_integration_evaluation_exercises_process_boundaries():
    report = run_runtime_integration_evaluation(
        revision="test-revision",
        repo_root=REPO_ROOT,
        repetitions=1,
        timeout_seconds=10.0,
    )
    payload = report.as_dict()

    assert report.errors == []
    assert payload["scope"]["cases"] == 11
    assert payload["scope"]["integrations"] == {
        "claude-code-hook-process": 3,
        "codex-hook-process": 5,
        "mcp-proxy-process": 3,
    }
    assert payload["failure_count"] == 0
    assert payload["http_transport"]["errors"] == []
    assert payload["protocol_distribution"]["allow"] == 3
    assert payload["protocol_distribution"]["ask"] == 1
    assert payload["protocol_distribution"]["deny"] == 4
    assert payload["protocol_distribution"]["upstream_result"] == 1
    assert payload["protocol_distribution"]["approval_required"] == 1
    assert payload["protocol_distribution"]["blocked"] == 1
    assert payload["evidence_linkage"]["observation_cases"] == 4
    assert payload["evidence_linkage"]["observation_rate"] == 1.0
    assert payload["friction_categories"] == []
    assert payload["latency_ms"]["subprocess_end_to_end"]["p50"] > 0


def test_runtime_report_is_sanitized_and_documents_representative_cases():
    report = run_runtime_integration_evaluation(
        revision="test-revision",
        repo_root=REPO_ROOT,
        repetitions=1,
    )
    rendered = json.dumps(report.as_dict(), sort_keys=True)
    markdown = render_runtime_integration_markdown(report)

    assert "/workspace/runtime-evaluation/README.md" not in rendered
    assert "synthetic fixture" not in rendered
    assert "rm -rf /" not in rendered
    assert "false-block examples: none observed" in markdown
    assert "critical catch" in markdown
    assert "unknown MCP tool" in markdown
    assert "universal real-world agent safety" in markdown


@pytest.mark.parametrize("repetitions", [0, -1])
def test_runtime_evaluation_rejects_invalid_repetition_count(repetitions):
    with pytest.raises(ValueError, match="repetitions must be at least 1"):
        run_runtime_integration_evaluation(
            revision="test-revision",
            repo_root=REPO_ROOT,
            repetitions=repetitions,
        )


def test_runtime_evaluation_requires_versioned_process_fixtures(tmp_path):
    with pytest.raises(ValueError, match="runtime evaluation fixtures are missing"):
        run_runtime_integration_evaluation(
            revision="test-revision",
            repo_root=tmp_path,
            repetitions=1,
        )
