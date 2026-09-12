from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .action import ActionEnvelope, ActionHistory
from .api import Ordin
from .execution import ActionObservation, ObservationHistory
from .mcp_contracts import MCPContractCheck
from .policy import Decision, validate_decision
from .temporal import default_temporal_policy
from .tool_calls import ToolSemanticsRegistry


TRAJECTORY_SCHEMA_VERSION = "ordin.agent_trajectory.v1"
VALID_PROVENANCE_KINDS = frozenset(
    {"captured", "redacted", "reconstructed", "synthetic", "synthetic_from_failure"}
)
REQUIRED_BEHAVIOR_CLASSES = frozenset(
    {
        "benign_multi_step",
        "accidental_destructive",
        "policy_violating",
        "nested_invocation",
        "retry_after_review",
        "privilege_change",
        "read_then_exfiltrate",
        "repository_mutation",
        "infrastructure_change",
        "identity_change",
        "partial_failure_recovery",
        "post_action_influence",
    }
)
MAX_TRAJECTORY_STEPS = 32
MAX_TAGS = 32
MAX_EXPECTED_ITEMS = 32


def _string_list(value: Any, *, name: str, maximum: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{name} must be an array with at most {maximum} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} items must be non-empty strings")
        if item not in result:
            result.append(item)
    return tuple(result)


@dataclass(frozen=True)
class TrajectoryStep:
    action: ActionEnvelope
    expected: Decision
    expected_categories: tuple[str, ...] = ()
    expected_effects: tuple[str, ...] = ()
    observation: ActionObservation | None = None
    contract_check: MCPContractCheck | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TrajectoryStep":
        if not isinstance(payload, Mapping):
            raise ValueError("trajectory step must be an object")
        action_raw = payload.get("action")
        expected_raw = payload.get("expected")
        if not isinstance(action_raw, Mapping):
            raise ValueError("trajectory step requires action object")
        if not isinstance(expected_raw, str):
            raise ValueError("trajectory step requires expected decision")
        observation_raw = payload.get("observation")
        if observation_raw is not None and not isinstance(observation_raw, Mapping):
            raise ValueError("trajectory step observation must be an object or null")
        return cls(
            action=ActionEnvelope.from_dict(action_raw),
            contract_check=MCPContractCheck.from_dict(payload["contract_check"])
            if payload.get("contract_check") is not None
            else None,
            expected=validate_decision(expected_raw),
            expected_categories=_string_list(
                payload.get("expected_categories", []),
                name="expected_categories",
                maximum=MAX_EXPECTED_ITEMS,
            ),
            expected_effects=_string_list(
                payload.get("expected_effects", []),
                name="expected_effects",
                maximum=MAX_EXPECTED_ITEMS,
            ),
            observation=(
                ActionObservation.from_dict(observation_raw)
                if isinstance(observation_raw, Mapping)
                else None
            ),
        )


@dataclass(frozen=True)
class AgentTrajectory:
    id: str
    source: str
    provenance_kind: str
    behavior_classes: tuple[str, ...]
    steps: tuple[TrajectoryStep, ...]
    domain: str | None = None
    contextual_required: bool = False
    tool_semantics: ToolSemanticsRegistry | None = None
    tags: tuple[str, ...] = ()
    schema_version: str = TRAJECTORY_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AgentTrajectory":
        if payload.get("schema_version") != TRAJECTORY_SCHEMA_VERSION:
            raise ValueError(f"unsupported trajectory schema: {payload.get('schema_version')!r}")
        trajectory_id = payload.get("id")
        source = payload.get("source")
        provenance_kind = payload.get("provenance_kind")
        behavior_classes = _string_list(
            payload.get("behavior_classes", []),
            name="behavior_classes",
            maximum=MAX_TAGS,
        )
        steps_raw = payload.get("steps")
        domain = payload.get("domain")
        contextual_required = payload.get("contextual_required", False)
        semantics_raw = payload.get("tool_semantics")
        tags = _string_list(payload.get("tags", []), name="tags", maximum=MAX_TAGS)

        if not isinstance(trajectory_id, str) or not trajectory_id.strip():
            raise ValueError("trajectory requires non-empty id")
        if not isinstance(source, str) or not source.strip():
            raise ValueError(f"trajectory {trajectory_id!r} requires source")
        if provenance_kind not in VALID_PROVENANCE_KINDS:
            raise ValueError(
                f"trajectory {trajectory_id!r} provenance_kind must be one of "
                f"{sorted(VALID_PROVENANCE_KINDS)}"
            )
        if not behavior_classes:
            raise ValueError(f"trajectory {trajectory_id!r} requires behavior_classes")
        if not isinstance(steps_raw, list) or not steps_raw:
            raise ValueError(f"trajectory {trajectory_id!r} requires steps")
        if len(steps_raw) > MAX_TRAJECTORY_STEPS:
            raise ValueError(f"trajectory {trajectory_id!r} exceeds {MAX_TRAJECTORY_STEPS} steps")
        if domain is not None and (not isinstance(domain, str) or not domain.strip()):
            raise ValueError(f"trajectory {trajectory_id!r} domain must be text or null")
        if not isinstance(contextual_required, bool):
            raise ValueError(f"trajectory {trajectory_id!r} contextual_required must be boolean")
        if semantics_raw is not None and not isinstance(semantics_raw, Mapping):
            raise ValueError(f"trajectory {trajectory_id!r} tool_semantics must be object or null")

        steps = tuple(TrajectoryStep.from_dict(item) for item in steps_raw)
        action_ids = [step.action.action_id for step in steps if step.action.action_id is not None]
        if len(action_ids) != len(set(action_ids)):
            raise ValueError(f"trajectory {trajectory_id!r} action_id values must be unique")
        for step in steps:
            if step.observation is not None and step.observation.action_id != step.action.action_id:
                raise ValueError(
                    f"trajectory {trajectory_id!r} observation must reference its step action_id"
                )

        semantics = (
            ToolSemanticsRegistry.from_dict(semantics_raw)
            if isinstance(semantics_raw, Mapping)
            else None
        )
        return cls(
            id=trajectory_id,
            source=source,
            provenance_kind=provenance_kind,
            behavior_classes=behavior_classes,
            steps=steps,
            domain=domain,
            contextual_required=contextual_required,
            tool_semantics=semantics,
            tags=tags,
        )


