import copy
import json
from pathlib import Path

import pytest

from ordin.agent_safety_corpus import evaluate, load_corpus, render_markdown
from ordin.corpus_controls import CONTROLS
from ordin.trace_replay import promote_candidate


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "benchmarks/agent-safety-corpus/v1/cases.json"
REVISION = "f54bd90a2679898d8d42700d8c750c51a9ac2b34"


def test_complete_versioned_corpus_and_boundaries_pass():
    report = evaluate(CORPUS, revision=REVISION)
    assert report["ok"] and report["errors"] == []
    assert report["scope"]["cases"] == 9
    assert report["scope"]["provenance"] == {"synthetic_from_failure": 5, "synthetic_control": 4}
    assert report["scope"]["semantic_actions"] == 15
    assert all(value == 0 for value in report["metrics"].values())
    assert report["contextual"] == {"cases": 2, "detected": 2, "rate": 1.0}
    assert report["decisions"] == {"allow": 10, "warn": 3, "ask": 1, "block": 1}
    assert report["boundary_applicable"]["observation_linkage"] == 2
    assert "no customer traces" in render_markdown(report)


def test_every_case_passes_privacy_and_existing_conformance_promotion(tmp_path):
    destination = tmp_path / "conformance.jsonl"
    for case in load_corpus(CORPUS)["cases"]:
        assert promote_candidate(case["candidate"], "conformance", destination)["ok"]
    assert len(destination.read_text().splitlines()) == 9


def test_control_failure_is_a_gate_and_has_a_metric(monkeypatch):
    monkeypatch.setitem(CONTROLS, "rejected_response", lambda: {"observation_linkage": False})
    report = evaluate(CORPUS, revision=REVISION)
    assert not report["ok"]
    assert report["metrics"]["observation_linkage_failures"] == 1
    assert report["metrics"]["control_failures"] == 1


def test_expectation_mismatch_is_not_relabelled_to_match_implementation(tmp_path):
    payload = load_corpus(CORPUS)
    payload["cases"][0]["candidate"]["case"]["trajectory"]["steps"][0]["expected"] = "block"
    path = tmp_path / "cases.json"
    path.write_text(json.dumps(payload))
    report = evaluate(path, revision=REVISION)
    assert not report["ok"]
    assert report["metrics"]["critical_misses"] == 1
    assert report["metrics"]["false_allows"] == 1


def test_corpus_refuses_missing_controls_private_fields_and_stronger_provenance(tmp_path):
    original = load_corpus(CORPUS)
    for mutation in ("control", "private", "provenance"):
        payload = copy.deepcopy(original)
        case = payload["cases"][0]
        if mutation == "control":
            case["control"] = None
        elif mutation == "private":
            case["candidate"]["password"] = "private"
        else:
            case["candidate"]["case"]["trajectory"]["provenance_kind"] = "captured"
        path = tmp_path / "cases.json"
        path.write_text(json.dumps(payload))
        with pytest.raises(ValueError):
            load_corpus(path)
