from __future__ import annotations

import re
import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from . import (
    ACTION_ENVELOPE_SCHEMA_VERSION,
    ACTION_HISTORY_SCHEMA_VERSION,
    ACTION_REVIEW_SCHEMA_VERSION,
)
from .context import ExecutionContext
from ._json_contracts import reject_unknown
from .execution import (
    ActionObservation,
    ExecutionCapabilityProfile,
    ObservationHistory,
    derive_capabilities,
)
from .graph import EffectEvidence
from .policy import Decision, DecisionResultMixin, stronger_decision
from .provenance import DecisionProvenance, ProvenanceRecord, ProvenanceResource
from .review import review_command
from .risk import RiskReview, check_command, decision_for_risk, max_risk
from .semantics import semantic_evidence_for_command

if TYPE_CHECKING:
    from .tool_calls import CompiledToolSemanticsRegistry, ToolSemanticsRegistry

from .temporal import (
    CompiledTemporalPolicySet,
    TemporalActionEvidence,
    TemporalPolicySet,
    default_temporal_policy,
    temporal_evidence_for_command,
)


ACTION_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
MAX_ACTION_KIND_LENGTH = 64
MAX_OPERATION_LENGTH = 128
MAX_ACTION_ID_LENGTH = 128
MAX_ACTION_HISTORY = 32
MAX_PARAMETER_DEPTH = 8
MAX_PARAMETER_ITEMS = 128
MAX_PARAMETER_STRING_LENGTH = 32768
KNOWN_ACTION_KINDS = frozenset(("shell", "file", "network", "mcp", "database", "tool"))


JsonValue = Any


