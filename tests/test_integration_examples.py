from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "integrations"


def _run_python(name: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EXAMPLES / name), *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )


def test_python_quickstart_reviews_executes_and_records_observation():
    completed = _run_python("quickstart.py")

    assert completed.returncode == 0, completed.stderr
    assert "decision=allow disposition=execute" in completed.stdout
    assert "README.md" in completed.stdout
    assert "observation=ordin.action_observation.v1 exit=0" in completed.stdout


def test_json_subprocess_example_uses_versioned_cli_boundary():
    completed = _run_python("json_subprocess.py")

    assert completed.returncode == 0, completed.stderr
    review = json.loads(completed.stdout)
    assert review["schema_version"] == "ordin.action_review.v1"
    assert review["decision"] == "allow"
    assert review["action"]["action_id"] == "starter-kit-json-1"


def test_ci_gate_allows_safe_command_and_rejects_blocked_command():
    safe = _run_python("ci_gate.py", "--intent", "list files", "ls README.md")
    blocked = _run_python("ci_gate.py", "--intent", "remove files", "rm -rf /")

    assert safe.returncode == 0, safe.stdout + safe.stderr
    assert "decision=allow" in safe.stdout
    assert blocked.returncode == 1
    assert "decision=block" in blocked.stdout


def test_mcp_starter_fixture_runs_through_installed_proxy():
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "read_note", "arguments": {"path": "README.md"}},
    }
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "ordin.mcp_proxy",
            "--server-id",
            "starter-kit",
            "--semantics",
            str(EXAMPLES / "mcp-semantics.json"),
            "--",
            sys.executable,
            str(EXAMPLES / "fixture_mcp_server.py"),
        ],
        cwd=ROOT,
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=15,
    )

    assert completed.returncode == 0, completed.stderr
    response = json.loads(completed.stdout)
    assert response["id"] == 1
    assert response["result"]["content"][0]["text"] == "fixture note"
