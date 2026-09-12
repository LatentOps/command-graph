from __future__ import annotations

from ._json_contracts import load_configuration

import hashlib
import json
import posixpath
import re
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

from .action import ActionResource, ActionReview
from .policy import Decision, ENFORCEMENT_ORDER, validate_decision
from .provenance import ProvenanceRecord


POLICY_SCHEMA_VERSION = "ordin.policy_set.v1"
MAX_POLICY_FILE_BYTES = 1_048_576
MAX_POLICY_RULES = 256
MAX_POLICY_ID_LENGTH = 128
MAX_POLICY_VERSION_LENGTH = 64
MAX_RULE_ID_LENGTH = 128
MAX_MATCH_VALUES = 128
MAX_MATCH_VALUE_LENGTH = 32768
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:@/-]*$")
RepoScope = Literal["inside", "outside", "unknown"]
IntentState = Literal["present", "absent", "aligned", "mismatch", "not_applicable"]
ResourceMatchMode = Literal["exact", "prefix"]


@dataclass(frozen=True)
class PolicyResourceMatcher:
    type: str
    value: str | None = None
    match: ResourceMatchMode = "exact"

    def __post_init__(self) -> None:
        _validate_text(self.type, "resource matcher type", MAX_MATCH_VALUE_LENGTH)
        if self.value is not None:
            _validate_text(self.value, "resource matcher value", MAX_MATCH_VALUE_LENGTH)
        if self.match not in {"exact", "prefix"}:
            raise ValueError(f"unsupported resource match mode: {self.match!r}")
        if self.value is None and self.match != "exact":
            raise ValueError("resource matcher without a value must use exact mode")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "PolicyResourceMatcher":
        _reject_unknown_keys(payload, {"type", "value", "match"}, "resource matcher")
        resource_type = payload.get("type")
        value = payload.get("value")
        match = payload.get("match", "exact")
        if not isinstance(resource_type, str):
            raise ValueError("resource matcher requires string type")
        if value is not None and not isinstance(value, str):
            raise ValueError("resource matcher value must be a string or null")
        if not isinstance(match, str):
            raise ValueError("resource matcher match must be a string")
        return cls(type=resource_type, value=value, match=cast(ResourceMatchMode, match))

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type}
        if self.value is not None:
            payload["value"] = self.value
        if self.match != "exact":
            payload["match"] = self.match
        return payload

    def matches(self, resource: ActionResource) -> bool:
        if resource.type != self.type:
            return False
        if self.value is None:
            return True
        if self.match == "exact":
            return resource.value == self.value
        return resource.value.startswith(self.value)


