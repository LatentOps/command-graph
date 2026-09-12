from __future__ import annotations

from ._json_contracts import load_configuration

import hashlib
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Mapping, Sequence

from .context import ExecutionContext
from .risk import RISK_ORDER, RiskReview, check_command
from .semantics import semantic_evidence_for_command
from .shell import SHELL_EXECUTABLES, executable_name_from_tokens, split_shell_segments


TEMPORAL_POLICY_SCHEMA_VERSION = "ordin.temporal_policy_set.v1"
MAX_HISTORY_ACTIONS = 32
MAX_TEMPORAL_POLICY_FILE_BYTES = 1_048_576
MAX_TEMPORAL_TEXT_LENGTH = 32768
MAX_TEMPORAL_SIGNAL_LENGTH = 256
MAX_TEMPORAL_RULES = 128
MAX_TEMPORAL_STEPS = 8
MAX_STEP_SIGNALS = 64
MAX_STEP_VALUES = 32
VALID_TEMPORAL_RISKS = frozenset(("medium", "high", "critical"))

ROOT = Path(__file__).resolve().parents[1]
SOURCE_POLICY_PATH = ROOT / "data" / "temporal_policies.json"
PACKAGE_POLICY_PATH = Path(__file__).resolve().parent / "resources" / "temporal_policies.json"
DEFAULT_POLICY_PATH = SOURCE_POLICY_PATH if SOURCE_POLICY_PATH.exists() else PACKAGE_POLICY_PATH


@dataclass(frozen=True)
class TemporalActionEvidence:
    kind: str
    operation: str
    signals: frozenset[str]


@dataclass(frozen=True)
class TemporalPredicate:
    signals_any: tuple[str, ...]
    kinds: tuple[str, ...] = ()
    operations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.signals_any:
            raise ValueError("temporal predicate requires at least one signal")
        if len(self.signals_any) > MAX_STEP_SIGNALS:
            raise ValueError(f"temporal predicate supports at most {MAX_STEP_SIGNALS} signals")
        if len(self.kinds) > MAX_STEP_VALUES or len(self.operations) > MAX_STEP_VALUES:
            raise ValueError(
                f"temporal predicate supports at most {MAX_STEP_VALUES} kind/operation values"
            )
        for signal in self.signals_any:
            if not isinstance(signal, str) or not signal:
                raise ValueError("temporal predicate signals must be non-empty strings")
            if len(signal) > MAX_TEMPORAL_SIGNAL_LENGTH:
                raise ValueError(
                    f"temporal predicate signal must be at most {MAX_TEMPORAL_SIGNAL_LENGTH} characters"
                )
            if not signal.startswith(("effect:", "category:", "signal:")):
                raise ValueError(f"unsupported temporal signal namespace: {signal!r}")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TemporalPredicate":
        _reject_unknown(payload, {"signals_any", "kinds", "operations"}, "temporal predicate")
        return cls(
            signals_any=_string_tuple(
                payload, "signals_any", required=True, maximum=MAX_STEP_SIGNALS
            ),
            kinds=_string_tuple(payload, "kinds", maximum=MAX_STEP_VALUES),
            operations=_string_tuple(payload, "operations", maximum=MAX_STEP_VALUES),
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"signals_any": list(self.signals_any)}
        if self.kinds:
            payload["kinds"] = list(self.kinds)
        if self.operations:
            payload["operations"] = list(self.operations)
        return payload

    def matches(self, action: TemporalActionEvidence) -> bool:
        if self.kinds and action.kind not in self.kinds:
            return False
        if self.operations and action.operation not in self.operations:
            return False
        return bool(action.signals.intersection(self.signals_any))


