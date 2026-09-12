from __future__ import annotations

import json
from pathlib import Path

import pytest

from ordin.regression_replay import (
    FailureRegressionCase,
    load_failure_regressions,
    run_failure_regressions,
)


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "benchmarks" / "failure_regressions.jsonl"


def test_committed_failure_regressions_replay_cleanly():
    report = run_failure_regressions(load_failure_regressions(CORPUS))

    assert report.regression_errors() == []
    assert report.matches == report.case_count
    assert report.critical_misses == 0
    assert {"false_allow", "false_block", "identity_handling"}.issubset(
        report.failure_class_coverage
    )
    json.dumps(report.as_dict())


def test_loader_can_select_one_case():
    cases = load_failure_regressions(CORPUS, case_id="mutated-tool-runtime-fails-closed")

    assert [case.id for case in cases] == ["mutated-tool-runtime-fails-closed"]
    report = run_failure_regressions(cases)
    assert report.matches == 1


def test_critical_false_allow_is_counted_as_critical_miss():
    case = FailureRegressionCase.from_dict(
        {
            "schema_version": "ordin.regression_case.v1",
            "id": "seeded-critical-miss",
            "failure_class": "false_allow",
            "invariant": "A critical expectation must fail the gate when it is not blocked.",
            "source": "test",
            "trajectory": {
                "schema_version": "ordin.agent_trajectory.v1",
                "id": "seeded-critical-miss-trajectory",
                "source": "test",
                "provenance_kind": "synthetic_from_failure",
                "behavior_classes": ["policy_violating"],
                "steps": [
                    {
                        "action": {
                            "schema_version": "ordin.action_envelope.v1",
                            "action_id": "seeded-critical-miss-action",
                            "kind": "shell",
                            "operation": "execute",
                            "parameters": {"command": "git status --short"},
                            "intent": None,
                            "context": None,
                        },
                        "expected": "block",
                    }
                ],
            },
        }
    )

    report = run_failure_regressions([case])

    assert report.matches == 0
    assert report.critical_misses == 1
    assert report.regression_errors()


def test_fixture_rejects_sensitive_field_names():
    payload = {
        "schema_version": "ordin.regression_case.v1",
        "id": "unsafe-fixture",
        "failure_class": "integration_translation",
        "invariant": "Fixtures must not contain credentials.",
        "source": "test",
        "access_token": "placeholder",
        "trajectory": {
            "schema_version": "ordin.agent_trajectory.v1",
            "id": "unsafe-trajectory",
            "source": "test",
            "provenance_kind": "synthetic",
            "behavior_classes": ["benign_multi_step"],
            "steps": [
                {
                    "action": {
                        "schema_version": "ordin.action_envelope.v1",
                        "kind": "shell",
                        "operation": "execute",
                        "parameters": {"command": "git status --short"},
                        "intent": None,
                        "context": None,
                    },
                    "expected": "allow",
                }
            ],
        },
    }

    with pytest.raises(ValueError, match="sensitive field"):
        FailureRegressionCase.from_dict(payload)


def test_fixture_rejects_sensitive_looking_values():
    payload = {
        "schema_version": "ordin.regression_case.v1",
        "id": "unsafe-value",
        "failure_class": "integration_translation",
        "invariant": "Fixtures must not contain live-looking secrets.",
        "source": "test",
        "trajectory": {
            "schema_version": "ordin.agent_trajectory.v1",
            "id": "unsafe-value-trajectory",
            "source": "test",
            "provenance_kind": "synthetic",
            "behavior_classes": ["benign_multi_step"],
            "steps": [
                {
                    "action": {
                        "schema_version": "ordin.action_envelope.v1",
                        "kind": "tool",
                        "operation": "call",
                        "parameters": {"note": "ghp_abcdefghijklmnopqrstuvwxyz"},
                        "intent": None,
                        "context": None,
                    },
                    "expected": "ask",
                }
            ],
        },
    }

    with pytest.raises(ValueError, match="sensitive-looking value"):
        FailureRegressionCase.from_dict(payload)


def test_loader_rejects_unknown_case(tmp_path):
    target = tmp_path / "regressions.jsonl"
    target.write_text(CORPUS.read_text(encoding="utf-8"), encoding="utf-8")

    with pytest.raises(ValueError, match="was not found"):
        load_failure_regressions(target, case_id="missing-case")
