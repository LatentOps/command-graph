from __future__ import annotations

import json
from pathlib import Path

import pytest

from ordin.regression_replay import (
    RegressionReplay,
    load_regression_replays,
    run_regression_replays,
)


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "benchmarks" / "regressions.jsonl"


def test_committed_promoted_regressions_replay_cleanly():
    replays = load_regression_replays(CORPUS)
    report = run_regression_replays(replays)

    assert report.regression_errors() == []
    assert report.passed == report.case_count
    assert report.critical_false_allows == 0
    assert {
        "critical_miss",
        "identity",
        "policy",
        "post_action_observation",
    }.issubset(report.as_dict()["failure_class_coverage"])


def test_one_promoted_regression_can_replay_independently():
    report = run_regression_replays(
        load_regression_replays(CORPUS),
        replay_id="trusted-runtime-identity-mutation",
    )

    assert report.case_count == 1
    assert report.passed == 1
    assert report.results[0].actual == "ask"


def test_replay_gate_detects_critical_false_allow_before_promotion(tmp_path):
    fixture = {
        "schema_version": "ordin.regression_replay.v1",
        "id": "seeded-pre-fix-control",
        "failure_class": "false_allow",
        "severity": "critical",
        "invariant": "A known destructive action must not be allowed.",
        "why_it_matters": "This negative control proves the replay gate rejects a false allow.",
        "kind": "safety",
        "safety": {
            "schema_version": "ordin.safety_fixture.v1",
            "id": "seeded-pre-fix-control-case",
            "type": "shell",
            "command": "git status --short",
            "expected": "block",
            "critical": True,
        },
    }
    path = tmp_path / "seeded.jsonl"
    path.write_text(json.dumps(fixture) + "\n", encoding="utf-8")

    report = run_regression_replays(load_regression_replays(path))

    assert report.passed == 0
    assert report.critical_false_allows == 1
    assert report.regression_errors()
    assert "expected block, got allow" in report.regression_errors()[0]


def test_loader_rejects_credential_like_values():
    payload = {
        "schema_version": "ordin.regression_replay.v1",
        "id": "unsafe-fixture",
        "failure_class": "integration_translation",
        "severity": "high",
        "invariant": "Fixtures are sanitized.",
        "why_it_matters": "Committed replay data must not leak credentials.",
        "kind": "safety",
        "safety": {
            "schema_version": "ordin.safety_fixture.v1",
            "id": "unsafe-case",
            "type": "tool",
            "runtime": "fixture",
            "tool": "call",
            "arguments": {"api_key": "sk-live-secret"},
            "expected": "ask",
        },
    }

    with pytest.raises(ValueError, match="unsafe regression fixture"):
        RegressionReplay.from_dict(payload)


def test_redacted_sensitive_fields_are_allowed():
    payload = {
        "schema_version": "ordin.regression_replay.v1",
        "id": "redacted-fixture",
        "failure_class": "integration_translation",
        "severity": "medium",
        "invariant": "Explicitly redacted values remain usable as fixtures.",
        "why_it_matters": "Sanitization should not make safe reconstruction impossible.",
        "kind": "safety",
        "safety": {
            "schema_version": "ordin.safety_fixture.v1",
            "id": "redacted-case",
            "type": "tool",
            "runtime": "fixture",
            "tool": "call",
            "arguments": {"api_key": "<redacted>"},
            "expected": "ask",
        },
    }

    replay = RegressionReplay.from_dict(payload)
    assert replay.id == "redacted-fixture"


def test_unknown_replay_id_fails_explicitly():
    with pytest.raises(ValueError, match="unknown regression replay id"):
        run_regression_replays(load_regression_replays(CORPUS), replay_id="missing")
