from __future__ import annotations

import json
import subprocess
import sys


def main() -> int:
    request = {
        "schema_version": "ordin.action_envelope.v1",
        "kind": "shell",
        "operation": "execute",
        "parameters": {"command": "printf 'json boundary\\n'"},
        "intent": "demonstrate a non-Python JSON integration",
        "context": {"cwd": ".", "agent": "json-starter"},
        "action_id": "starter-kit-json-1",
    }
    completed = subprocess.run(
        ["ordin", "action", "--stdin", "--json"],
        input=json.dumps(request),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        print(completed.stderr, file=sys.stderr, end="")
        return completed.returncode

    review = json.loads(completed.stdout)
    print(json.dumps(review, indent=2, sort_keys=True))
    return 0 if review["decision"] == "allow" else 1


if __name__ == "__main__":
    raise SystemExit(main())
