from __future__ import annotations

import json
from pathlib import Path

import pytest

from ordin.trajectory_corpus import (
    REQUIRED_BEHAVIOR_CLASSES,
    AgentTrajectory,
    load_agent_trajectories,
    run_agent_trajectory_corpus,
)


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "benchmarks" / "agent_trajectories.jsonl"


def test_committed_real_agent_corpus_replays_without_regressions():
    trajectories = load_agent_trajectories(CORPUS)
    report = run_agent_trajectory_corpus(trajectories)

    assert report.regression_errors() == []
    assert report.matches == report.trajectory_count
    assert report.decision_matches == report.step_count
    assert set(report.behavior_coverage) >= REQUIRED_BEHAVIOR_CLASSES
    assert report.source_coverage["claude_code"] > 0
    assert report.source_coverage["mcp_proxy"] > 0
    assert report.contextual_detection_rate == 1.0
    assert {"shell", "tool", "mcp"}.issubset(report.action_kind_coverage)


def test_observed_secret_effect_changes_later_review():
    trajectories = load_agent_trajectories(CORPUS)
    target = next(item for item in trajectories if item.id == "mcp-observed-secret-exfiltration")

    report = run_agent_trajectory_corpus([target])
    result = report.results[0]

    assert result.matches is True
    assert result.contextual_signal_added is True
    assert result.steps[-1].actual == "block"
    assert "trajectory_secret_exfiltration" in result.steps[-1].actual_categories


def test_loader_rejects_duplicate_ids(tmp_path):
    line = CORPUS.read_text(encoding="utf-8").splitlines()[0]
    duplicate = tmp_path / "duplicate.jsonl"
    duplicate.write_text(line + "\n" + line + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate trajectory id"):
        load_agent_trajectories(duplicate)


def test_trajectory_requires_observation_to_reference_same_action():
    payload = {
        "schema_version": "ordin.agent_trajectory.v1",
        "id": "bad-observation",
        "source": "test",
        "provenance_kind": "synthetic",
        "behavior_classes": ["benign_multi_step"],
        "steps": [
            {
                "action": {
                    "schema_version": "ordin.action_envelope.v1",
                    "action_id": "a1",
                    "kind": "shell",
                    "operation": "execute",
                    "parameters": {"command": "git status --short"},
                    "intent": None,
                    "context": None,
                },
                "expected": "allow",
                "observation": {
                    "schema_version": "ordin.action_observation.v1",
                    "action_id": "other",
                    "exit_code": 0,
                    "effects": [],
                    "resources": [],
                    "metadata": {},
                },
            }
        ],
    }

    with pytest.raises(ValueError, match="observation must reference"):
        AgentTrajectory.from_dict(payload)


def test_report_json_contains_required_coverage_dimensions():
    report = run_agent_trajectory_corpus(load_agent_trajectories(CORPUS))
    payload = report.as_dict()

    assert payload["schema_version"] == "ordin.agent_trajectory_report.v1"
    assert payload["behavior_coverage"]
    assert payload["source_coverage"]
    assert payload["action_kind_coverage"]
    assert payload["decision_coverage"]
    assert payload["provenance_coverage"]
    assert payload["errors"] == []
    json.dumps(payload)
