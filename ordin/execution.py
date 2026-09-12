from __future__ import annotations

import math
from ._json_contracts import reject_unknown

import re
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Protocol, Sequence, TypeAlias

from . import (
    ACTION_OBSERVATION_SCHEMA_VERSION,
    EXECUTION_CAPABILITIES_SCHEMA_VERSION,
    OBSERVATION_HISTORY_SCHEMA_VERSION,
)


CapabilityAccess: TypeAlias = Literal["none", "read", "write", "unknown"]
MAX_HISTORY_ITEMS = 32
MAX_EFFECTS = 128
MAX_RESOURCES = 128
MAX_ID_LENGTH = 128
MAX_METADATA_DEPTH = 8
MAX_METADATA_ITEMS = 128
MAX_METADATA_STRING_LENGTH = 32768
MAX_EFFECT_LENGTH = 256
RESOURCE_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
EFFECT_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]*$")
FILESYSTEM_READ_ONLY_EFFECTS = frozenset(("filesystem.read", "filesystem.metadata_read"))
NETWORK_READ_ONLY_EFFECTS = frozenset(("network.connect", "network.download"))
FILESYSTEM_RESOURCE_TYPES = frozenset(("path", "file", "directory"))
NETWORK_RESOURCE_TYPES = frozenset(("url", "host", "network", "endpoint"))


class ResourceLike(Protocol):
    @property
    def type(self) -> str: ...

    @property
    def value(self) -> str: ...


def _validate_json(value: Any, *, path: str = "metadata", depth: int = 0) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{path} requires finite JSON numbers")
    if depth > MAX_METADATA_DEPTH:
        raise ValueError(f"{path} exceeds maximum nesting depth {MAX_METADATA_DEPTH}")
    if value is None or isinstance(value, (bool, int, float, str)):
        if isinstance(value, str) and len(value) > MAX_METADATA_STRING_LENGTH:
            raise ValueError(
                f"{path} string must be at most {MAX_METADATA_STRING_LENGTH} characters"
            )
        return
    if isinstance(value, list):
        if len(value) > MAX_METADATA_ITEMS:
            raise ValueError(f"{path} must contain at most {MAX_METADATA_ITEMS} items")
        for index, item in enumerate(value):
            _validate_json(item, path=f"{path}[{index}]", depth=depth + 1)
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_METADATA_ITEMS:
            raise ValueError(f"{path} must contain at most {MAX_METADATA_ITEMS} properties")
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ValueError(f"{path} keys must be non-empty strings")
            if len(key) > MAX_ID_LENGTH:
                raise ValueError(f"{path} key must be at most {MAX_ID_LENGTH} characters")
            _validate_json(item, path=f"{path}.{key}", depth=depth + 1)
        return
    raise ValueError(f"{path} contains non-JSON value {type(value).__name__}")


def _copy_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value


def _validate_scope(scope: str, *, name: str) -> str:
    if not isinstance(scope, str) or not scope:
        raise ValueError(f"{name} must be non-empty text")
    if len(scope) > MAX_METADATA_STRING_LENGTH:
        raise ValueError(f"{name} must be at most {MAX_METADATA_STRING_LENGTH} characters")
    return scope


def _validate_effect(effect: str) -> str:
    if not isinstance(effect, str) or not effect:
        raise ValueError("observation effects must be non-empty strings")
    if len(effect) > MAX_EFFECT_LENGTH:
        raise ValueError(f"observation effect must be at most {MAX_EFFECT_LENGTH} characters")
    if EFFECT_PATTERN.fullmatch(effect) is None:
        raise ValueError(f"unsupported observation effect identifier: {effect!r}")
    return effect