def _validate_identifier(value: str, *, name: str, maximum: int, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty text")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    if pattern.fullmatch(value) is None:
        raise ValueError(f"{name} contains unsupported characters")
    return value


def _validate_json_value(value: JsonValue, *, path: str = "parameters", depth: int = 0) -> None:
    if depth > MAX_PARAMETER_DEPTH:
        raise ValueError(f"{path} exceeds maximum nesting depth {MAX_PARAMETER_DEPTH}")
    if value is None or isinstance(value, (str, bool, int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{path} requires finite JSON numbers")
        if isinstance(value, str) and len(value) > MAX_PARAMETER_STRING_LENGTH:
            raise ValueError(
                f"{path} string must be at most {MAX_PARAMETER_STRING_LENGTH} characters"
            )
        return
    if isinstance(value, list):
        if len(value) > MAX_PARAMETER_ITEMS:
            raise ValueError(f"{path} must contain at most {MAX_PARAMETER_ITEMS} items")
        for index, item in enumerate(value):
            _validate_json_value(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_PARAMETER_ITEMS:
            raise ValueError(f"{path} must contain at most {MAX_PARAMETER_ITEMS} properties")
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path} keys must be non-empty strings")
            if len(key) > MAX_OPERATION_LENGTH:
                raise ValueError(f"{path} key must be at most {MAX_OPERATION_LENGTH} characters")
            _validate_json_value(item, path=f"{path}.{key}", depth=depth + 1)
        return
    raise ValueError(f"{path} contains a non-JSON value of type {type(value).__name__}")


def _copy_json_mapping(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    def clone(item: JsonValue) -> JsonValue:
        if isinstance(item, Mapping):
            return {str(key): clone(child) for key, child in item.items()}
        if isinstance(item, list):
            return [clone(child) for child in item]
        return item

    return {str(key): clone(item) for key, item in value.items()}


@dataclass(frozen=True)
class ActionResource:
    type: str
    value: str

    def __post_init__(self) -> None:
        _validate_identifier(
            self.type,
            name="resource type",
            maximum=MAX_ACTION_KIND_LENGTH,
            pattern=ACTION_KIND_PATTERN,
        )
        if not isinstance(self.value, str) or not self.value:
            raise ValueError("resource value must be non-empty text")
        if len(self.value) > MAX_PARAMETER_STRING_LENGTH:
            raise ValueError(
                f"resource value must be at most {MAX_PARAMETER_STRING_LENGTH} characters"
            )

    def as_dict(self) -> dict[str, str]:
        return {"type": self.type, "value": self.value}


@dataclass(frozen=True)
class ActionEnvelope:
    """Caller-owned description of an action Ordin may review but never execute."""

    kind: str
    operation: str
    parameters: Mapping[str, JsonValue] = field(default_factory=dict)
    intent: str | None = None
    context: ExecutionContext | None = None
    action_id: str | None = None

    def __post_init__(self) -> None:
        _validate_identifier(
            self.kind,
            name="action kind",
            maximum=MAX_ACTION_KIND_LENGTH,
            pattern=ACTION_KIND_PATTERN,
        )
        _validate_identifier(
            self.operation,
            name="action operation",
            maximum=MAX_OPERATION_LENGTH,
            pattern=ACTION_KIND_PATTERN,
        )
        if self.action_id is not None:
            if not isinstance(self.action_id, str) or not self.action_id.strip():
                raise ValueError("action_id must be non-empty text or null")
            if len(self.action_id) > MAX_ACTION_ID_LENGTH:
                raise ValueError(f"action_id must be at most {MAX_ACTION_ID_LENGTH} characters")
        if self.intent is not None and not isinstance(self.intent, str):
            raise ValueError("action intent must be a string or null")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("action parameters must be a JSON object")
        if self.context is not None and not isinstance(self.context, ExecutionContext):
            raise ValueError("action context must be ExecutionContext or null")
        _validate_json_value(self.parameters)
        object.__setattr__(self, "parameters", _copy_json_mapping(self.parameters))

    @classmethod
    def shell(
        cls,
        command: str,
        *,
        intent: str | None = None,
        context: ExecutionContext | None = None,
        action_id: str | None = None,
    ) -> "ActionEnvelope":
        if not isinstance(command, str) or not command.strip():
            raise ValueError("shell action requires non-empty command text")
        return cls(
            kind="shell",
            operation="execute",
            parameters={"command": command},
            intent=intent,
            context=context,
            action_id=action_id,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ACTION_ENVELOPE_SCHEMA_VERSION,
            "action_id": self.action_id,
            "kind": self.kind,
            "operation": self.operation,
            "parameters": _copy_json_mapping(self.parameters),
            "intent": self.intent,
            "context": self.context.as_dict() if self.context else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActionEnvelope":
        if not isinstance(payload, Mapping):
            raise ValueError("action envelope must be a JSON object")
        reject_unknown(
            payload,
            {"schema_version", "action_id", "kind", "operation", "parameters", "intent", "context"},
            "action envelope",
        )
        schema_version = payload.get("schema_version")
        if schema_version is not None and schema_version != ACTION_ENVELOPE_SCHEMA_VERSION:
            raise ValueError(f"unsupported action envelope schema: {schema_version!r}")
        kind = payload.get("kind")
        operation = payload.get("operation")
        parameters = payload.get("parameters", {})
        intent = payload.get("intent")
        action_id = payload.get("action_id")
        if not isinstance(kind, str):
            raise ValueError("action envelope requires string kind")
        if not isinstance(operation, str):
            raise ValueError("action envelope requires string operation")
        if not isinstance(parameters, Mapping):
            raise ValueError("action envelope parameters must be a JSON object")
        if intent is not None and not isinstance(intent, str):
            raise ValueError("action envelope intent must be a string or null")
        if action_id is not None and not isinstance(action_id, str):
            raise ValueError("action envelope action_id must be a string or null")
        return cls(
            kind=kind,
            operation=operation,
            parameters=parameters,
            intent=intent,
            context=ExecutionContext.from_dict(payload.get("context")),
            action_id=action_id,
        )


@dataclass(frozen=True)
class ActionHistory:
    """Bounded caller-supplied history of prior generic actions."""

    actions: tuple[ActionEnvelope, ...] = ()

    def __post_init__(self) -> None:
        if len(self.actions) > MAX_ACTION_HISTORY:
            raise ValueError(f"action history maximum is {MAX_ACTION_HISTORY}")
        if any(not isinstance(action, ActionEnvelope) for action in self.actions):
            raise ValueError("action history entries must be ActionEnvelope values")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ACTION_HISTORY_SCHEMA_VERSION,
            "actions": [action.as_dict() for action in self.actions],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActionHistory":
        if not isinstance(payload, Mapping):
            raise ValueError("action history must be a JSON object")
        reject_unknown(payload, {"schema_version", "actions"}, "action history")
        schema_version = payload.get("schema_version")
        if schema_version != ACTION_HISTORY_SCHEMA_VERSION:
            raise ValueError(f"unsupported action history schema: {schema_version!r}")
        actions_raw = payload.get("actions")
        if not isinstance(actions_raw, list):
            raise ValueError("action history actions must be an array")
        if len(actions_raw) > MAX_ACTION_HISTORY:
            raise ValueError(f"action history maximum is {MAX_ACTION_HISTORY}")
        actions: list[ActionEnvelope] = []
        for item in actions_raw:
            if not isinstance(item, Mapping):
                raise ValueError("action history entries must be JSON objects")
            actions.append(ActionEnvelope.from_dict(item))
        return cls(actions=tuple(actions))


@dataclass(frozen=True)
class ActionReview(DecisionResultMixin):
    action: ActionEnvelope
    decision: Decision
    risk: str
    reasons: list[str]
    safer_next_step: str | None
    effects: list[str]
    resources: list[ActionResource]
    adapter: str | None
    capabilities: ExecutionCapabilityProfile | None = None
    provenance: DecisionProvenance | None = None
    intent_alignment: str = "not_applicable"
    trajectory_categories: list[str] = field(default_factory=list)
    policy: dict[str, str] | None = None
    policy_matches: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": ACTION_REVIEW_SCHEMA_VERSION,
            "action": self.action.as_dict(),
            "decision": self.decision,
            "risk": self.risk,
            "reasons": list(self.reasons),
            "safer_next_step": self.safer_next_step,
            "effects": list(self.effects),
            "resources": [resource.as_dict() for resource in self.resources],
            "adapter": self.adapter,
            "capabilities": self.capabilities.as_dict() if self.capabilities else None,
            "provenance": self.provenance.as_dict() if self.provenance else None,
            "intent_alignment": self.intent_alignment,
            "trajectory_categories": list(self.trajectory_categories),
        }
        if self.policy is not None:
            payload["policy"] = dict(self.policy)
        if self.policy_matches:
            payload["policy_matches"] = [dict(item) for item in self.policy_matches]
        return payload


def _resources_from_semantics(
    command: str, context: ExecutionContext | None
) -> list[ActionResource]:
    resolved = semantic_evidence_for_command(command, context=context)
    seen: set[tuple[str, str]] = set()
    resources: list[ActionResource] = []
    for item in resolved.evidence:
        if not item.resource:
            continue
        resource_type, separator, value = item.resource.partition(":")
        if not separator or not resource_type or not value:
            resource_type, value = "unknown", item.resource
        resource_type = resource_type.lower().replace("_", "-")
        key = (resource_type, value)
        if key in seen:
            continue
        seen.add(key)
        resources.append(ActionResource(type=resource_type, value=value))
    return resources


CONTEXT_PROVENANCE_CATEGORIES = frozenset(
    ("root_filesystem_mutation", "outside_repo_mutation", "elevated_context")
)


def _provenance_resource(raw: str | None) -> ProvenanceResource | None:
    if not raw:
        return None
    resource_type, separator, value = raw.partition(":")
    if not separator or not resource_type or not value:
        return ProvenanceResource(type="unknown", value=raw)
    resource_type = resource_type.lower().replace("_", "-")
    if ACTION_KIND_PATTERN.fullmatch(resource_type) is None:
        resource_type = "unknown"
        value = raw
    return ProvenanceResource(type=resource_type, value=value)


def _base_provenance(
    review: ActionReview,
    *,
    semantic_evidence: Sequence[EffectEvidence] = (),
    risk_review: RiskReview | None = None,
) -> DecisionProvenance:
    adapter_rule_id = (
        review.adapter.removeprefix("tool-semantics:")
        if review.adapter and review.adapter.startswith("tool-semantics:")
        else None
    )
    records: list[ProvenanceRecord] = [
        ProvenanceRecord(
            source="adapter",
            kind="rule" if adapter_rule_id else "finding",
            code="adapter.classification",
            summary=(
                f'classified by adapter "{review.adapter}"'
                if review.adapter
                else "no deterministic semantic adapter classified the action"
            ),
            decision=review.decision,
            risk=review.risk,
            rule_id=adapter_rule_id,
            action_id=review.action.action_id,
            metadata={"adapter": review.adapter or "none"},
        )
    ]

    represented_effects: set[str] = set()
    represented_resources: set[tuple[str, str]] = set()
    for item in semantic_evidence:
        provenance_resource = _provenance_resource(item.resource)
        represented_effects.add(item.effect)
        if provenance_resource is not None:
            represented_resources.add((provenance_resource.type, provenance_resource.value))
        records.append(
            ProvenanceRecord(
                source="semantic",
                kind="effect",
                code="semantic.effect",
                summary=item.reason,
                risk=item.risk,
                effect=item.effect,
                resource=provenance_resource,
                category=item.category,
                metadata={"semantic_source": item.source},
            )
        )

    for effect in review.effects:
        if effect not in represented_effects:
            records.append(
                ProvenanceRecord(
                    source="semantic",
                    kind="effect",
                    code="semantic.effect",
                    effect=effect,
                    metadata={"semantic_source": review.adapter or "action-review"},
                )
            )
    for action_resource in review.resources:
        key = (action_resource.type, action_resource.value)
        if key not in represented_resources:
            records.append(
                ProvenanceRecord(
                    source="semantic",
                    kind="resource",
                    code="semantic.resource",
                    resource=ProvenanceResource(
                        type=action_resource.type,
                        value=action_resource.value,
                    ),
                    metadata={"semantic_source": review.adapter or "action-review"},
                )
            )

    if risk_review is not None:
        for rule_id in risk_review.matched_rules or []:
            records.append(
                ProvenanceRecord(
                    source="risk_rule",
                    kind="rule",
                    code="risk-rule.match",
                    rule_id=rule_id,
                )
            )
        for category in risk_review.risk_categories or []:
            if category in CONTEXT_PROVENANCE_CATEGORIES:
                records.append(
                    ProvenanceRecord(
                        source="context",
                        kind="finding",
                        code=f"context.{category}",
                        category=category,
                    )
                )

    if review.intent_alignment == "mismatch":
        records.append(
            ProvenanceRecord(
                source="intent",
                kind="finding",
                code="intent.mismatch",
                summary="the proposed action does not align with the supplied intent",
                decision="warn",
                risk="medium",
            )
        )

    records.append(
        ProvenanceRecord(
            source="decision",
            kind="merge",
            code="decision.base",
            decision=review.decision,
            risk=review.risk,
        )
    )
    return DecisionProvenance(
        records=tuple(records),
        final_decision=review.decision,
        final_risk=review.risk,
    )


def _with_base_provenance(
    review: ActionReview,
    *,
    semantic_evidence: Sequence[EffectEvidence] = (),
    risk_review: RiskReview | None = None,
) -> ActionReview:
    if review.provenance is not None:
        return review
    return replace(
        review,
        provenance=_base_provenance(
            review,
            semantic_evidence=semantic_evidence,
            risk_review=risk_review,
        ),
    )


def _with_capabilities(review: ActionReview) -> ActionReview:
    updated = review
    if updated.capabilities is None:
        updated = replace(
            updated,
            capabilities=derive_capabilities(
                updated.action.kind, updated.effects, updated.resources
            ),
        )
    return _with_base_provenance(updated)


def _review_action_base(
    action: ActionEnvelope,
    *,
    tool_semantics: "ToolSemanticsRegistry | CompiledToolSemanticsRegistry | None" = None,
) -> ActionReview:
    """Review one action without applying temporal history."""

    if action.kind in {"tool", "mcp"} and action.operation == "call" and tool_semantics is not None:
        from .tool_calls import review_tool_action

        return _with_capabilities(review_tool_action(action, tool_semantics))

    if action.kind == "shell" and action.operation == "execute":
        command = action.parameters.get("command")
        if not isinstance(command, str) or not command.strip():
            return _with_base_provenance(
                ActionReview(
                    action=action,
                    decision="block",
                    risk="critical",
                    reasons=["shell.execute action requires non-empty parameters.command"],
                    safer_next_step="Provide the exact command text before review.",
                    effects=[],
                    resources=[],
                    adapter="shell",
                    capabilities=derive_capabilities("shell", (), ()),
                )
            )
        risk_review = check_command(command, context=action.context)
        command_review = review_command(
            command,
            intent=action.intent,
            context=action.context,
            _risk_review=risk_review,
        )
        semantics = semantic_evidence_for_command(command, context=action.context)
        effects = list(semantics.effects)
        resources = _resources_from_semantics(command, action.context)
        return _with_base_provenance(
            ActionReview(
                action=action,
                decision=command_review.decision,
                risk=command_review.risk,
                reasons=list(command_review.reasons),
                safer_next_step=command_review.safer_next_step,
                effects=effects,
                resources=resources,
                adapter="shell",
                capabilities=derive_capabilities(action.kind, effects, resources),
                intent_alignment=command_review.intent_alignment,
                trajectory_categories=list(command_review.trajectory_categories or []),
            ),
            semantic_evidence=semantics.evidence,
            risk_review=risk_review,
        )

    known_kind = action.kind in KNOWN_ACTION_KINDS
    scope = f"{action.kind}.{action.operation}"
    if known_kind:
        reason = f'no semantic adapter is registered for action "{scope}"'
    else:
        reason = f'action kind "{action.kind}" is not classified by Ordin'
    return _with_base_provenance(
        ActionReview(
            action=action,
            decision="ask",
            risk="unknown",
            reasons=[reason],
            safer_next_step=(
                "Require explicit review or add a deterministic semantic adapter before execution."
            ),
            effects=[],
            resources=[],
            adapter=None,
            capabilities=derive_capabilities(action.kind, (), ()),
        )
    )


def _temporal_evidence_for_action(
    action: ActionEnvelope,
    review: ActionReview | None = None,
    *,
    observation: ActionObservation | None = None,
    tool_semantics: "ToolSemanticsRegistry | CompiledToolSemanticsRegistry | None" = None,
) -> TemporalActionEvidence:
    if action.kind == "shell" and action.operation == "execute":
        command = action.parameters.get("command")
        if isinstance(command, str) and command.strip():
            evidence = temporal_evidence_for_command(command, context=action.context)
            signals = set(evidence.signals)
        else:
            signals = set()
    else:
        base = review or _review_action_base(action, tool_semantics=tool_semantics)
        signals = {f"effect:{effect}" for effect in base.effects}

    predicted_effects = tuple(
        signal.removeprefix("effect:") for signal in signals if signal.startswith("effect:")
    )
    signals.update(f"signal:predicted-effect:{effect}" for effect in predicted_effects)

    if observation is not None:
        for effect in observation.effects:
            signals.add(f"effect:{effect}")
            signals.add(f"signal:observed-effect:{effect}")
        if observation.exit_code is not None:
            signals.add(
                "signal:observed-success"
                if observation.exit_code == 0
                else "signal:observed-failure"
            )

    return TemporalActionEvidence(
        kind=action.kind,
        operation=action.operation,
        signals=frozenset(signals),
    )


def _observations_for_history(
    history: ActionHistory | None,
    observations: ObservationHistory | None,
) -> dict[str, ActionObservation]:
    if observations is None or not observations.observations:
        return {}
    if history is None or not history.actions:
        raise ValueError("observations require matching action history")

    counts: dict[str, int] = {}
    for prior in history.actions:
        if prior.action_id is not None:
            counts[prior.action_id] = counts.get(prior.action_id, 0) + 1

    result = observations.by_action_id()
    unknown = sorted(set(result) - set(counts))
    if unknown:
        raise ValueError("observations reference unknown action_id values: " + ", ".join(unknown))
    ambiguous = sorted(action_id for action_id in result if counts[action_id] != 1)
    if ambiguous:
        raise ValueError(
            "observations require unique matching action_id values: " + ", ".join(ambiguous)
        )
    return result


def _with_observation_provenance(
    review: ActionReview,
    history: ActionHistory | None,
    observation_map: Mapping[str, ActionObservation],
) -> ActionReview:
    if not observation_map or history is None:
        return review
    provenance = review.provenance or _base_provenance(review)
    records: list[ProvenanceRecord] = []
    for index, prior in enumerate(history.actions):
        if prior.action_id is None:
            continue
        observation = observation_map.get(prior.action_id)
        if observation is None:
            continue
        records.append(
            ProvenanceRecord(
                source="observation",
                kind="observation",
                code="observation.record",
                action_id=observation.action_id,
                metadata={
                    "history_index": index,
                    "exit_code": observation.exit_code,
                },
            )
        )
        for effect in observation.effects:
            records.append(
                ProvenanceRecord(
                    source="observation",
                    kind="effect",
                    code="observation.effect",
                    effect=effect,
                    action_id=observation.action_id,
                    metadata={"history_index": index},
                )
            )
        for resource in observation.resources:
            records.append(
                ProvenanceRecord(
                    source="observation",
                    kind="resource",
                    code="observation.resource",
                    resource=ProvenanceResource(type=resource.type, value=resource.value),
                    action_id=observation.action_id,
                    metadata={"history_index": index},
                )
            )
    if not records:
        return review
    return replace(review, provenance=provenance.append(*records))


def review_action(
    action: ActionEnvelope,
    *,
    history: ActionHistory | None = None,
    observations: ObservationHistory | None = None,
    temporal_policy: TemporalPolicySet | CompiledTemporalPolicySet | None = None,
    tool_semantics: "ToolSemanticsRegistry | CompiledToolSemanticsRegistry | None" = None,
) -> ActionReview:
    """Review an action and optionally apply bounded temporal history."""

    base = _review_action_base(action, tool_semantics=tool_semantics)
    observation_map = _observations_for_history(history, observations)
    base = _with_observation_provenance(base, history, observation_map)
    if history is None or not history.actions:
        return base

    if temporal_policy is None:
        compiled = default_temporal_policy()
    elif isinstance(temporal_policy, TemporalPolicySet):
        compiled = temporal_policy.compile()
    elif isinstance(temporal_policy, CompiledTemporalPolicySet):
        compiled = temporal_policy
    else:
        raise ValueError("temporal_policy must be a temporal policy set, compiled policy, or null")

    prior = tuple(
        _temporal_evidence_for_action(
            item,
            observation=observation_map.get(item.action_id) if item.action_id else None,
            tool_semantics=tool_semantics,
        )
        for item in history.actions
    )
    current = _temporal_evidence_for_action(
        action,
        review=base,
        tool_semantics=tool_semantics,
    )
    evaluation = compiled.evaluate(prior, current)
    provenance = base.provenance or _base_provenance(base)
    temporal_metadata = {
        "policy_id": compiled.policy.policy_id,
        "policy_version": compiled.policy.version,
        "policy_digest": compiled.policy.digest,
    }
    if not evaluation.matches:
        return replace(
            base,
            provenance=provenance.append(
                ProvenanceRecord(
                    source="temporal_policy",
                    kind="finding",
                    code="temporal.no-match",
                    metadata=temporal_metadata,
                )
            ),
        )

    review_risk = base.risk
    decision = base.decision
    reasons = list(base.reasons)
    safer_next_step = base.safer_next_step
    prior_risk = review_risk
    if evaluation.risk is not None:
        review_risk = max_risk(review_risk, evaluation.risk)
        decision = stronger_decision(decision, decision_for_risk(evaluation.risk))
    for match in evaluation.matches:
        if match.reason not in reasons:
            reasons.append(match.reason)
    if review_risk != prior_risk:
        first_step = next(
            (match.safer_next_step for match in evaluation.matches if match.safer_next_step),
            None,
        )
        if first_step:
            safer_next_step = first_step

    categories = sorted(
        set(base.trajectory_categories).union(match.category for match in evaluation.matches)
    )
    temporal_records = [
        ProvenanceRecord(
            source="temporal_policy",
            kind="rule",
            code="temporal.match",
            summary=match.reason,
            decision=decision_for_risk(match.risk),
            risk=match.risk,
            rule_id=match.rule_id,
            category=match.category,
            matched_indices=match.matched_indices,
            metadata=temporal_metadata,
        )
        for match in evaluation.matches
    ]
    temporal_records.append(
        ProvenanceRecord(
            source="decision",
            kind="merge",
            code="decision.temporal-merge",
            decision=decision,
            risk=review_risk,
            metadata={
                "previous_decision": base.decision,
                "previous_risk": base.risk,
                **temporal_metadata,
            },
        )
    )
    return replace(
        base,
        decision=decision,
        risk=review_risk,
        reasons=reasons,
        safer_next_step=safer_next_step,
        trajectory_categories=categories,
        provenance=provenance.append(
            *temporal_records,
            final_decision=decision,
            final_risk=review_risk,
        ),
    )