@dataclass(frozen=True)
class TemporalRule:
    id: str
    risk: str
    category: str
    within_actions: int
    pattern: tuple[TemporalPredicate, ...]
    reason: str
    safer_next_step: str | None = None
    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.within_actions, int) or isinstance(self.within_actions, bool):
            raise ValueError("temporal within_actions must be an integer")
        if not isinstance(self.enabled, bool):
            raise ValueError("temporal enabled flag must be boolean")
        if any(not isinstance(predicate, TemporalPredicate) for predicate in self.pattern):
            raise ValueError("temporal pattern requires typed predicates")
        if not self.id or len(self.id) > 128:
            raise ValueError("temporal rule id must contain 1 to 128 characters")
        if self.risk not in VALID_TEMPORAL_RISKS:
            raise ValueError(f"unsupported temporal risk: {self.risk!r}")
        if not self.category or len(self.category) > 128:
            raise ValueError("temporal category must contain 1 to 128 characters")
        if not 1 <= self.within_actions <= MAX_HISTORY_ACTIONS:
            raise ValueError(f"within_actions must be between 1 and {MAX_HISTORY_ACTIONS}")
        if not 1 <= len(self.pattern) <= MAX_TEMPORAL_STEPS:
            raise ValueError(f"temporal pattern must contain 1 to {MAX_TEMPORAL_STEPS} steps")
        if self.within_actions < len(self.pattern):
            raise ValueError("within_actions cannot be smaller than the number of pattern steps")
        if not self.reason:
            raise ValueError("temporal rule reason must be non-empty")
        if len(self.reason) > MAX_TEMPORAL_TEXT_LENGTH:
            raise ValueError(
                f"temporal rule reason must be at most {MAX_TEMPORAL_TEXT_LENGTH} characters"
            )
        if (
            self.safer_next_step is not None
            and len(self.safer_next_step) > MAX_TEMPORAL_TEXT_LENGTH
        ):
            raise ValueError(
                f"temporal safer_next_step must be at most {MAX_TEMPORAL_TEXT_LENGTH} characters"
            )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TemporalRule":
        _reject_unknown(
            payload,
            {
                "id",
                "risk",
                "category",
                "within_actions",
                "pattern",
                "reason",
                "safer_next_step",
                "enabled",
            },
            "temporal rule",
        )
        rule_id = payload.get("id")
        risk = payload.get("risk")
        category = payload.get("category")
        within_actions = payload.get("within_actions")
        pattern_raw = payload.get("pattern")
        reason = payload.get("reason")
        safer_next_step = payload.get("safer_next_step")
        enabled = payload.get("enabled", True)
        if (
            not isinstance(rule_id, str)
            or not isinstance(risk, str)
            or not isinstance(category, str)
        ):
            raise ValueError("temporal rule requires string id, risk, and category")
        if not isinstance(within_actions, int) or isinstance(within_actions, bool):
            raise ValueError("temporal rule within_actions must be an integer")
        if not isinstance(pattern_raw, list):
            raise ValueError("temporal rule pattern must be an array")
        if not isinstance(reason, str):
            raise ValueError("temporal rule reason must be a string")
        if safer_next_step is not None and not isinstance(safer_next_step, str):
            raise ValueError("temporal safer_next_step must be a string or null")
        if not isinstance(enabled, bool):
            raise ValueError("temporal rule enabled must be a boolean")
        predicates: list[TemporalPredicate] = []
        for item in pattern_raw:
            if not isinstance(item, Mapping):
                raise ValueError("temporal pattern steps must be JSON objects")
            predicates.append(TemporalPredicate.from_dict(item))
        return cls(
            id=rule_id,
            risk=risk,
            category=category,
            within_actions=within_actions,
            pattern=tuple(predicates),
            reason=reason,
            safer_next_step=safer_next_step,
            enabled=enabled,
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "risk": self.risk,
            "category": self.category,
            "within_actions": self.within_actions,
            "pattern": [predicate.as_dict() for predicate in self.pattern],
            "reason": self.reason,
            "safer_next_step": self.safer_next_step,
        }
        if not self.enabled:
            payload["enabled"] = False
        return payload


