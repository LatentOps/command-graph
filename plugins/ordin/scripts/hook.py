"""Bounded launcher: handler startup/errors cannot silently allow a pre-tool call."""

import json
import subprocess
import sys


def deny(mode):
    if mode == "permission":
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny", "message": "Ordin review unavailable"},
            }
        }
    else:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Ordin review unavailable",
            }
        }
    print(json.dumps(output))


def main():
    mode = sys.argv[1] if len(sys.argv) == 2 else "pre"
    try:
        payload = sys.stdin.buffer.read(1_048_577)
        if len(payload) > 1_048_576:
            raise ValueError("oversized hook input")
        result = subprocess.run(
            [sys.executable, "-m", "ordin.codex", mode],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2 if mode == "session-end" else 12,
        )
        if result.returncode != 0 or len(result.stdout) > 65536:
            raise ValueError("hook handler failed")
        if mode in {"pre", "permission"}:
            output = json.loads(result.stdout)
            specific = output.get("hookSpecificOutput", {})
            if mode == "pre" and (
                specific.get("hookEventName") != "PreToolUse"
                or specific.get("permissionDecision") not in {"allow", "deny"}
            ):
                raise ValueError("unsupported pre-tool hook output")
            if (
                mode == "permission"
                and output
                and (
                    specific.get("hookEventName") != "PermissionRequest"
                    or specific.get("decision", {}).get("behavior") != "deny"
                )
            ):
                raise ValueError("unsupported permission hook output")
        sys.stdout.buffer.write(result.stdout)
        return 0
    except (OSError, ValueError, TypeError, AttributeError, subprocess.TimeoutExpired):
        if mode in {"pre", "permission"}:
            deny(mode)
            return 0
        print("Ordin hook failed; inspect the local integration setup", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
