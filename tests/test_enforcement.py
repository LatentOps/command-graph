import io
import json
import sys
from pathlib import Path

from ordin.cli import main
from ordin.enforcement import enforcement_exit_code


def test_enforcement_exit_code_contract():
    assert enforcement_exit_code("allow", enforce=True) == 0
    assert enforcement_exit_code("warn", enforce=True) == 10
    assert enforcement_exit_code("ask", enforce=True) == 20
    assert enforcement_exit_code("block", enforce=True) == 30


def test_default_cli_exit_behavior_remains_backward_compatible(capsys):
    assert main(["check", "rm -rf /", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["decision"] == "block"


def test_enforce_maps_warn_ask_and_block_to_distinct_codes(capsys):
    assert main(["check", "chmod 600 file.txt", "--json", "--enforce"]) == 10
    assert json.loads(capsys.readouterr().out)["decision"] == "warn"

    assert main(["check", "unknown-command --flag", "--json", "--enforce"]) == 20
    assert json.loads(capsys.readouterr().out)["decision"] == "ask"

    assert main(["check", "rm -rf /", "--json", "--enforce"]) == 30
    assert json.loads(capsys.readouterr().out)["decision"] == "block"


def test_fail_on_threshold_can_allow_lower_decisions(capsys):
    assert main(["check", "chmod 600 file.txt", "--json", "--fail-on", "block"]) == 0
    assert json.loads(capsys.readouterr().out)["decision"] == "warn"

    assert main(["check", "unknown-command", "--json", "--fail-on", "ask"]) == 20
    assert json.loads(capsys.readouterr().out)["decision"] == "ask"


def test_stdin_review_request_is_validated_and_enforced(monkeypatch, capsys):
    request = {
        "schema_version": "ordin.review_request.v1",
        "command": "rm -rf .",
        "intent": "clean generated files",
        "context": {
            "cwd": "/",
            "shell": "bash",
            "euid": 0,
            "interactive": False,
            "repo_root": None,
            "agent": "test-agent",
        },
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    exit_code = main(["review", "--stdin", "--json", "--enforce"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 30
    assert payload["decision"] == "block"
    assert payload["context"]["cwd"] == "/"


def test_stdin_requires_versioned_schema(monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"command": "git status", "intent": None, "context": None})),
    )
    exit_code = main(["review", "--stdin", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"] == "invalid_review_request"
    assert "schema validation failed" in payload["message"]


def test_documented_review_example_is_accepted_through_stdin(monkeypatch, capsys):
    example = Path(__file__).resolve().parents[1] / "examples" / "review.json"
    text = example.read_text(encoding="utf-8")
    request = json.loads(text)
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))

    exit_code = main(["review", "--stdin", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload["command"] == request["command"]
    assert payload["context"]["cwd"] == request["context"]["cwd"]
    assert "decision" in payload


def test_invalid_stdin_json_is_structured_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not-json"))
    exit_code = main(["review", "--stdin", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert payload["error"] == "invalid_review_request"


def test_stdin_rejects_mixed_request_context(monkeypatch, capsys):
    request = {
        "schema_version": "ordin.review_request.v1",
        "command": "git status",
        "intent": None,
        "context": None,
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(request)))
    exit_code = main(["review", "--stdin", "--cwd", "/repo", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 2
    assert "cannot be combined" in payload["message"]