@dataclass(frozen=True)
class TemporalPolicySet:
    policy_id: str
    version: str
    rules: tuple[TemporalRule, ...]
    schema_version: str = TEMPORAL_POLICY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TEMPORAL_POLICY_SCHEMA_VERSION:
            raise ValueError(f"unsupported temporal policy schema: {self.schema_version!r}")
        if not self.policy_id or len(self.policy_id) > 128:
            raise ValueError("temporal policy_id must contain 1 to 128 characters")
        if not self.version or len(self.version) > 64:
            raise ValueError("temporal policy version must contain 1 to 64 characters")
        if len(self.rules) > MAX_TEMPORAL_RULES:
            raise ValueError(f"temporal policy supports at most {MAX_TEMPORAL_RULES} rules")
        seen: set[str] = set()
        duplicates: set[str] = set()
        for rule in self.rules:
            if rule.id in seen:
                duplicates.add(rule.id)
            seen.add(rule.id)
        if duplicates:
            raise ValueError("duplicate temporal rule ids: " + ", ".join(sorted(duplicates)))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TemporalPolicySet":
        _reject_unknown(
            payload, {"schema_version", "policy_id", "version", "rules"}, "temporal policy set"
        )
        schema_version = payload.get("schema_version")
        policy_id = payload.get("policy_id")
        version = payload.get("version")
        rules_raw = payload.get("rules")
        if schema_version != TEMPORAL_POLICY_SCHEMA_VERSION:
            raise ValueError(f"unsupported temporal policy schema: {schema_version!r}")
        if not isinstance(policy_id, str) or not isinstance(version, str):
            raise ValueError("temporal policy set requires string policy_id and version")
        if not isinstance(rules_raw, list):
            raise ValueError("temporal policy rules must be an array")
        rules: list[TemporalRule] = []
        for item in rules_raw:
            if not isinstance(item, Mapping):
                raise ValueError("temporal rules must be JSON objects")
            rules.append(TemporalRule.from_dict(item))
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

    def compile(self) -> "CompiledTemporalPolicySet":
        return compile_temporal_policy(self)


@dataclass(frozen=True)
class TemporalMatch:
    rule_id: str
    risk: str
    category: str
    reason: str
    safer_next_step: str | None
    matched_indices: tuple[int, ...]


@dataclass(frozen=True)
class TemporalEvaluation:
    history_length: int
    matches: tuple[TemporalMatch, ...]

    @property
    def risk(self) -> str | None:
        if not self.matches:
            return None
        return max((match.risk for match in self.matches), key=lambda value: RISK_ORDER[value])

    @property
    def categories(self) -> list[str]:
        return sorted({match.category for match in self.matches})


@dataclass(frozen=True)
class _MachineState:
    next_step: int
    start_index: int
    matched_indices: tuple[int, ...]


@dataclass(frozen=True)
class CompiledTemporalRule:
    rule: TemporalRule

    def evaluate(self, actions: Sequence[TemporalActionEvidence]) -> TemporalMatch | None:
        if not actions:
            return None
        final_index = len(actions) - 1
        states: list[_MachineState] = []
        for index, action in enumerate(actions):
            next_states: list[_MachineState] = []
            for state in states:
                span = index - state.start_index + 1
                if span > self.rule.within_actions:
                    continue
                predicate = self.rule.pattern[state.next_step]
                if predicate.matches(action):
                    matched = state.matched_indices + (index,)
                    next_step = state.next_step + 1
                    if next_step == len(self.rule.pattern):
                        if index == final_index:
                            return TemporalMatch(
                                rule_id=self.rule.id,
                                risk=self.rule.risk,
                                category=self.rule.category,
                                reason=self.rule.reason,
                                safer_next_step=self.rule.safer_next_step,
                                matched_indices=matched,
                            )
                    else:
                        next_states.append(
                            _MachineState(
                                next_step=next_step,
                                start_index=state.start_index,
                                matched_indices=matched,
                            )
                        )
                else:
                    next_states.append(state)

            first = self.rule.pattern[0]
            if first.matches(action):
                if len(self.rule.pattern) == 1:
                    if index == final_index:
                        return TemporalMatch(
                            rule_id=self.rule.id,
                            risk=self.rule.risk,
                            category=self.rule.category,
                            reason=self.rule.reason,
                            safer_next_step=self.rule.safer_next_step,
                            matched_indices=(index,),
                        )
                else:
                    next_states.append(
                        _MachineState(next_step=1, start_index=index, matched_indices=(index,))
                    )
            states = next_states[-self.rule.within_actions :]
        return None


