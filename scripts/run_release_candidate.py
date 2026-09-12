"""Record exact-source acceptance of the installed candidate artifact."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def source_version() -> str:
    for statement in ast.parse((ROOT / "ordin/__init__.py").read_text(encoding="utf-8")).body:
        if isinstance(statement, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "__version__"
            for target in statement.targets
        ):
            value = ast.literal_eval(statement.value)
            if isinstance(value, str):
                return value
    raise ValueError("candidate source version is missing")


def validate_source(revision: str, output: Path) -> None:
    if re.fullmatch(r"[a-f0-9]{40}", revision) is None:
        raise ValueError("candidate requires a full commit SHA")
    if (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        != revision
    ):
        raise ValueError("candidate revision differs from checked-out source")
    if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT, text=True).strip():
        raise ValueError("candidate acceptance requires a clean source checkout")
    if output.resolve() == ROOT or ROOT in output.resolve().parents:
        raise ValueError("write candidate reports outside the source checkout")


def workload_commands(output: Path, revision: str) -> list[tuple[str, list[str]]]:
    return [
        ("safety", ["run_safety_benchmark.py", "--json-out", str(output / "safety.json")]),
        (
            "trajectories",
            ["run_trajectory_corpus.py", "--json-out", str(output / "trajectories.json")],
        ),
        (
            "failure_regressions",
            ["run_regression_replay.py", "--json-out", str(output / "failure-regressions.json")],
        ),
        (
            "extended_regressions",
            ["replay_regression.py", "--json-out", str(output / "extended-regressions.json")],
        ),
        (
            "conformance",
            ["run_integration_conformance.py", "--json-out", str(output / "conformance.json")],
        ),
        (
            "integration",
            [
                "run_integration_evaluation.py",
                "--revision",
                revision,
                "--json-out",
                str(output / "integration.json"),
                "--markdown-out",
                str(output / "integration.md"),
            ],
        ),
        (
            "runtime",
            [
                "run_runtime_evaluation.py",
                "--revision",
                revision,
                "--repo-root",
                str(ROOT),
                "--repetitions",
                "5",
                "--json-out",
                str(output / "runtime.json"),
                "--markdown-out",
                str(output / "runtime.md"),
            ],
        ),
        ("quickstarts", ["check_quickstarts.py", "--json-out", str(output / "quickstarts.json")]),
    ]


def report_index(output: Path) -> dict[str, str]:
    result = {}
    for path in sorted(output.iterdir()):
        if path.is_file() and path.name not in {"candidate.json", "candidate.md"}:
            digest = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            result[path.name] = digest.hexdigest()
    return result


def resource_hashes(package: Path) -> dict[str, str]:
    result = {}
    for directory in ("resources", "plugin_assets"):
        for path in sorted((package / directory).rglob("*")):
            if path.is_file() and path.suffix in {".json", ".py"}:
                text = path.read_text(encoding="utf-8")
                if path.suffix == ".json":
                    text = json.dumps(json.loads(text), sort_keys=True, separators=(",", ":"))
                result[path.relative_to(package).as_posix()] = hashlib.sha256(
                    text.encode()
                ).hexdigest()
    return result


def run_candidate(wheel_python: Path, output: Path, revision: str) -> dict[str, Any]:
    validate_source(revision, output)
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    env = {key: value for key, value in os.environ.items() if not key.startswith("ORDIN_")}
    probe = """import hashlib,json,platform,sys,pathlib,ordin
from importlib.metadata import distribution
p=distribution('ordin')
package=pathlib.Path(ordin.__file__).resolve().parent
assert pathlib.Path(sys.argv[1]).resolve() not in package.parents
assert p.version==ordin.__version__
resources={}
for directory in ('resources','plugin_assets'):
    for path in sorted((package/directory).rglob('*')):
        if path.is_file() and path.suffix in ('.json','.py'):
            text=path.read_text(encoding='utf-8')
            if path.suffix=='.json':
                text=json.dumps(json.loads(text),sort_keys=True,separators=(',',':'))
            resources[path.relative_to(package).as_posix()]=hashlib.sha256(text.encode()).hexdigest()