@dataclass(frozen=True)
class ExecutionCapabilityProfile:
    """Advisory capabilities a caller-owned sandbox should consider granting."""

    filesystem: CapabilityAccess = "none"
    filesystem_scopes: tuple[str, ...] = ()
    network: CapabilityAccess = "none"
    network_scopes: tuple[str, ...] = ()
    privilege_escalation: bool | None = False
    process_execution: bool | None = False

    def __post_init__(self) -> None:
        valid = {"none", "read", "write", "unknown"}
        if self.filesystem not in valid or self.network not in valid:
            raise ValueError("capability access must be none, read, write, or unknown")
        if len(self.filesystem_scopes) > MAX_RESOURCES:
            raise ValueError("too many filesystem capability scopes")
        if len(self.network_scopes) > MAX_RESOURCES:
            raise ValueError("too many network capability scopes")
        for scope in self.filesystem_scopes:
            _validate_scope(scope, name="filesystem capability scope")
        for scope in self.network_scopes:
            _validate_scope(scope, name="network capability scope")
        if self.privilege_escalation is not None and not isinstance(
            self.privilege_escalation, bool
        ):
            raise ValueError("privilege_escalation must be boolean or null")
        if self.process_execution is not None and not isinstance(self.process_execution, bool):
            raise ValueError("process_execution must be boolean or null")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EXECUTION_CAPABILITIES_SCHEMA_VERSION,
            "filesystem": self.filesystem,
            "filesystem_scopes": list(self.filesystem_scopes),
            "network": self.network,
            "network_scopes": list(self.network_scopes),
            "privilege_escalation": self.privilege_escalation,
            "process_execution": self.process_execution,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionCapabilityProfile":
        reject_unknown(
            payload,
            {
                "schema_version",
                "filesystem",
                "filesystem_scopes",
                "network",
                "network_scopes",
                "privilege_escalation",
                "process_execution",
            },
            "execution capabilities",
        )
        if not isinstance(payload, Mapping):
            raise ValueError("execution capability profile must be a JSON object")
        if payload.get("schema_version") != EXECUTION_CAPABILITIES_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported execution capability schema: {payload.get('schema_version')!r}"
            )
        filesystem = payload.get("filesystem")
        filesystem_scopes = payload.get("filesystem_scopes")
        network = payload.get("network")
        network_scopes = payload.get("network_scopes")
        privilege = payload.get("privilege_escalation")
        process_execution = payload.get("process_execution")
        if filesystem not in {"none", "read", "write", "unknown"}:
            raise ValueError("invalid filesystem capability")
        if network not in {"none", "read", "write", "unknown"}:
            raise ValueError("invalid network capability")
        if not isinstance(filesystem_scopes, list) or any(
            not isinstance(item, str) for item in filesystem_scopes
        ):
            raise ValueError("filesystem_scopes must be an array of strings")
        if not isinstance(network_scopes, list) or any(
            not isinstance(item, str) for item in network_scopes
        ):
            raise ValueError("network_scopes must be an array of strings")
        if privilege is not None and not isinstance(privilege, bool):
            raise ValueError("privilege_escalation must be boolean or null")
        if process_execution is not None and not isinstance(process_execution, bool):
            raise ValueError("process_execution must be boolean or null")
        return cls(
            filesystem=filesystem,
            filesystem_scopes=tuple(filesystem_scopes),
            network=network,
            network_scopes=tuple(network_scopes),
            privilege_escalation=privilege,
            process_execution=process_execution,
        )


@dataclass(frozen=True)
class ObservedResource:
    type: str
    value: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.type, str)
            or not self.type
            or len(self.type) > 64
            or RESOURCE_TYPE_PATTERN.fullmatch(self.type) is None
        ):
            raise ValueError("observed resource type must be a valid 1 to 64 character identifier")
        _validate_scope(self.value, name="observed resource value")

    def as_dict(self) -> dict[str, str]:
        return {"type": self.type, "value": self.value}


@dataclass(frozen=True)
class ActionObservation:
    """Caller-supplied evidence about what happened after an action executed."""

    action_id: str
    exit_code: int | None = None
    effects: tuple[str, ...] = ()
    resources: tuple[ObservedResource, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.action_id, str) or not self.action_id.strip():
            raise ValueError("observation action_id must be non-empty text")
        if len(self.action_id) > MAX_ID_LENGTH:
            raise ValueError(f"observation action_id must be at most {MAX_ID_LENGTH} characters")
        if self.exit_code is not None and (
            not isinstance(self.exit_code, int) or isinstance(self.exit_code, bool)
        ):
            raise ValueError("observation exit_code must be an integer or null")
        if len(self.effects) > MAX_EFFECTS:
            raise ValueError(f"observation supports at most {MAX_EFFECTS} effects")
        for effect in self.effects:
            _validate_effect(effect)
        if len(self.resources) > MAX_RESOURCES:
            raise ValueError(f"observation supports at most {MAX_RESOURCES} resources")
        if any(not isinstance(resource, ObservedResource) for resource in self.resources):
            raise ValueError("observation resources must be ObservedResource values")
        if not isinstance(self.metadata, Mapping):
            raise ValueError("observation metadata must be a JSON object")
        _validate_json(self.metadata)
        object.__setattr__(self, "metadata", _copy_json(self.metadata))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ACTION_OBSERVATION_SCHEMA_VERSION,
            "action_id": self.action_id,
            "exit_code": self.exit_code,
            "effects": list(self.effects),
            "resources": [resource.as_dict() for resource in self.resources],
            "metadata": _copy_json(self.metadata),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ActionObservation":
        reject_unknown(
            payload,
            {"schema_version", "action_id", "exit_code", "effects", "resources", "metadata"},
            "action observation",
        )
        if not isinstance(payload, Mapping):
            raise ValueError("action observation must be a JSON object")
        if payload.get("schema_version") != ACTION_OBSERVATION_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported action observation schema: {payload.get('schema_version')!r}"
            )
        action_id = payload.get("action_id")
        exit_code = payload.get("exit_code")
        effects = payload.get("effects", [])
        resources_raw = payload.get("resources", [])
        metadata = payload.get("metadata", {})
        if not isinstance(action_id, str):
            raise ValueError("observation requires string action_id")
        if not isinstance(effects, list) or any(not isinstance(item, str) for item in effects):
            raise ValueError("observation effects must be an array of strings")
        if not isinstance(resources_raw, list):
            raise ValueError("observation resources must be an array")
        if not isinstance(metadata, Mapping):
            raise ValueError("observation metadata must be an object")
        resources: list[ObservedResource] = []
        for item in resources_raw:
            if not isinstance(item, Mapping):
                raise ValueError("observation resources must be objects")
            reject_unknown(item, {"type", "value"}, "observation resource")
            resource_type = item.get("type")
            value = item.get("value")
            if not isinstance(resource_type, str) or not isinstance(value, str):
                raise ValueError("observation resource requires string type and value")
            resources.append(ObservedResource(type=resource_type, value=value))
        return cls(
            action_id=action_id,
            exit_code=exit_code,
            effects=tuple(effects),
            resources=tuple(resources),
            metadata=metadata,
        )