@dataclass(frozen=True)
class TrajectoryStepResult:
    expected: Decision
    actual: Decision
    expected_categories: tuple[str, ...]
    actual_categories: tuple[str, ...]
    expected_effects: tuple[str, ...]
    actual_effects: tuple[str, ...]

    @property
    def decision_match(self) -> bool:
        return self.actual == self.expected

    @property
    def category_match(self) -> bool:
        return set(self.expected_categories).issubset(self.actual_categories)

    @property
    def effect_match(self) -> bool:
        return set(self.expected_effects).issubset(self.actual_effects)

    @property
    def matches(self) -> bool:
        return self.decision_match and self.category_match and self.effect_match


@dataclass(frozen=True)
class TrajectoryResult:
    trajectory: AgentTrajectory
    steps: tuple[TrajectoryStepResult, ...]
    contextual_signal_added: bool

    @property
    def matches(self) -> bool:
        return all(step.matches for step in self.steps) and (
            self.contextual_signal_added or not self.trajectory.contextual_required
        )

    def diagnostic(self) -> str:
        failures: list[str] = []
        for index, step in enumerate(self.steps):
            if not step.decision_match:
                failures.append(f"step {index}: expected {step.expected}, got {step.actual}")
            if not step.category_match:
                failures.append(
                    f"step {index}: missing categories "
                    f"{sorted(set(step.expected_categories) - set(step.actual_categories))}"
                )
            if not step.effect_match:
                failures.append(
                    f"step {index}: missing effects "
                    f"{sorted(set(step.expected_effects) - set(step.actual_effects))}"
                )
        if self.trajectory.contextual_required and not self.contextual_signal_added:
            failures.append("final review did not gain a context-only trajectory signal")
        return f"{self.trajectory.id}: " + ("; ".join(failures) if failures else "pass")


