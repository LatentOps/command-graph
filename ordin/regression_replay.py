from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter_ns
from typing import Any, Literal, Mapping, cast

from .action_policy import ActionPolicySet
from .api import Ordin
from .execution import ActionObservation, ObservationHistory
from .policy import Decision
from .safety_benchmark import SafetyFixture
from .temporal import default_temporal_policy
from .tool_calls import ToolSemanticsRegistry
from .trajectory_corpus import AgentTrajectory, run_agent_trajectory_corpus


REGRESSION_REPLAY_SCHEMA_VERSION = "ordin.regression_replay.v1"
RegressionKind = Literal["safety", "trajectory"]
FailureClass = Literal[
    "false_allow",
    "critical_miss",
    "false_block",
    "unnecessary_escalation",
    "semantic_effect",
    "semantic_resource",
    "parser_normalizer",
    "identity",
    "policy",
    "temporal_policy",
    "provenance",
    "post_action_observation",
    "integration_translation",
    "performance",
]
Severity = Literal["low", "medium", "high", "critical"]
VALID_FAILURE_CLASSES = frozenset(
    {
        "false_allow",
        "critical_miss",
        "false_block",
        "unnecessary_escalation",
        "semantic_effect",
        "semantic_resource",
        "parser_normalizer",
        "identity",
        "policy",
        "temporal_policy",
        "provenance",
        "post_action_observation",
        "integration_translation",
        "performance",
    }
)
VALID_SEVERITIES = frozenset({"low", "medium", "high", "critical"})
MAX_REPLAYS = 512
MAX_OBSERVATIONS = 32
MAX_EXPECTED_CODES = 64
SENSITIVE_VALUE_PREFIXES = ("sk-", "ghp_", "github_pat_", "Bearer ", "AKIA")
SENSITIVE_KEY_FRAGMENTS = (
    "password",
    "passwd",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "secret_key",
    "private_key",
)
REDACTED_VALUES = frozenset({"<redacted>", "redacted", "***", "[redacted]"})


def _required_text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value.strip()


def _string_tuple(value: Any, *, name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} must be an array with at most {maximum} items")
    result: list[str] = []
    for item in value:
        text = _required_text(item, name=f"{name} item")
        if text not in result:
            result.append(text)
    return tuple(result)


def _sanitization_errors(value: Any, *, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}"
            lower_key = key_text.lower()
            if any(fragment in lower_key for fragment in SENSITIVE_KEY_FRAGMENTS):
                if isinstance(item, str) and item.lower() not in REDACTED_VALUES:
                    errors.append(f"{child_path} contains a non-redacted sensitive field")
            errors.extend(_sanitization_errors(item, path=child_path))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            errors.extend(_sanitization_errors(item, path=f"{path}[{index}]"))
    elif isinstance(value, str):
        if value.startswith(SENSITIVE_VALUE_PREFIXES):
            errors.append(f"{path} contains a credential-like value")
    return errors


