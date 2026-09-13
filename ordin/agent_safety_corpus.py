"""Evaluate versioned, sanitized semantic cases and explicit boundary controls."""

from __future__ import annotations

from collections import Counter
import hashlib
import math
import platform
import re
from time import perf_counter_ns
from pathlib import Path
from typing import Any

from . import __version__
from ._json_contracts import load_configuration
from .corpus_controls import CONTROLS
from .regression_replay import _scan_sensitive
from .trace_replay import replay_integration_candidate, validate_candidate
from .trajectory_corpus import run_agent_trajectory_corpus


CASE_CONTROLS = {
    **{name: name for name in CONTROLS if name != "child_isolation"},
    "secret_child": "child_isolation",
    "destructive_retries": None,
}


def load_corpus(path: Path) -> dict[str, Any]:
    data = load_configuration(path, label="Agent Safety Corpus", maximum=1048576)
    if (
        set(data) != {"corpus_version", "minimum_ordin", "cases"}
        or data["corpus_version"] != "1.0.0"
    ):
        raise ValueError("unsupported corpus snapshot")
    _scan_sensitive(data)
    cases = data["cases"]
    if not isinstance(cases, list) or len(cases) != len(CASE_CONTROLS):
        raise ValueError("incomplete v1 corpus")
    seen = set()
    for case in cases:
        if not isinstance(case, dict) or set(case) != {
            "id",
            "provenance",
            "source",
            "integrations",
            "failure_classes",
            "control",
            "candidate",
        }:
            raise ValueError("invalid corpus item")
        name = case["id"]
        if (
            not isinstance(name, str)
            or name not in CASE_CONTROLS
            or name in seen
            or case["control"] != CASE_CONTROLS[name]
        ):
            raise ValueError("duplicate, missing, or unknown corpus control")
        seen.add(name)
        source = case["source"]
        if case["provenance"] == "synthetic_from_failure":
            if (
                not isinstance(source, dict)
                or set(source) != {"pr", "revision"}
                or type(source["pr"]) is not int
                or source["pr"] not in {125, 142, 143, 144}
                or not isinstance(source["revision"], str)
                or not re.fullmatch(r"[0-9a-f]{40}", source["revision"])
            ):
                raise ValueError("maintainer reconstruction requires pinned public source")
        elif case["provenance"] != "synthetic_control" or source is not None:
            raise ValueError("invalid source provenance")
        for field in ("integrations", "failure_classes"):
            values = case[field]
            if (
                not isinstance(values, list)
                or not 1 <= len(values) <= 16
                or any(
                    not isinstance(value, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", value)
                    for value in values
                )
            ):
                raise ValueError("invalid corpus classification")
        if not isinstance(case["candidate"], dict):
            raise ValueError("missing sanitized candidate")
        validate_candidate(case["candidate"])
    return data


def _latency(samples: list[int]) -> dict[str, float | int]:
    values = sorted(samples)
    return {
        "samples": len(values),
        **{
            label: values[max(0, math.ceil(len(values) * quantile) - 1)] / 1e6 if values else 0.0
            for label, quantile in (("p50", 0.5), ("p95", 0.95), ("p99", 0.99))
        },
    }


def evaluate(path: Path, *, revision: str) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("report requires an exact Git revision")
    data = load_corpus(path)
    results = []
    decisions: Counter[str] = Counter()
    metrics: Counter[str] = Counter(
        {
            name: 0
            for name in (
                "critical_misses",
                "false_allows",
                "false_blocks",
                "unnecessary_escalations",
                "control_failures",
                "contract_drift_failures",
                "identity_isolation_failures",
                "observation_linkage_failures",
                "transport_parity_failures",
                "context_policy_failures",
                "session_isolation_failures",
                "deadline_failures",
            )
        }
    )
    applicable: Counter[str] = Counter()
    core_samples = []
    integration_samples = []
    control_samples = []
    contextual = detected = actions = 0
    errors = []
    for case in data["cases"]:
        candidate = case["candidate"]
        trajectory = validate_candidate(candidate).trajectory
        start = perf_counter_ns()
        core = run_agent_trajectory_corpus([trajectory]).results[0]
        core_samples.append(perf_counter_ns() - start)
        start = perf_counter_ns()
        integration = replay_integration_candidate(candidate)
        integration_samples.append(perf_counter_ns() - start)
        for step in core.steps:
            decisions[step.actual] += 1
            actions += 1
            metrics["critical_misses"] += step.expected == "block" and step.actual != "block"
            metrics["false_allows"] += step.expected != "allow" and step.actual == "allow"
            metrics["false_blocks"] += step.expected != "block" and step.actual == "block"
            metrics["unnecessary_escalations"] += step.expected == "allow" and step.actual in {
                "warn",
                "ask",
            }
        contextual += trajectory.contextual_required
        detected += trajectory.contextual_required and core.contextual_signal_added
        controls = {}
        if case["control"] is not None:
            start = perf_counter_ns()
            try:
                controls = CONTROLS[case["control"]]()
            except Exception as exc:
                controls = {"unavailable": False}
                errors.append(f"{case['id']}: control raised {type(exc).__name__}")
            control_samples.append(perf_counter_ns() - start)
            for name, passed in controls.items():
                applicable[name] += 1
                if not passed:
                    metrics["control_failures"] += 1
                    metrics[name + "_failures"] += 1
        ok = core.matches and integration["ok"] and all(controls.values())
        if not ok:
            errors.append(case["id"] + ": invariant failed")
        results.append(
            {
                "id": case["id"],
                "ok": bool(ok),
                "core_match": core.matches,
                "integration_match": integration["ok"],
                "boundary_controls": controls,
                "expected": [step.expected for step in core.steps],
                "actual": [step.actual for step in core.steps],
            }
        )
    return {
        "schema_version": "ordin.agent_safety_corpus_report.v1",
        "corpus_version": data["corpus_version"],
        "revision": revision,
        "dataset_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "environment": {
            "ordin": __version__,
            "python": platform.python_version(),
            "platform": platform.platform(),
        },
        "scope": {
            "cases": len(results),
            "semantic_actions": actions,
            "integrations": dict(
                Counter(name for case in data["cases"] for name in case["integrations"])
            ),
            "provenance": dict(Counter(case["provenance"] for case in data["cases"])),
            "failure_classes": dict(
                Counter(name for case in data["cases"] for name in case["failure_classes"])
            ),
        },
        "decisions": dict(decisions),
        "metrics": dict(metrics),
        "boundary_applicable": dict(applicable),
        "boundary_detection_rates": {
            name: (count - metrics[name + "_failures"]) / count
            for name, count in applicable.items()
        },
        "contextual": {
            "cases": contextual,
            "detected": detected,
            "rate": detected / contextual if contextual else None,
        },
        "latency_ms": {
            "core_trajectory": _latency(core_samples),
            "integration_reconstruction": _latency(integration_samples),
            "boundary_control": _latency(control_samples),
        },
        "results": results,
        "errors": errors,
        "ok": not errors,
        "limitations": [
            "Five maintainer-derived boundary reproductions and four intentionally synthetic controls; no customer traces.",
            "Sanitized candidates are semantic anchors; protocol/state defects require the separate named controls.",
            "Core counts include anchors, not the pressure loop's internal protocol messages; no production prevalence estimate.",
            "Timings are per trajectory or complete boundary control, not per tool; reconstruction includes validation and adapter setup.",
            "HTTP is loopback only. No model, credentials, remote server, actual tool execution, or proprietary host binary is exercised.",
        ],
    }


