"""Deterministic semantic reconstruction and guarded fixture promotion."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .regression_promotion import RegressionReplay, _sanitization_errors
from .regression_replay import FailureRegressionCase, _scan_sensitive, run_failure_regressions
from .tool_calls import ToolSemanticsRegistry
from .trace_capture import digest, read_capture
from .schema import validate_named_schema
from .session import _load


TRACE_CANDIDATE_SCHEMA_VERSION = "ordin.trace_candidate.v1"
_RAW_FIELDS = {
    "raw_action",
    "raw_observation",
    "tool_response",
    "prompt",
    "transcript",
    "environment",
    "source_code",
    "command_output",
    "capture_path",
}


def _reject_raw(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            if key in _RAW_FIELDS:
                raise ValueError("raw private fields cannot be promoted")
            _reject_raw(child)
    elif isinstance(value, list):
        for child in value:
            _reject_raw(child)


def sanitize_capture(
    path: str | Path,
    *,
    expected: str,
    category: str | None = None,
    session_key: str | None = None,
    start: int = 1,
    end: int | None = None,
) -> dict[str, Any]:
    if expected not in {"allow", "warn", "ask", "block"}:
        raise ValueError("an explicit final decision expectation is required")
    if category is not None and not re.fullmatch(r"[a-z][a-z0-9_.-]{0,127}", category):
        raise ValueError("invalid expected trajectory category")
    if start < 1 or (end is not None and end < start):
        raise ValueError("invalid capture segment")
    capture = read_capture(path)
    sessions = {event["session_key"] for event in capture["events"]}
    if session_key is None:
        if len(sessions) != 1:
            raise ValueError("select exactly one captured session")
        session_key = next(iter(sessions))
    records = [
        event
        for event in capture["events"]
        if event["session_key"] == session_key
        and event["sequence"] >= start
        and (end is None or event["sequence"] <= end)
    ]
    reviews = [event for event in records if event["event"] == "review"]
    if any(event["event"] == "boundary" for event in records):
        raise ValueError("select a segment between session lifecycle boundaries")
    preceding = None
    for event in records:
        if event["event"] == "review":
            preceding = event["action_key"]
        elif event["action_key"] != preceding:
            raise ValueError(
                "interleaved observations require a narrower segment or manual reconstruction"
            )
    if not 1 <= len(reviews) <= 32:
        raise ValueError("select a segment with 1 to 32 reviewed actions")
    if any(event.get("truncated") for event in records):
        raise ValueError("truncated evidence requires manual reconstruction")
    if (
        len({event.get("configuration_digest") for event in reviews}) > 1
        or len({event["integration"] for event in reviews}) > 1
    ):
        raise ValueError("select a segment with one integration configuration")
    observations = {
        event["action_key"]: event for event in records if event["event"] == "observation"
    }
    if set(observations) - {event["action_key"] for event in reviews}:
        raise ValueError("segment contains an observation without its reviewed action")
    names: dict[tuple[str, str], str] = {}

    def name(kind: str, value: str) -> str:
        key = (kind, value)
        if key not in names:
            names[key] = f"{kind}-{1 + sum(category == kind for category, _ in names)}"
        return names[key]

    resources: dict[str, str] = {}

    def resource_value(resource: Mapping[str, str]) -> str:
        key = resource["value_digest"]
        if key not in resources:
            index = len(resources) + 1
            kind = resource["type"]
            resources[key] = (
                f"https://host-{index}.example.invalid/resource"
                if kind == "url"
                else f"host-{index}.example.invalid"
                if kind in {"host", "network", "endpoint"}
                else f"/workspace/resource-{index}"
                if kind in {"path", "file", "directory"}
                else f"resource-{index}"
            )
        return resources[key]

    rules: dict[tuple[str, str, str], dict[str, Any]] = {}
    steps = []
    for index, event in enumerate(reviews):
        action_id = f"capture-action-{index + 1}"
        identity = event["identity"]
        kind = "mcp" if event["kind"] == "mcp" else "tool"
        scope = (
            name("server", identity["server"])
            if kind == "mcp"
            else name("runtime", identity.get("runtime", digest(event["integration"])))
        )
        tool = name("tool", identity.get("tool", event["action_key"]))
        # Shell summaries need independent synthetic identities because their
        # effects depend on arguments that were deliberately not captured.
        if event["kind"] == "shell":
            tool = name("shell", event["action_key"])
        arguments = {
            f"resource_{i}": resource_value(resource)
            for i, resource in enumerate(event["resources"])
        }
        bindings = [
            {"argument": f"resource_{i}", "type": resource["type"]}
            for i, resource in enumerate(event["resources"])
        ]
        effects = set(event["effects"])
        # The existing temporal policy treats secret_exposure as a secret-read
        # signal. Preserve that abstract property without preserving a filename.
        if "category:secret_exposure" in event.get("temporal_signals", []):
            effects.add("secret.read")
        rule_key = (kind, scope, tool)
        if effects:
            existing = rules.get(rule_key)
            if existing is not None:
                if existing["effects"] != sorted(effects):
                    raise ValueError(
                        "changing tool semantics require a narrower segment or manual reconstruction"
                    )
                merged = {binding["argument"]: binding["type"] for binding in existing["resources"]}
                for binding in bindings:
                    if (
                        binding["argument"] in merged
                        and merged[binding["argument"]] != binding["type"]
                    ):
                        raise ValueError("changing resource bindings require manual reconstruction")
                    merged[binding["argument"]] = binding["type"]
                bindings = [
                    {"argument": argument, "type": kind}
                    for argument, kind in sorted(merged.items())
                ]
            rules[rule_key] = {
                "id": f"captured-rule-{len(rules) + 1}" if existing is None else existing["id"],
                "kind": kind,
                "server" if kind == "mcp" else "runtime": scope,
                "tool": tool,
                "effects": sorted(effects),
                "resources": bindings,
            }
        parameters = {
            "server" if kind == "mcp" else "runtime": scope,
            "tool": tool,
            "arguments": arguments,
        }
        action = {
            "schema_version": "ordin.action_envelope.v1",
            "action_id": action_id,
            "kind": kind,
            "operation": "call",
            "parameters": parameters,
            "intent": None,
            "context": None,
        }
        if "signal:path_execution" in event.get("temporal_signals", []):
            action = {
                **action,
                "kind": "shell",
                "operation": "execute",
                "parameters": {"command": "./redacted-program"},
            }
        step: dict[str, Any] = {
            "action": action,
            "expected": expected if index == len(reviews) - 1 else event["decision"],
        }
        if index == len(reviews) - 1 and category:
            step["expected_categories"] = [category]
        observation = observations.get(event["action_key"])
        if observation:
            step["observation"] = {
                "schema_version": "ordin.action_observation.v1",
                "action_id": action_id,
                "exit_code": observation["exit_code"],
                "effects": observation["effects"],
                "resources": [
                    {"type": resource["type"], "value": resource_value(resource)}
                    for resource in observation["resources"]
                ],
                "metadata": {"status": observation["status"]},
            }
        steps.append(step)
    source_hash = digest(records)
    case_id = f"capture-{source_hash[:16]}"
    semantics = {
        "schema_version": "ordin.tool_semantics.v1",
        "registry_id": case_id,
        "version": "1",
        "rules": list(rules.values()),
    }
    ToolSemanticsRegistry.from_dict(semantics)
    case = {
        "schema_version": "ordin.regression_case.v1",
        "id": case_id,
        "failure_class": "policy_temporal" if category else "integration_translation",
        "invariant": f"Final review must be {expected}"
        + (f" with {category}." if category else "."),
        "source": f"trace_capture:{reviews[0]['integration']}",
        "trajectory": {
            "schema_version": "ordin.agent_trajectory.v1",
            "id": case_id + "-trajectory",
            "source": "trace_capture",
            "provenance_kind": "synthetic_from_failure",
            "behavior_classes": ["post_action_influence" if observations else "identity_change"],
            "contextual_required": bool(category),
            "tags": ["captured", "semantic_reconstruction"],
            "tool_semantics": semantics,
            "steps": steps,
        },
    }
    candidate = {
        "schema_version": TRACE_CANDIDATE_SCHEMA_VERSION,
        "derivation": "semantic_reconstruction",
        "capture_digest": source_hash,
        "source_mode": capture["mode"],
        "integration": reviews[0]["integration"],
        "review_policy": reviews[0]["fail_on"],
        "captured_decisions": [event["decision"] for event in reviews],
        "capture_context": {
            key: reviews[0][key]
            for key in (
                "ordin_version",
                "ordin_revision",
                "configuration_digest",
                "integration_version",
            )
        },
        "case": case,
    }
    validate_candidate(candidate)
    return candidate


def validate_candidate(candidate: Mapping[str, Any]) -> FailureRegressionCase:
    from .mcp_contracts import canonical_json

    canonical_json(candidate)
    if validate_named_schema("trace_candidate", dict(candidate)):
        raise ValueError("invalid trace candidate schema")
    allowed = {
        "schema_version",
        "derivation",
        "capture_digest",
        "source_mode",
        "integration",
        "review_policy",
        "captured_decisions",
        "capture_context",
        "case",
    }
    if (
        set(candidate) != allowed
        or candidate.get("schema_version") != TRACE_CANDIDATE_SCHEMA_VERSION
        or candidate.get("derivation") != "semantic_reconstruction"
    ):
        raise ValueError("invalid trace candidate contract")
    _reject_raw(candidate)
    _scan_sensitive(candidate)
    if _sanitization_errors(candidate):
        raise ValueError("candidate contains sensitive values")
    case = FailureRegressionCase.from_dict(candidate["case"])
    if case.trajectory.provenance_kind != "synthetic_from_failure":
        raise ValueError("semantic reconstruction provenance cannot be strengthened")
    if len(candidate["captured_decisions"]) != len(case.trajectory.steps):
        raise ValueError("candidate decision count must match the captured segment")
    return case


def replay_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    case = validate_candidate(candidate)
    report = run_failure_regressions([case])
    return {
        "ok": report.matches == 1,
        "id": case.id,
        "captured_decisions": candidate["captured_decisions"],
        "replayed_decisions": [step.actual for step in report.results[0].trajectory_result.steps],
        "errors": report.regression_errors(),
        "provenance_kind": case.trajectory.provenance_kind,
    }


def promote_candidate(
    candidate: Mapping[str, Any], target: str, output: str | Path
) -> dict[str, Any]:
    case = validate_candidate(candidate)
    replay = replay_candidate(candidate)
    if not replay["ok"]:
        raise ValueError("sanitized candidate does not reproduce the reviewed invariant")
    if target == "trajectory":
        payload = candidate["case"]["trajectory"]
    elif target in {"failure", "conformance"}:
        payload = candidate["case"]
        if target == "conformance":
            controls = replay_integration_candidate(candidate)
            if not controls["ok"]:
                raise ValueError("candidate does not reproduce through the selected integration")
            payload = {
                **payload,
                "capture_integration": candidate["integration"],
                "capture_policy": candidate["review_policy"],
            }
    elif target == "extended":
        payload = {
            "schema_version": "ordin.regression_replay.v1",
            "id": case.id,
            "failure_class": "integration_translation",
            "severity": "high"
            if any(step.expected == "block" for step in case.trajectory.steps)
            else "medium",
            "invariant": case.invariant,
            "why_it_matters": "Retain the reviewed property of a locally captured action sequence.",
            "kind": "trajectory",
            "trajectory": candidate["case"]["trajectory"],
            "origin": "semantic_reconstruction_from_local_capture",
        }
        RegressionReplay.from_dict(payload)
    else:
        raise ValueError("unknown trace promotion target")
    payload = {
        **payload,
        "capture_provenance": {key: value for key, value in candidate.items() if key != "case"},
    }
    _scan_sensitive(payload)
    path = Path(output).absolute()
    # Cooperating promoters serialize the entire read/validate/replace operation.
    # A stale lock is never stolen: the user must inspect it before removing it.
    lock_path = path.with_name(path.name + ".ordin-lock")
    lock = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        _append_corpus(path, payload, target)
    finally:
        os.close(lock)
        lock_path.unlink()
    return {"ok": True, "id": payload["id"], "target": target, "output": str(path)}


def _read_corpus(path: Path) -> bytes:
    if path.is_symlink():
        raise ValueError("promotion destination must not be a symlink")
    if not path.exists():
        return b""
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("promotion corpus exceeds size limit")
    with path.open("rb") as handle:
        content = handle.read(16 * 1024 * 1024 + 1)
    if len(content) > 16 * 1024 * 1024:
        raise ValueError("promotion corpus exceeds size limit")
    return content


def _append_corpus(path: Path, payload: Mapping[str, Any], target: str) -> None:
    from .trajectory_corpus import AgentTrajectory

    existing = _read_corpus(path)
    for line in existing.splitlines():
        if line.strip() and not line.lstrip().startswith(b"#"):
            record = _load(line.decode("utf-8"))
            if target == "trajectory":
                AgentTrajectory.from_dict(record)
            elif target == "extended":
                RegressionReplay.from_dict(record)
            else:
                FailureRegressionCase.from_dict(record)
            if record.get("id") == payload["id"]:
                raise ValueError("promotion id already exists")
    encoded = (
        existing
        + (b"\n" if existing and not existing.endswith(b"\n") else b"")
        + json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        + b"\n"
    )
    descriptor, temporary = tempfile.mkstemp(prefix=".ordin-promote-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        # Refuse a concurrent edit rather than replacing another user's work.
        if _read_corpus(path) != existing:
            raise ValueError("promotion destination changed; review and retry")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def replay_integration_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Replay abstract effects through a maintained integration, never a tool."""
    from .action import ActionEnvelope
    from .agent import AgentGate
    from .api import Ordin
    from .claude_code import ClaudeCodeIntegration
    from .codex import CodexIntegration
    from .cursor import CursorIntegration, _identity as cursor_identity
    from .mcp_proxy import MCPStdioSafetyProxy
    from .policy import ReviewPolicy, validate_fail_threshold
    from .session import IntegrationSession, SessionIdentity

    case = validate_candidate(candidate)
    runtime = candidate["integration"]
    if runtime not in {"claude-code", "codex", "cursor", "mcp-proxy", "mcp-http"}:
        return {
            "ok": False,
            "errors": ["integration reconstruction requires supported abstract tool actions"],
        }
    registry = candidate["case"]["trajectory"]["tool_semantics"]
    translated_rules = []
    aliases = {}
    for rule in registry["rules"]:
        alias = (
            "captured_"
            + digest([rule["kind"], rule.get("server"), rule.get("runtime"), rule["tool"]])[:16]
        )
        aliases[(rule["kind"], rule.get("server"), rule.get("runtime"), rule["tool"])] = alias
        translated_rules.append(
            {
                "id": rule["id"],
                "kind": "mcp" if runtime.startswith("mcp-") else "tool",
                "server" if runtime.startswith("mcp-") else "runtime": "capture-fixture"
                if runtime.startswith("mcp-")
                else runtime,
                "tool": alias,
                "effects": rule["effects"],
                "resources": rule["resources"],
            }
        )
    semantics = ToolSemanticsRegistry.from_dict({**registry, "rules": translated_rules})
    from .audit import build_audit_event

    class Collector:
        review: Any = None

        def record(self, review):
            self.review = review
            return build_audit_event(review)

    collector = Collector()
    gate = AgentGate(
        Ordin(
            tool_semantics=semantics,
            policy=ReviewPolicy(validate_fail_threshold(candidate["review_policy"])),
            audit=collector,
        )
    )
    state = IntegrationSession(
        cursor_identity({"conversation_id": "capture-fixture"})
        if runtime == "cursor"
        else SessionIdentity(runtime, "capture-fixture"),
        gate,
    )
    integration = (
        ClaudeCodeIntegration(gate=gate, session=state)
        if runtime == "claude-code"
        else CodexIntegration(gate=gate, session=state)
        if runtime == "codex"
        else CursorIntegration(gate=gate, session=state)
        if runtime == "cursor"
        else None
    )
    proxy = (
        MCPStdioSafetyProxy(
            server_id="capture-fixture",
            gate=gate,
            runtime_id=runtime,
            shell_tools=frozenset({"captured_path_execute"}),
        )
        if runtime.startswith("mcp-")
        else None
    )
    results = []
    for index, step in enumerate(case.trajectory.steps):
        parameters = step.action.parameters
        key = (
            step.action.kind,
            parameters.get("server"),
            parameters.get("runtime"),
            parameters.get("tool"),
        )
        alias = aliases.get(key, "untrusted_" + digest(key)[:16])
        arguments = parameters.get("arguments", {})
        if step.action.kind == "shell":
            alias = "Bash" if integration is not None else "captured_path_execute"
            if runtime == "cursor":
                alias = "Shell"
            arguments = {"command": parameters["command"]}
        if integration is not None:
            payload = {
                "hook_event_name": "PreToolUse",
                "session_id": "capture-fixture",
                "turn_id": "capture-turn",
                "tool_use_id": str(index),
                "tool_name": alias,
                "tool_input": arguments,
                "cwd": "/workspace",
                "permission_mode": "default",
            }
            if runtime == "cursor":
                payload.update(
                    hook_event_name="preToolUse",
                    conversation_id="capture-fixture",
                    generation_id="capture-turn",
                    cursor_version="1.7.2",
                )
            decision = integration.review_pre_tool(payload)
            actual = decision.review.decision
            categories = decision.review.trajectory_categories or []
            if step.observation:
                integration.observation_from_hook(
                    {
                        **payload,
                        "hook_event_name": "postToolUse" if runtime == "cursor" else "PostToolUse",
                        "tool_response": {"exit_code": step.observation.exit_code},
                        "tool_output": json.dumps({"exitCode": step.observation.exit_code}),
                    },
                    observed_effects=step.observation.effects,
                )
        else:
            assert proxy is not None
            collector.review = None
            mapped = proxy.process_client_message(
                {
                    "jsonrpc": "2.0",
                    "id": index,
                    "method": "tools/call",
                    "params": {"name": alias, "arguments": arguments},
                }
            )
            if collector.review is None:
                return {"ok": False, "errors": ["integration rejected the reconstructed action"]}
            actual = collector.review.decision
            categories = collector.review.trajectory_categories or []
            if step.observation:
                if not mapped.forward:
                    return {
                        "ok": False,
                        "errors": [
                            "captured execution was not permitted by the reconstructed integration"
                        ],
                    }
                result = (
                    {"resultType": "captured_unknown"}
                    if step.observation.exit_code is None
                    else {"isError": step.observation.exit_code != 0, "content": []}
                )
                proxy.observe_server_message(
                    {"jsonrpc": "2.0", "id": index, "result": result},
                    observed_effects=step.observation.effects,
                )
        results.append(
            actual == step.expected and set(step.expected_categories).issubset(categories)
        )
    return {
        "ok": all(results),
        "checks": results,
        "errors": []
        if all(results)
        else ["integration replay differs from the reviewed expectation"],
    }