@dataclass(frozen=True)
class RegressionReplay:
    id: str
    failure_class: FailureClass
    severity: Severity
    invariant: str
    why_it_matters: str
    kind: RegressionKind
    safety: SafetyFixture | None = None
    trajectory: AgentTrajectory | None = None
    tool_semantics: ToolSemanticsRegistry | None = None
    action_policy: ActionPolicySet | None = None
    observations: tuple[ActionObservation, ...] = ()
    expected_trajectory_categories: tuple[str, ...] = ()
    expected_provenance_codes: tuple[str, ...] = ()
    max_latency_ms: float | None = None
    origin: str | None = None
    schema_version: str = REGRESSION_REPLAY_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "RegressionReplay":
        if payload.get("schema_version") != REGRESSION_REPLAY_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported regression replay schema: {payload.get('schema_version')!r}"
            )
        sanitization_errors = _sanitization_errors(payload)
        if sanitization_errors:
            raise ValueError("unsafe regression fixture: " + "; ".join(sanitization_errors))

        replay_id = _required_text(payload.get("id"), name="regression id")
        failure_class = payload.get("failure_class")
        severity = payload.get("severity")
        invariant = _required_text(payload.get("invariant"), name=f"{replay_id} invariant")
        why_it_matters = _required_text(
            payload.get("why_it_matters"), name=f"{replay_id} why_it_matters"
        )
        kind = payload.get("kind")
        if failure_class not in VALID_FAILURE_CLASSES:
            raise ValueError(
                f"{replay_id} failure_class must be one of {sorted(VALID_FAILURE_CLASSES)}"
            )
        if severity not in VALID_SEVERITIES:
            raise ValueError(f"{replay_id} severity must be one of {sorted(VALID_SEVERITIES)}")
        if kind not in {"safety", "trajectory"}:
            raise ValueError(f"{replay_id} kind must be safety or trajectory")

        safety_raw = payload.get("safety")
        trajectory_raw = payload.get("trajectory")
        semantics_raw = payload.get("tool_semantics")
        policy_raw = payload.get("action_policy")
        observations_raw = payload.get("observations", [])
        expected_trajectory_categories = _string_tuple(
            payload.get("expected_trajectory_categories", []),
            name=f"{replay_id} expected_trajectory_categories",
            maximum=MAX_EXPECTED_CODES,
        )
        expected_provenance_codes = _string_tuple(
            payload.get("expected_provenance_codes", []),
            name=f"{replay_id} expected_provenance_codes",
            maximum=MAX_EXPECTED_CODES,
        )
        max_latency_ms = payload.get("max_latency_ms")
        origin_raw = payload.get("origin")

        if kind == "safety":
            if not isinstance(safety_raw, Mapping) or trajectory_raw is not None:
                raise ValueError(f"{replay_id} safety replay requires only a safety object")
        elif not isinstance(trajectory_raw, Mapping) or safety_raw is not None:
            raise ValueError(f"{replay_id} trajectory replay requires only a trajectory object")
        if semantics_raw is not None and not isinstance(semantics_raw, Mapping):
            raise ValueError(f"{replay_id} tool_semantics must be an object or null")
        if policy_raw is not None and not isinstance(policy_raw, Mapping):
            raise ValueError(f"{replay_id} action_policy must be an object or null")
        if not isinstance(observations_raw, list) or len(observations_raw) > MAX_OBSERVATIONS:
            raise ValueError(f"{replay_id} observations must contain at most {MAX_OBSERVATIONS} items")
        observations: list[ActionObservation] = []
        for item in observations_raw:
            if not isinstance(item, Mapping):
                raise ValueError(f"{replay_id} observations must be objects")
            observations.append(ActionObservation.from_dict(item))
        if max_latency_ms is not None and (
            isinstance(max_latency_ms, bool)
            or not isinstance(max_latency_ms, (int, float))
            or max_latency_ms <= 0
        ):
            raise ValueError(f"{replay_id} max_latency_ms must be a positive number or null")
        if origin_raw is not None and not isinstance(origin_raw, str):
            raise ValueError(f"{replay_id} origin must be text or null")

        return cls(
            id=replay_id,
            failure_class=cast(FailureClass, failure_class),
            severity=cast(Severity, severity),
            invariant=invariant,
            why_it_matters=why_it_matters,
            kind=cast(RegressionKind, kind),
            safety=(SafetyFixture.from_dict(safety_raw) if isinstance(safety_raw, Mapping) else None),
            trajectory=(
                AgentTrajectory.from_dict(trajectory_raw)
                if isinstance(trajectory_raw, Mapping)
                else None
            ),
            tool_semantics=(
                ToolSemanticsRegistry.from_dict(semantics_raw)
                if isinstance(semantics_raw, Mapping)
                else None
            ),
            action_policy=(
                ActionPolicySet.from_dict(policy_raw) if isinstance(policy_raw, Mapping) else None
            ),
            observations=tuple(observations),
            expected_trajectory_categories=expected_trajectory_categories,
            expected_provenance_codes=expected_provenance_codes,
            max_latency_ms=float(max_latency_ms) if max_latency_ms is not None else None,
            origin=origin_raw.strip() if isinstance(origin_raw, str) and origin_raw.strip() else None,
        )


@dataclass(frozen=True)
class RegressionReplayResult:
    replay: RegressionReplay
    actual: Decision | None
    latency_ms: float
    errors: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.errors

    @property
    def critical_false_allow(self) -> bool:
        if self.replay.severity != "critical" or self.replay.kind != "safety":
            return False
        assert self.replay.safety is not None
        return self.actual == "allow" and self.replay.safety.expected != "allow"

    def diagnostic(self) -> str:
        if self.passed:
            return f"{self.replay.id}: pass"
        return f"{self.replay.id}: " + "; ".join(self.errors)


@dataclass(frozen=True)
class RegressionReplayReport:
    results: tuple[RegressionReplayResult, ...]

    @property
    def case_count(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(result.passed for result in self.results)

    @property
    def critical_false_allows(self) -> int:
        return sum(result.critical_false_allow for result in self.results)

    def regression_errors(self) -> list[str]:
        return [result.diagnostic() for result in self.results if not result.passed]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "ordin.regression_replay_report.v1",
            "cases": self.case_count,
            "passed": self.passed,
            "critical_false_allows": self.critical_false_allows,
            "failure_class_coverage": dict(
                sorted(
                    {
                        failure_class: sum(
                            result.replay.failure_class == failure_class for result in self.results
                        )
                        for failure_class in {result.replay.failure_class for result in self.results}
                    }.items()
                )
            ),
            "errors": self.regression_errors(),
            "results": [
                {
                    "id": result.replay.id,
                    "failure_class": result.replay.failure_class,
                    "severity": result.replay.severity,
                    "passed": result.passed,
                    "actual": result.actual,
                    "latency_ms": round(result.latency_ms, 4),
                    "errors": list(result.errors),
                }
                for result in self.results
            ],
        }