def render_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Agent Safety Corpus v1 evaluation",
        "",
        f"Revision: `{report['revision']}`.",
        f"Dataset SHA-256: `{report['dataset_sha256']}`.",
        f"Environment: Ordin {report['environment']['ordin']}, Python {report['environment']['python']}, {report['environment']['platform']}.",
        "",
        f"Result: {sum(item['ok'] for item in report['results'])}/{report['scope']['cases']} cases passed; {report['scope']['semantic_actions']} semantic actions.",
        "",
        "| Metric | Count |",
        "| --- | --- |",
    ]
    lines.extend(f"| {name} | {value} |" for name, value in report["metrics"].items())
    lines.extend(
        [
            "",
            f"Context-dependent detection: {report['contextual']['detected']}/{report['contextual']['cases']}.",
            "",
            "| Case | Core replay | Integration replay | Boundary controls |",
            "| --- | --- | --- | --- |",
        ]
    )
    lines.extend(
        f"| {item['id']} | {item['core_match']} | {item['integration_match']} | {', '.join(name + '=' + str(value) for name, value in item['boundary_controls'].items()) or 'semantic only'} |"
        for item in report["results"]
    )
    lines.extend(
        [
            "",
            "| Latency (ms per trajectory/control) | p50 | p95 | p99 |",
            "| --- | --- | --- | --- |",
        ]
    )
    lines.extend(
        f"| {name} | {value['p50']:.3f} | {value['p95']:.3f} | {value['p99']:.3f} |"
        for name, value in report["latency_ms"].items()
    )
    lines.extend(["", *[f"- {item}" for item in report["limitations"]], ""])
    return "\n".join(lines)