@dataclass(frozen=True)
class ActionPolicyCondition:
    kinds: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()
    effects_any: tuple[str, ...] = ()
    effects_all: tuple[str, ...] = ()
    resources_any: tuple[PolicyResourceMatcher, ...] = ()
    risks: tuple[str, ...] = ()
    decisions: tuple[Decision, ...] = ()
    agents: tuple[str, ...] = ()
    cwd_prefixes: tuple[str, ...] = ()
    repo_scope: RepoScope | None = None
    privileged: bool | None = None
    intent: IntentState | None = None
    trajectory_any: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for name in (
            "kinds",
            "operations",
            "effects_any",
            "effects_all",
            "risks",
            "decisions",
            "agents",
            "cwd_prefixes",
            "trajectory_any",
        ):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)):
                raise ValueError(f"policy condition {name} must be a sequence")
            _string_list({name: list(values)}, name)
        if len(self.resources_any) > MAX_MATCH_VALUES or any(
            not isinstance(resource, PolicyResourceMatcher) for resource in self.resources_any
        ):
            raise ValueError("policy condition resources must be bounded resource matchers")
        for decision in self.decisions:
            validate_decision(decision)
        if any(risk not in {"unknown", "low", "medium", "high", "critical"} for risk in self.risks):
            raise ValueError("invalid policy condition risk")
        if self.repo_scope not in (None, "inside", "outside", "unknown"):
            raise ValueError("invalid policy repository scope")
        if self.privileged is not None and not isinstance(self.privileged, bool):
            raise ValueError("policy privilege selector must be boolean or null")
        if self.intent not in (None, "present", "absent", "aligned", "mismatch", "not_applicable"):
            raise ValueError("invalid policy intent selector")
        for prefix in self.cwd_prefixes:
            _validate_text(prefix, "policy cwd prefix", MAX_MATCH_VALUE_LENGTH)
            if not posixpath.isabs(prefix):
                raise ValueError("policy cwd prefixes require absolute POSIX paths")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActionPolicyCondition":
        allowed = {
            "kinds",
            "operations",
            "effects_any",
            "effects_all",
            "resources_any",
            "risks",
            "decisions",
            "agents",
            "cwd_prefixes",
            "repo_scope",
            "privileged",
            "intent",
            "trajectory_any",
        }
        _reject_unknown_keys(payload, allowed, "policy condition")

        decisions = tuple(validate_decision(value) for value in _string_list(payload, "decisions"))
        resources_raw = payload.get("resources_any", [])
        if not isinstance(resources_raw, list):
            raise ValueError("policy condition resources_any must be an array")
        if len(resources_raw) > MAX_MATCH_VALUES:
            raise ValueError(
                f"policy condition resources_any must contain at most {MAX_MATCH_VALUES} items"
            )
        resources: list[PolicyResourceMatcher] = []
        for item in resources_raw:
            if not isinstance(item, Mapping):
                raise ValueError("policy resource matchers must be JSON objects")
            resources.append(PolicyResourceMatcher.from_dict(item))

        repo_scope = payload.get("repo_scope")
        if repo_scope is not None and repo_scope not in {"inside", "outside", "unknown"}:
            raise ValueError(f"unsupported repo_scope: {repo_scope!r}")
        privileged = payload.get("privileged")
        if privileged is not None and not isinstance(privileged, bool):
            raise ValueError("policy condition privileged must be a boolean or null")
        intent = payload.get("intent")
        if intent is not None and intent not in {
            "present",
            "absent",
            "aligned",
            "mismatch",
            "not_applicable",
        }:
            raise ValueError(f"unsupported intent condition: {intent!r}")

        risks = _string_list(payload, "risks")
        for risk in risks:
            if risk not in {"unknown", "low", "medium", "high", "critical"}:
                raise ValueError(f"unsupported risk in policy condition: {risk!r}")

        return cls(
            kinds=_string_list(payload, "kinds"),
            operations=_string_list(payload, "operations"),
            effects_any=_string_list(payload, "effects_any"),
            effects_all=_string_list(payload, "effects_all"),
            resources_any=tuple(resources),
            risks=risks,
            decisions=decisions,
            agents=_string_list(payload, "agents"),
            cwd_prefixes=_string_list(payload, "cwd_prefixes"),
            repo_scope=cast(RepoScope | None, repo_scope),
            privileged=privileged,
            intent=cast(IntentState | None, intent),
            trajectory_any=_string_list(payload, "trajectory_any"),
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        for key in (
            "kinds",
            "operations",
            "effects_any",
            "effects_all",
            "risks",
            "decisions",
            "agents",
            "cwd_prefixes",
            "trajectory_any",
        ):
            value = getattr(self, key)
            if value:
                payload[key] = list(value)
        if self.resources_any:
            payload["resources_any"] = [item.as_dict() for item in self.resources_any]
        if self.repo_scope is not None:
            payload["repo_scope"] = self.repo_scope
        if self.privileged is not None:
            payload["privileged"] = self.privileged
        if self.intent is not None:
            payload["intent"] = self.intent
        return payload


@dataclass(frozen=True)
class ActionPolicyRule:
    id: str
    decision: Decision
    when: ActionPolicyCondition = field(default_factory=ActionPolicyCondition)
    reason: str | None = None
    safer_next_step: str | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool) or not isinstance(self.when, ActionPolicyCondition):
            raise ValueError("policy rule requires a boolean enabled flag and typed condition")
        _validate_identifier(self.id, "policy rule id", MAX_RULE_ID_LENGTH)
        validate_decision(self.decision)
        if self.reason is not None:
            _validate_text(self.reason, "policy rule reason", MAX_MATCH_VALUE_LENGTH)
        if self.safer_next_step is not None:
            _validate_text(
                self.safer_next_step,
                "policy rule safer_next_step",
                MAX_MATCH_VALUE_LENGTH,
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActionPolicyRule":
        _reject_unknown_keys(
            payload,
            {"id", "decision", "when", "reason", "safer_next_step", "enabled"},
            "policy rule",
        )
        rule_id = payload.get("id")
        decision = payload.get("decision")
        condition = payload.get("when", {})
        reason = payload.get("reason")
        safer_next_step = payload.get("safer_next_step")
        enabled = payload.get("enabled", True)
        if not isinstance(rule_id, str):
            raise ValueError("policy rule requires string id")
        if not isinstance(decision, str):
            raise ValueError("policy rule requires string decision")
        if not isinstance(condition, Mapping):
            raise ValueError("policy rule when must be a JSON object")
        if reason is not None and not isinstance(reason, str):
            raise ValueError("policy rule reason must be a string or null")
        if safer_next_step is not None and not isinstance(safer_next_step, str):
            raise ValueError("policy rule safer_next_step must be a string or null")
        if not isinstance(enabled, bool):
            raise ValueError("policy rule enabled must be a boolean")
        return cls(
            id=rule_id,
            decision=validate_decision(decision),
            when=ActionPolicyCondition.from_dict(condition),
            reason=reason,
            safer_next_step=safer_next_step,
            enabled=enabled,
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "decision": self.decision,
            "when": self.when.as_dict(),
        }
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.safer_next_step is not None:
            payload["safer_next_step"] = self.safer_next_step
        if not self.enabled:
            payload["enabled"] = False
        return payload


@dataclass(frozen=True)
class ActionPolicySet:
    policy_id: str
    version: str
    rules: tuple[ActionPolicyRule, ...]
    schema_version: str = POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != POLICY_SCHEMA_VERSION:
            raise ValueError(f"unsupported policy schema: {self.schema_version!r}")
        _validate_identifier(self.policy_id, "policy_id", MAX_POLICY_ID_LENGTH)
        _validate_identifier(self.version, "policy version", MAX_POLICY_VERSION_LENGTH)
        if len(self.rules) > MAX_POLICY_RULES:
            raise ValueError(f"policy set must contain at most {MAX_POLICY_RULES} rules")
        seen: set[str] = set()
        duplicates: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                duplicates.add(rule.id)
            seen.add(rule.id)
        if duplicates:
            raise ValueError("duplicate policy rule ids: " + ", ".join(sorted(duplicates)))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActionPolicySet":
        _reject_unknown_keys(
            payload, {"schema_version", "policy_id", "version", "rules"}, "policy set"
        )
        schema_version = payload.get("schema_version")
        policy_id = payload.get("policy_id")
        version = payload.get("version")
        rules_raw = payload.get("rules")
        if schema_version != POLICY_SCHEMA_VERSION:
            raise ValueError(f"unsupported policy schema: {schema_version!r}")
        if not isinstance(policy_id, str):
            raise ValueError("policy set requires string policy_id")
        if not isinstance(version, str):
            raise ValueError("policy set requires string version")
        if not isinstance(rules_raw, list):
            raise ValueError("policy set rules must be an array")
        if len(rules_raw) > MAX_POLICY_RULES:
            raise ValueError(f"policy set must contain at most {MAX_POLICY_RULES} rules")
        rules: list[ActionPolicyRule] = []
        for item in rules_raw:
            if not isinstance(item, Mapping):
                raise ValueError("policy rules must be JSON objects")
            rules.append(ActionPolicyRule.from_dict(item))
        return cls(policy_id=policy_id, version=version, rules=tuple(rules))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_id": self.policy_id,
            "version": self.version,
            "rules": [rule.as_dict() for rule in self.rules],
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def compile(self) -> "CompiledActionPolicySet":
        return compile_action_policy(self)


@dataclass(frozen=True)
class PolicyMatch:
    rule_id: str
    decision: Decision
    reason: str
    safer_next_step: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "decision": self.decision,
            "reason": self.reason,
            "safer_next_step": self.safer_next_step,
        }


