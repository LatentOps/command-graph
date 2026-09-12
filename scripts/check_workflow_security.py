"""Dependency-free guard for immutable workflow actions and credential hygiene."""

from __future__ import annotations

import re
from pathlib import Path


def workflow_errors(path: Path) -> list[str]:
    source = path.read_text(encoding="utf-8")
    errors = []
    lines = source.splitlines()
    for index, line in enumerate(lines):
        match = re.match(r"\s*(?:-\s*)?uses:\s*([^\s#]+)", line)
        if not match:
            continue
        action = match[1]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[a-f0-9]{40}", action):
            errors.append(f"{path.name}:{index + 1}: action must use a full immutable commit SHA")
        if not re.search(r"#\s+v\d", line):
            errors.append(f"{path.name}:{index + 1}: pin needs a human-readable version comment")
        if action.startswith("actions/checkout@"):
            following = []
            for child in lines[index + 1 :]:
                if re.match(r"\s*- (?:name|uses|run):", child):
                    break
                following.append(child)
            if not re.search(r"persist-credentials:\s*false", "\n".join(following)):
                errors.append(
                    f"{path.name}:{index + 1}: checkout must disable persisted credentials"
                )
    if "pull_request_target" in source or "secrets: inherit" in source or "write-all" in source:
        errors.append(f"{path.name}: privileged PR execution or broad permissions are forbidden")
    if re.search(r"\$\{\{\s*github\.event\.(?:pull_request|issue|comment)\.(?:title|body)", source):
        errors.append(f"{path.name}: pass untrusted event text through explicit environment inputs")
    if not re.search(r"(?m)^permissions:\s*\n  contents: read\s*$", source):
        errors.append(f"{path.name}: workflow default must be contents: read")
    return errors


def main() -> int:
    root = Path(__file__).resolve().parents[1] / ".github" / "workflows"
    errors = [error for path in sorted(root.glob("*.y*ml")) for error in workflow_errors(path)]
    for error in errors:
        print(error)
    if not errors:
        print("Workflow actions are pinned; credential and PR boundaries passed.")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