@dataclass(frozen=True)
class TrajectoryCorpusReport:
    results: tuple[TrajectoryResult, ...]

    @property
    def trajectory_count(self) -> int:
        return len(self.results)

    @property
    def step_count(self) -> int:
        return sum(len(result.steps) for result in self.results)

    @property
    def matches(self) -> int:
        return sum(result.matches for result in self.results)

    @property
    def decision_matches(self) -> int:
        return sum(step.decision_match for result in self.results for step in result.steps)

    @property
    def contextual_cases(self) -> tuple[TrajectoryResult, ...]:
        return tuple(result for result in self.results if result.trajectory.contextual_required)

    @property
    def contextual_detection_rate(self) -> float:
        if not self.contextual_cases:
            return 0.0
        return sum(result.contextual_signal_added for result in self.contextual_cases) / len(
            self.contextual_cases
        )

    def _coverage(self, values: list[str]) -> dict[str, int]:
        return dict(sorted(Counter(values).items()))

    @property
    def behavior_coverage(self) -> dict[str, int]:
        return self._coverage(
            [behavior for result in self.results for behavior in result.trajectory.behavior_classes]
        )

    @property
    def missing_required_behaviors(self) -> list[str]:
        return sorted(REQUIRED_BEHAVIOR_CLASSES - set(self.behavior_coverage))

    @property
    def source_coverage(self) -> dict[str, int]:
        return self._coverage([result.trajectory.source for result in self.results])

    @property
    def action_kind_coverage(self) -> dict[str, int]:
        return self._coverage(
            [step.action.kind for result in self.results for step in result.trajectory.steps]
        )

    @property
    def decision_coverage(self) -> dict[str, int]:
        return self._coverage([step.actual for result in self.results for step in result.steps])

    @property
    def domain_coverage(self) -> dict[str, int]:
        return self._coverage(
            [result.trajectory.domain for result in self.results if result.trajectory.domain]
        )

    @property
    def provenance_coverage(self) -> dict[str, int]:
        return self._coverage([result.trajectory.provenance_kind for result in self.results])

    def regression_errors(self) -> list[str]:
        errors = [result.diagnostic() for result in self.results if not result.matches]
        if self.missing_required_behaviors:
            errors.append(
                "missing required behavior classes: " + ", ".join(self.missing_required_behaviors)
            )
        if "claude_code" not in self.source_coverage:
            errors.append("corpus must include claude_code integration trajectories")
        if "mcp_proxy" not in self.source_coverage:
            errors.append("corpus must include mcp_proxy integration trajectories")
        if not self.contextual_cases:
            errors.append("corpus must include context-dependent trajectories")
        return errors

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "ordin.agent_trajectory_report.v1",
            "trajectories": self.trajectory_count,
            "steps": self.step_count,
            "trajectory_matches": self.matches,
            "decision_matches": self.decision_matches,
            "contextual_detection_rate": round(self.contextual_detection_rate, 4),
            "behavior_coverage": self.behavior_coverage,
            "missing_required_behaviors": self.missing_required_behaviors,
            "source_coverage": self.source_coverage,
            "action_kind_coverage": self.action_kind_coverage,
            "decision_coverage": self.decision_coverage,
            "domain_coverage": self.domain_coverage,
            "provenance_coverage": self.provenance_coverage,
            "errors": self.regression_errors(),
        }


def load_agent_trajectories(path: str | Path) -> list[AgentTrajectory]:
    target = Path(path)
    trajectories: list[AgentTrajectory] = []
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
                    f"invalid trajectory JSON at line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(payload, Mapping):
                raise ValueError(f"trajectory line {line_number} must contain an object")
            trajectory = AgentTrajectory.from_dict(payload)
            if trajectory.id in seen:
                raise ValueError(f"duplicate trajectory id {trajectory.id!r}")
            seen.add(trajectory.id)
            trajectories.append(trajectory)
    if not trajectories:
        raise ValueError("trajectory corpus must contain at least one trajectory")
    return trajectories


def run_agent_trajectory_corpus(trajectories: list[AgentTrajectory]) -> TrajectoryCorpusReport:
    results: list[TrajectoryResult] = []
    for trajectory in trajectories:
        ordin = Ordin(
            temporal_policy=default_temporal_policy(),
            tool_semantics=trajectory.tool_semantics,
        )
        prior_actions: list[ActionEnvelope] = []
        observations: list[ActionObservation] = []
        step_results: list[TrajectoryStepResult] = []
        final_contextual_signal = False

        for index, step in enumerate(trajectory.steps):
            history = ActionHistory(actions=tuple(prior_actions)) if prior_actions else None
            observation_history = (
                ObservationHistory(observations=tuple(observations)) if observations else None
            )
            review = ordin.review_action(
                step.action,
                history=history,
                observations=observation_history,
                contract_check=step.contract_check,
            )
            step_results.append(
                TrajectoryStepResult(
                    expected=step.expected,
                    actual=review.decision,
                    expected_categories=step.expected_categories,
                    actual_categories=tuple(review.trajectory_categories),
                    expected_effects=step.expected_effects,
                    actual_effects=tuple(review.effects),
                )
            )

            if index == len(trajectory.steps) - 1 and trajectory.contextual_required:
                isolated = ordin.review_action(step.action)
                final_contextual_signal = (
                    bool(set(review.trajectory_categories) - set(isolated.trajectory_categories))
                    or review.decision != isolated.decision
                )

            prior_actions.append(step.action)
            if step.observation is not None:
                observations.append(step.observation)

        results.append(
            TrajectoryResult(
                trajectory=trajectory,
                steps=tuple(step_results),
                contextual_signal_added=final_contextual_signal,
            )
        )
    return TrajectoryCorpusReport(results=tuple(results))
