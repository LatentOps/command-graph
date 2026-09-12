import json
from pathlib import Path

from ordin.claude_code import ClaudeCodeIntegration
from ordin.codex import CodexIntegration
from ordin.schema import SCHEMA_FILES, load_schema, validate_named_schema


ROOT = Path(__file__).resolve().parents[1]


def test_versioned_onboarding_json_examples_use_registered_contracts():
    versions = {
        load_schema(name).get("properties", {}).get("schema_version", {}).get("const"): name
        for name in SCHEMA_FILES
    }
    checked = 0
    for path in (ROOT / "examples").rglob("*.json"):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if "schema_version" not in payload:
            continue
        version = payload["schema_version"]
        assert version in versions, path
        assert not validate_named_schema(versions[version], payload), path
        checked += 1
    assert checked >= 8


def test_documented_hook_requests_are_benign_and_need_no_host_binary():
    claude = json.loads((ROOT / "examples/claude-code-pre.json").read_text())
    codex = json.loads((ROOT / "examples/codex-pre.json").read_text())
    assert ClaudeCodeIntegration().review_pre_tool(claude).may_execute
    assert CodexIntegration().review_pre_tool(codex).may_execute


def test_quickstart_checks_are_part_of_the_isolated_wheel_ci_gate():
    workflow = (ROOT / ".github/workflows/tests.yml").read_text()
    assert "/tmp/ordin-wheel/bin/python -I scripts/check_quickstarts.py" in workflow
    assert "quickstarts-report.json" in workflow
