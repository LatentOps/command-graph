import hashlib
from pathlib import Path

import pytest

from scripts import run_release_candidate as candidate


def test_candidate_refuses_missing_exact_revision_and_dirty_source(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="full commit"):
        candidate.validate_source("main", tmp_path)
    monkeypatch.setattr(candidate.subprocess, "check_output", lambda *a, **k: "b" * 40)
    with pytest.raises(ValueError, match="differs"):
        candidate.validate_source("a" * 40, tmp_path)
    results = iter(["a" * 40, " M ordin/api.py"])
    monkeypatch.setattr(candidate.subprocess, "check_output", lambda *a, **k: next(results))
    with pytest.raises(ValueError, match="clean"):
        candidate.validate_source("a" * 40, tmp_path)


def test_candidate_reports_cannot_pollute_source_or_be_silently_reused(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    monkeypatch.setattr(candidate, "ROOT", root)
    results = iter(["a" * 40, ""])
    monkeypatch.setattr(candidate.subprocess, "check_output", lambda *a, **k: next(results))
    with pytest.raises(ValueError, match="outside"):
        candidate.validate_source("a" * 40, root / "reports")
    monkeypatch.setattr(candidate, "validate_source", lambda *a: None)
    with pytest.raises(FileExistsError):
        candidate.run_candidate(Path("python"), tmp_path, "a" * 40)


def test_candidate_contains_every_required_workload_and_stamps_revision(tmp_path):
    commands = dict(candidate.workload_commands(tmp_path, "a" * 40))
    assert set(commands) == {
        "safety",
        "trajectories",
        "failure_regressions",
        "extended_regressions",
        "conformance",
        "integration",
        "runtime",
        "quickstarts",
    }
    assert commands["failure_regressions"][0] == "run_regression_replay.py"
    assert commands["extended_regressions"][0] == "replay_regression.py"
    assert "a" * 40 in commands["integration"] and "a" * 40 in commands["runtime"]
    assert "--repetitions" in commands["runtime"]


def test_report_index_records_exact_bytes(tmp_path):
    (tmp_path / "report.json").write_bytes(b'{"ok":true}\n')
    assert candidate.report_index(tmp_path) == {
        "report.json": hashlib.sha256(b'{"ok":true}\n').hexdigest()
    }


def test_resource_hashes_include_hidden_plugin_assets_and_ignore_json_formatting(tmp_path):
    resources = tmp_path / "resources"
    plugin = tmp_path / "plugin_assets/ordin/.codex-plugin"
    resources.mkdir()
    plugin.mkdir(parents=True)
    (resources / "data.json").write_text('{"x":1}')
    (plugin / "plugin.json").write_text('{"version":"1"}')
    before = candidate.resource_hashes(tmp_path)
    assert "plugin_assets/ordin/.codex-plugin/plugin.json" in before
    (resources / "data.json").write_text('{\n "x": 1\n}')
    assert candidate.resource_hashes(tmp_path) == before
    (resources / "data.json").write_text('{"x":2}')
    assert candidate.resource_hashes(tmp_path) != before