@dataclass(frozen=True)
class PolicyEvaluation:
    policy_id: str
    policy_version: str
    policy_digest: str
    matches: tuple[PolicyMatch, ...]
    decision: Decision | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "policy_digest": self.policy_digest,
            "decision": self.decision,
            "matches": [match.as_dict() for match in self.matches],
        }


@dataclass(frozen=True)
class CompiledActionPolicySet:
    policy: ActionPolicySet
    active_rules: tuple[ActionPolicyRule, ...]

    @property
    def digest(self) -> str:
        return self.policy.digest

    def evaluate(self, review: ActionReview) -> PolicyEvaluation:
        matches = tuple(
            _match_from_rule(rule)
            for rule in self.active_rules
            if _condition_matches(rule.when, review)
        )
        decision: Decision | None = None
        for match in matches:
            if decision is None or ENFORCEMENT_ORDER[match.decision] > ENFORCEMENT_ORDER[decision]:
                decision = match.decision
        return PolicyEvaluation(
            policy_id=self.policy.policy_id,
            policy_version=self.policy.version,
            policy_digest=self.digest,
            matches=matches,
            decision=decision,
        )

    def apply(self, review: ActionReview) -> ActionReview:
        evaluation = self.evaluate(review)
        policy_metadata = {
            "policy_id": evaluation.policy_id,
            "policy_version": evaluation.policy_version,
            "policy_digest": evaluation.policy_digest,
        }
        if not evaluation.matches:
            provenance = review.provenance
            if provenance is not None:
                provenance = provenance.append(
                    ProvenanceRecord(
                        source="action_policy",
                        kind="finding",
                        code="policy.no-match",
                        metadata=policy_metadata,
                    )
                )
            return replace(
                review,
                policy={
                    "policy_id": evaluation.policy_id,
                    "version": evaluation.policy_version,
                    "digest": evaluation.policy_digest,
                },
                policy_matches=[],
                provenance=provenance,
            )

        final_decision = review.decision
        if (
            evaluation.decision is not None
            and ENFORCEMENT_ORDER[evaluation.decision] > ENFORCEMENT_ORDER[review.decision]
        ):
            final_decision = evaluation.decision

        reasons = list(review.reasons)
        for match in evaluation.matches:
            reasons.append(f"policy {match.rule_id}: {match.reason}")

        safer_next_step = review.safer_next_step
        if final_decision != review.decision and evaluation.decision is not None:
            dominant = next(
                (
                    match
                    for match in evaluation.matches
                    if match.decision == evaluation.decision and match.safer_next_step
                ),
                None,
            )
            if dominant is not None:
                safer_next_step = dominant.safer_next_step

        provenance = review.provenance
        if provenance is not None:
            policy_records = [
                ProvenanceRecord(
                    source="action_policy",
                    kind="rule",
                    code="policy.match",
                    summary=match.reason,
                    decision=match.decision,
                    rule_id=match.rule_id,
                    metadata=policy_metadata,
                )
                for match in evaluation.matches
            ]
            policy_records.append(
                ProvenanceRecord(
                    source="decision",
                    kind="merge",
                    code="decision.policy-merge",
                    decision=final_decision,
                    risk=review.risk,
                    metadata={
                        "previous_decision": review.decision,
                        **policy_metadata,
                    },
                )
            )
            provenance = provenance.append(
                *policy_records,
                final_decision=final_decision,
                final_risk=review.risk,
            )

        return replace(
            review,
            decision=final_decision,
            reasons=reasons,
            safer_next_step=safer_next_step,
            policy={
                "policy_id": evaluation.policy_id,
                "version": evaluation.policy_version,
                "digest": evaluation.policy_digest,
            },
            policy_matches=[match.as_dict() for match in evaluation.matches],
            provenance=provenance,
        )


