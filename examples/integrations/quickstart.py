from __future__ import annotations

import subprocess

from ordin import ActionEnvelope, ActionObservation, AgentGate, ExecutionContext


def main() -> int:
    command = "printf 'ordin quickstart\\n'"
    action = ActionEnvelope(
        kind="shell",
        operation="execute",
        parameters={"command": command},
        intent="print a local quickstart marker",
        context=ExecutionContext(cwd=".", agent="starter-kit"),
        action_id="starter-kit-shell-1",
    )
    decision = AgentGate().evaluate_action(action)
    print(f"decision={decision.review.decision} disposition={decision.disposition}")

    if not decision.may_execute:
        print("caller did not execute the command")
        return 2 if decision.denied else 1

    # Execution is intentionally caller-owned. Ordin only reviewed the action.
    completed = subprocess.run(
        ["sh", "-c", command],
        check=False,
        capture_output=True,
        text=True,
    )
    observation = ActionObservation(
        action_id=action.action_id or "starter-kit-shell-1",
        exit_code=completed.returncode,
        metadata={"runtime": "starter-kit", "status": "completed"},
    )
    print(completed.stdout, end="")
    print(f"observation={observation.as_dict()['schema_version']} exit={observation.exit_code}")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