@dataclass(frozen=True)
class CompiledTemporalPolicySet:
    policy: TemporalPolicySet
    rules: tuple[CompiledTemporalRule, ...]

    def evaluate(
        self,
        prior: Sequence[TemporalActionEvidence],
        current: TemporalActionEvidence,
    ) -> TemporalEvaluation:
        bounded_prior = tuple(prior[-MAX_HISTORY_ACTIONS:])
        actions = (*bounded_prior, current)
        matches = tuple(
            match for compiled in self.rules if (match := compiled.evaluate(actions)) is not None
        )
        return TemporalEvaluation(history_length=len(bounded_prior), matches=matches)


@lru_cache(maxsize=64)
def compile_temporal_policy(policy: TemporalPolicySet) -> CompiledTemporalPolicySet:
    return CompiledTemporalPolicySet(
        policy=policy,
        rules=tuple(CompiledTemporalRule(rule) for rule in policy.rules if rule.enabled),
    )


@lru_cache(maxsize=1)
def default_temporal_policy() -> CompiledTemporalPolicySet:
    return load_temporal_policy(DEFAULT_POLICY_PATH)


def load_temporal_policy(path: str | Path) -> CompiledTemporalPolicySet:
    payload = load_configuration(
        path, label="temporal policy file", maximum=MAX_TEMPORAL_POLICY_FILE_BYTES
    )
    from .schema import validate_named_schema

    errors = validate_named_schema("temporal_policy_set", dict(payload))
    if errors:
        raise ValueError("temporal policy schema validation failed: " + "; ".join(errors))
    return TemporalPolicySet.from_dict(payload).compile()


def temporal_evidence_for_command(
    command: str,
    *,
    context: ExecutionContext | None = None,
    review: RiskReview | None = None,
) -> TemporalActionEvidence:
    risk_review = review or check_command(command, context=context)
    semantics = semantic_evidence_for_command(command, context=context)
    signals = {f"effect:{effect}" for effect in semantics.effects}
    signals.update(f"category:{category}" for category in (risk_review.risk_categories or []))
    if _looks_like_path_execution(command):
        signals.add("signal:path_execution")
        signals.add("effect:code.execute")
    return TemporalActionEvidence(kind="shell", operation="execute", signals=frozenset(signals))


def _looks_like_path_execution(command: str) -> bool:
    try:
        segments, _ = split_shell_segments(command)
    except ValueError:
        return False
    if not segments:
        return False
    tokens = segments[0]
    executable = executable_name_from_tokens(tokens)
    if executable in SHELL_EXECUTABLES and len(tokens) > 1:
        candidate = tokens[-1]
        return candidate.startswith(("./", "../", "/", "~/"))
    if not tokens:
        return False
    candidate = tokens[0]
    return candidate.startswith(("./", "../", "/", "~/"))


def _string_tuple(
    payload: Mapping[str, Any],
    key: str,
    *,
    required: bool = False,
    maximum: int,
) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None:
        if required:
            raise ValueError(f"{key} is required")
        return ()
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    if len(value) > maximum:
        raise ValueError(f"{key} must contain at most {maximum} items")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError(f"{key} items must be non-empty strings")
        if len(item) > MAX_TEMPORAL_SIGNAL_LENGTH:
            raise ValueError(f"{key} items must be at most {MAX_TEMPORAL_SIGNAL_LENGTH} characters")
        if item not in result:
            result.append(item)
    if required and not result:
        raise ValueError(f"{key} must contain at least one value")
    return tuple(result)


def _reject_unknown(payload: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"{name} contains unsupported fields: {', '.join(unknown)}")