print(json.dumps({'ordin_version':ordin.__version__,'python':platform.python_version(),'platform':platform.platform(),'requires_dist':p.metadata.get_all('Requires-Dist',[]),'resources_sha256':resources}))
"""
    wheel = json.loads(
        subprocess.check_output(
            [str(wheel_python), "-I", "-c", probe, str(ROOT)],
            text=True,
            cwd=output,
            env=env,
            timeout=30,
        )
    )
    if wheel["ordin_version"] != source_version():
        raise ValueError("installed candidate version differs from source")
    if wheel["resources_sha256"] != resource_hashes(ROOT / "ordin"):
        raise ValueError("wheel schemas/data/plugin assets differ from source")
    from packaging.requirements import Requirement

    core = [
        value
        for value in wheel["requires_dist"]
        if (Requirement(value).marker is None or Requirement(value).marker.evaluate({"extra": ""}))
    ]
    if core:
        raise ValueError("candidate core unexpectedly declares runtime dependencies")
    tools = {
        package.metadata["Name"]: package.version
        for package in importlib.metadata.distributions()
        if package.metadata.get("Name")
    }
    payload: dict[str, Any] = {
        "schema_version": "ordin.release_candidate.v1",
        "revision": revision,
        "ok": False,
        "wheel_environment": wheel,
        "builder_environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "packages": dict(sorted(tools.items())),
            "git": subprocess.check_output(["git", "--version"], text=True).strip(),
            "gh": subprocess.check_output(["gh", "--version"], text=True).splitlines()[0],
        },
        "workflow": {
            key: os.environ.get(key)
            for key in ("GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT", "GITHUB_WORKFLOW_REF")
        },
        "core_required_dependencies": core,
        "steps": [],
        "ci_matrix": "must be verified independently for this SHA after all jobs complete",
        "limitations": "Finite local fixtures; not production prevalence, model latency, or a reproducible-build proof.",
    }
    try:
        for name, args in workload_commands(output, revision):
            started = time.monotonic()
            with (output / f"{name}.log").open("w", encoding="utf-8") as log:
                result = subprocess.run(
                    [str(wheel_python), "-I", str(ROOT / "scripts" / args[0]), *args[1:]],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    timeout=600,
                )
            payload["steps"].append(
                {
                    "name": name,
                    "exit_code": result.returncode,
                    "elapsed_seconds": round(time.monotonic() - started, 3),
                }
            )
            print(f"{name}: {'pass' if result.returncode == 0 else 'FAIL'}", flush=True)
            if result.returncode:
                raise ValueError(f"candidate {name} gate failed; inspect its log")
        payload["ok"] = True
    finally:
        payload["reports_sha256"] = report_index(output)
        (output / "candidate.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        lines = [
            f"# Candidate {revision}",
            "",
            f"Ordin {wheel['ordin_version']}; Python {wheel['python']}; {wheel['platform']}.",
            "",
            f"Artifact workload gate: {'PASS' if payload['ok'] else 'FAIL'}.",
            "",
            "The separate GitHub CI matrix and attestation run must also pass on this SHA.",
            "",
            "| Workload | Exit | Seconds |",
            "| --- | ---: | ---: |",
        ]
        lines.extend(
            f"| {step['name']} | {step['exit_code']} | {step['elapsed_seconds']} |"
            for step in payload["steps"]
        )
        lines += [
            "",
            "Detailed integration/runtime Markdown and all JSON/log reports are attached alongside this index. They contain workloads, decision/error counts and latency distributions under the existing limits.",
            "",
            payload["limitations"],
        ]
        (output / "candidate.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel-python", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--revision", required=True)
    args = parser.parse_args()
    run_candidate(args.wheel_python, args.output.absolute(), args.revision)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