def load_capture_conformance(path: str | Path) -> list[dict[str, Any]]:
    """Load explicit promoted conformance cases; no implicit repository discovery."""
    reports = []
    seen: set[str] = set()
    for line in _read_corpus(Path(path)).splitlines():
        if not line.strip() or line.lstrip().startswith(b"#"):
            continue
        payload = _load(line.decode("utf-8"))
        provenance = payload.get("capture_provenance")
        if not isinstance(provenance, dict):
            raise ValueError("captured conformance requires promotion provenance")
        case = {
            key: value
            for key, value in payload.items()
            if key not in {"capture_provenance", "capture_integration", "capture_policy"}
        }
        candidate = {**provenance, "case": case}
        validated = validate_candidate(candidate)
        if validated.id in seen or len(seen) >= 256:
            raise ValueError("duplicate or excessive captured conformance cases")
        seen.add(validated.id)
        if (
            payload.get("capture_integration") != candidate["integration"]
            or payload.get("capture_policy") != candidate["review_policy"]
        ):
            raise ValueError("captured conformance identity mismatch")
        report = replay_integration_candidate(candidate)
        reports.append({**report, "id": validated.id, "integration": candidate["integration"]})
    if not reports:
        raise ValueError("captured conformance corpus must not be empty")
    return reports