@lru_cache(maxsize=64)
def compile_action_policy(policy: ActionPolicySet) -> CompiledActionPolicySet:
    """Compile an immutable policy set once and cache by structural value."""

    return CompiledActionPolicySet(
        policy=policy,
        active_rules=tuple(rule for rule in policy.rules if rule.enabled),
    )


def load_action_policy(path: str | Path) -> CompiledActionPolicySet:
    payload = load_configuration(path, label="policy file", maximum=MAX_POLICY_FILE_BYTES)
    from .schema import validate_named_schema

    errors = validate_named_schema("policy_set", dict(payload))
    if errors:
        raise ValueError("policy schema validation failed: " + "; ".join(errors))
    return ActionPolicySet.from_dict(payload).compile()


def _match_from_rule(rule: ActionPolicyRule) -> PolicyMatch:
    reason = rule.reason or f"rule requires decision {rule.decision}"
    return PolicyMatch(
        rule_id=rule.id,
        decision=rule.decision,
        reason=reason,
        safer_next_step=rule.safer_next_step,
    )


def _condition_matches(condition: ActionPolicyCondition, review: ActionReview) -> bool:
    action = review.action
    if condition.kinds and action.kind not in condition.kinds:
        return False
    if condition.operations and action.operation not in condition.operations:
        return False

    effect_set = set(review.effects)
    if condition.effects_any and not effect_set.intersection(condition.effects_any):
        return False
    if condition.effects_all and not set(condition.effects_all).issubset(effect_set):
        return False
    if condition.resources_any and not any(
        matcher.matches(resource)
        for matcher in condition.resources_any
        for resource in review.resources
    ):
        return False
    if condition.risks and review.risk not in condition.risks:
        return False
    if condition.decisions and review.decision not in condition.decisions:
        return False

    context = action.context
    agent = context.agent if context else None
    if condition.agents and agent not in condition.agents:
        return False
    if condition.cwd_prefixes:
        cwd = context.cwd if context else None
        if cwd is None or not any(
            _path_within_prefix(cwd, prefix) for prefix in condition.cwd_prefixes
        ):
            return False
    if condition.repo_scope is not None and _repo_scope(review) != condition.repo_scope:
        return False
    if condition.privileged is not None:
        if context is None or context.euid is None:
            return False
        if (context.euid == 0) != condition.privileged:
            return False
    if condition.intent is not None and not _intent_matches(condition.intent, review):
        return False
    if condition.trajectory_any and not set(review.trajectory_categories).intersection(
        condition.trajectory_any
    ):
        return False
    return True