def load_regression_replays(path: str | Path) -> list[RegressionReplay]:
    target = Path(path)
    replays: list[RegressionReplay] = []
    seen: set[str] = set()
    with target.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid regression JSON at {target}:{line_number}: {exc.msg}"
                ) from exc
            if not isinstance(payload, Mapping):
                raise ValueError(f"regression at {target}:{line_number} must be a JSON object")
            replay = RegressionReplay.from_dict(payload)
            if replay.id in seen:
                raise ValueError(f"duplicate regression replay id {replay.id!r}")
            seen.add(replay.id)
            replays.append(replay)
            if len(replays) > MAX_REPLAYS:
                raise ValueError(f"regression corpus must contain at most {MAX_REPLAYS} cases")
    if not replays:
        raise ValueError("regression replay corpus must contain at least one case")
    return replays


def _provenance_codes(review: Any) -> set[str]:
    provenance = getattr(review, "provenance", None)
    records = getattr(provenance, "records", ()) if provenance is not None else ()
    return {record.code for record in records if isinstance(getattr(record, "code", None), str)}


def _run_safety_replay(replay: RegressionReplay) -> RegressionReplayResult:
    assert replay.safety is not None
    ordin = Ordin(
        temporal_policy=default_temporal_policy(),
        tool_semantics=replay.tool_semantics,
        action_policy=replay.action_policy,
    )
    action = replay.safety.build_action()
    history = replay.safety.build_history()
    observation_history = (
        ObservationHistory(observations=replay.observations) if replay.observations else None
    )
    started = perf_counter_ns()
    review = ordin.review_action(action, history=history, observations=observation_history)
    latency_ms = (perf_counter_ns() - started) / 1_000_000

    errors: list[str] = []
    if review.decision != replay.safety.expected:
        errors.append(f"expected {replay.safety.expected}, got {review.decision}")
    missing_effects = set(replay.safety.expected_effects) - set(review.effects)
    if missing_effects:
        errors.append(f"missing effects {sorted(missing_effects)}")
    resources = tuple(f"{resource.type}:{resource.value}" for resource in review.resources)
    missing_prefixes = [
        prefix
        for prefix in replay.safety.expected_resource_prefixes
        if not any(resource.startswith(prefix) for resource in resources)
    ]
    if missing_prefixes:
        errors.append(f"missing resource prefixes {missing_prefixes}")
    missing_categories = set(replay.expected_trajectory_categories) - set(
        review.trajectory_categories
    )
    if missing_categories:
        errors.append(f"missing trajectory categories {sorted(missing_categories)}")
    missing_provenance = set(replay.expected_provenance_codes) - _provenance_codes(review)
    if missing_provenance:
        errors.append(f"missing provenance codes {sorted(missing_provenance)}")
    if replay.max_latency_ms is not None and latency_ms > replay.max_latency_ms:
        errors.append(
            f"latency {latency_ms:.2f}ms exceeds replay budget {replay.max_latency_ms:.2f}ms"
        )
    return RegressionReplayResult(
        replay=replay,
        actual=review.decision,
        latency_ms=latency_ms,
        errors=tuple(errors),
    )


def _run_trajectory_replay(replay: RegressionReplay) -> RegressionReplayResult:
    assert replay.trajectory is not None
    started = perf_counter_ns()
    result = run_agent_trajectory_corpus([replay.trajectory]).results[0]
    latency_ms = (perf_counter_ns() - started) / 1_000_000
    errors: list[str] = []
    if not result.matches:
        errors.append(result.diagnostic())
    if replay.max_latency_ms is not None and latency_ms > replay.max_latency_ms:
        errors.append(
            f"latency {latency_ms:.2f}ms exceeds replay budget {replay.max_latency_ms:.2f}ms"
        )
    actual = result.steps[-1].actual if result.steps else None
    return RegressionReplayResult(
        replay=replay,
        actual=actual,
        latency_ms=latency_ms,
        errors=tuple(errors),
    )


def run_regression_replays(
    replays: list[RegressionReplay], *, replay_id: str | None = None
) -> RegressionReplayReport:
    selected = [replay for replay in replays if replay_id is None or replay.id == replay_id]
    if replay_id is not None and not selected:
        raise ValueError(f"unknown regression replay id {replay_id!r}")
    results = tuple(
        _run_safety_replay(replay)
        if replay.kind == "safety"
        else _run_trajectory_replay(replay)
        for replay in selected
    )
    return RegressionReplayReport(results=results)