@dataclass(frozen=True)
class ObservationHistory:
    observations: tuple[ActionObservation, ...] = ()

    def __post_init__(self) -> None:
        if len(self.observations) > MAX_HISTORY_ITEMS:
            raise ValueError(f"observation history maximum is {MAX_HISTORY_ITEMS}")
        ids = [observation.action_id for observation in self.observations]
        if len(ids) != len(set(ids)):
            raise ValueError("observation history action_id values must be unique")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": OBSERVATION_HISTORY_SCHEMA_VERSION,
            "observations": [observation.as_dict() for observation in self.observations],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ObservationHistory":
        reject_unknown(payload, {"schema_version", "observations"}, "observation history")
        if not isinstance(payload, Mapping):
            raise ValueError("observation history must be a JSON object")
        if payload.get("schema_version") != OBSERVATION_HISTORY_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported observation history schema: {payload.get('schema_version')!r}"
            )
        raw = payload.get("observations")
        if not isinstance(raw, list):
            raise ValueError("observation history observations must be an array")
        if len(raw) > MAX_HISTORY_ITEMS:
            raise ValueError(f"observation history maximum is {MAX_HISTORY_ITEMS}")
        observations: list[ActionObservation] = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise ValueError("observation history entries must be objects")
            observations.append(ActionObservation.from_dict(item))
        return cls(observations=tuple(observations))

    def by_action_id(self) -> dict[str, ActionObservation]:
        return {observation.action_id: observation for observation in self.observations}


def derive_capabilities(
    action_kind: str,
    effects: Sequence[str],
    resources: Sequence[ResourceLike],
) -> ExecutionCapabilityProfile:
    """Derive a conservative advisory sandbox profile from normalized semantics."""

    if not effects and action_kind != "shell":
        return ExecutionCapabilityProfile(
            filesystem="unknown",
            network="unknown",
            privilege_escalation=None,
            process_execution=None,
        )

    effect_set = set(effects)
    filesystem_effects = {effect for effect in effect_set if effect.startswith("filesystem.")}
    network_effects = {effect for effect in effect_set if effect.startswith("network.")}

    filesystem: CapabilityAccess = "none"
    if filesystem_effects:
        filesystem = (
            "read" if filesystem_effects.issubset(FILESYSTEM_READ_ONLY_EFFECTS) else "write"
        )

    network: CapabilityAccess = "none"
    if network_effects:
        network = "read" if network_effects.issubset(NETWORK_READ_ONLY_EFFECTS) else "write"

    filesystem_scopes = tuple(
        sorted(
            {resource.value for resource in resources if resource.type in FILESYSTEM_RESOURCE_TYPES}
        )
    )
    network_scopes = tuple(
        sorted(
            {resource.value for resource in resources if resource.type in NETWORK_RESOURCE_TYPES}
        )
    )
    privilege = any(effect.startswith("privilege.") for effect in effect_set)
    process_execution = action_kind == "shell" or any(
        effect.startswith("code.") or effect.startswith("process.") for effect in effect_set
    )

    return ExecutionCapabilityProfile(
        filesystem=filesystem,
        filesystem_scopes=filesystem_scopes,
        network=network,
        network_scopes=network_scopes,
        privilege_escalation=privilege,
        process_execution=process_execution,
    )