def _intent_matches(expected: IntentState, review: ActionReview) -> bool:
    present = bool(review.action.intent is not None and review.action.intent.strip())
    if expected == "present":
        return present
    if expected == "absent":
        return not present
    if not present:
        return False
    return review.intent_alignment == expected


def _repo_scope(review: ActionReview) -> RepoScope:
    context = review.action.context
    if context is None or not context.cwd or not context.repo_root:
        return "unknown"
    inside = context.path_within_repo(context.cwd)
    if inside is None:
        return "unknown"
    return "inside" if inside else "outside"


def _path_within_prefix(value: str, prefix: str) -> bool:
    if not posixpath.isabs(value) or not posixpath.isabs(prefix):
        return False
    normalized_value = posixpath.normpath(value)
    normalized_prefix = posixpath.normpath(prefix)
    return posixpath.commonpath((normalized_value, normalized_prefix)) == normalized_prefix


def _string_list(payload: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        raise ValueError(f"policy condition {key} must be an array")
    if len(value) > MAX_MATCH_VALUES:
        raise ValueError(f"policy condition {key} must contain at most {MAX_MATCH_VALUES} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"policy condition {key} items must be strings")
        _validate_text(item, f"policy condition {key} item", MAX_MATCH_VALUE_LENGTH)
        if item not in result:
            result.append(item)
    return tuple(result)


def _reject_unknown_keys(payload: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"{name} contains unsupported fields: {', '.join(unknown)}")


def _validate_identifier(value: str, name: str, maximum: int) -> None:
    _validate_text(value, name, maximum)
    if IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{name} contains unsupported characters")


def _validate_text(value: str, name: str, maximum: int) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty text")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
