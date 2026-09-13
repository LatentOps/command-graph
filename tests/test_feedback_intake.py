import copy
import json
from pathlib import Path
import runpy
import sys

import pytest

from ordin.trace_replay import (
    load_capture_conformance,
    promote_candidate,
    replay_candidate,
    replay_integration_candidate,
)


ROOT = Path(__file__).resolve().parents[1]
SUMMARY = runpy.run_path(str(ROOT / "scripts/summarize_feedback.py"))
main = SUMMARY["main"]
summarize = SUMMARY["summarize"]


def test_synthetic_report_becomes_private_reconstruction_and_regression(tmp_path, monkeypatch):
    directory = tmp_path / "demo"
    monkeypatch.setattr(sys, "argv", ["trace_capture_demo.py", str(directory)])
    runpy.run_path(str(ROOT / "examples/trace_capture_demo.py"), run_name="__main__")
    text = (directory / "candidate.json").read_text()
    for private in ("private payload", "private-repo", "private.example.invalid", "private-demo"):
        assert private not in text
    candidate = json.loads(text)
    assert replay_candidate(candidate)["ok"]
    assert replay_integration_candidate(candidate)["ok"]
    destination = tmp_path / "regressions.jsonl"
    assert promote_candidate(candidate, "conformance", destination)["ok"]
    assert all(report["ok"] for report in load_capture_conformance(destination))


def test_summary_counts_reports_without_identity_or_unknown_durations():
    payload = json.loads((ROOT / "tests/fixtures/feedback-summary-input.json").read_text())
    report = summarize(payload)
    assert report["reports"] == 3
    assert report["counts"]["failure"]["setup_failure"] == 2
    assert report["permanent_regressions"] == 1
    assert report["reported_setup_time"] == {"samples": 2, "mean_minutes": 10.0}
    empty = summarize({"schema_version": "ordin.feedback_summary_input.v1", "reports": []})
    assert empty["reported_setup_time"] == {"samples": 0, "mean_minutes": None}
    for extra in (
        {"user": "private"},
        {"integration": "private-host"},
        {"minutes_to_first_working": float("nan")},
        {"minutes_to_first_working": True},
    ):
        changed = copy.deepcopy(payload)
        changed["reports"][0].update(extra)
        with pytest.raises(ValueError):
            summarize(changed)


def test_summary_refuses_existing_output_and_does_not_echo_bad_input(tmp_path, capsys):
    target = tmp_path / "report.json"
    target.write_text("keep")
    assert (
        main([str(ROOT / "tests/fixtures/feedback-summary-input.json"), "--output", str(target)])
        == 2
    )
    assert target.read_text() == "keep"
    source = tmp_path / "input.json"
    source.write_text('{"secret":"never echo this"}')
    assert main([str(source)]) == 2
    assert "never echo this" not in capsys.readouterr().out
