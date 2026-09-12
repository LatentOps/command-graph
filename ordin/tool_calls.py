from __future__ import annotations

from ._json_contracts import load_configuration

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .action import ActionEnvelope, ActionResource, ActionReview
from .data import load_effect_catalog
from .risk import decision_for_risk, max_risk


TOOL_SEMANTICS_SCHEMA_VERSION = "ordin.tool_semantics.v1"
MAX_TOOL_SEMANTICS_FILE_BYTES = 1_048_576
MAX_TOOL_SEMANTICS_RULES = 256
MAX_TOOL_EFFECTS = 64
MAX_RESOURCE_BINDINGS = 64
MAX_ARGUMENT_PATH_SEGMENTS = 16
MAX_NAME_LENGTH = 256
MAX_ARGUMENT_PATH_LENGTH = 512

ToolIdentity = tuple[str, str, str]


def _required_text(value: str, *, name: str, maximum: int = MAX_NAME_LENGTH) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return value


def _reject_unknown(payload: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(payload) - allowed)
    if unknown:
        raise ValueError(f"{name} contains unsupported fields: {', '.join(unknown)}")


@dataclass(frozen=True)
class ToolResourceBinding:
    """Extract one resource from a bounded nested argument path."""

    argument: str
    type: str

    def __post_init__(self) -> None:
        _required_text(self.type, name="resource type")
        _required_text(
            self.argument,
            name="resource argument path",
            maximum=MAX_ARGUMENT_PATH_LENGTH,
        )
        parts = self.argument.split(".")
        if len(parts) > MAX_ARGUMENT_PATH_SEGMENTS:
            raise ValueError(
                f"resource argument path supports at most {MAX_ARGUMENT_PATH_SEGMENTS} segments"
            )
        for part in parts:
            _required_text(part, name="resource argument path segment")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ToolResourceBinding":
        _reject_unknown(payload, {"argument", "type"}, "tool resource binding")
        argument = payload.get("argument")
        resource_type = payload.get("type")
        if not isinstance(argument, str) or not isinstance(resource_type, str):
            raise ValueError("tool resource binding requires string argument and type")
        return cls(argument=argument, type=resource_type)

    def as_dict(self) -> dict[str, str]:
        return {"argument": self.argument, "type": self.type}


@dataclass(frozen=True)
class ToolSemanticRule:
    """Trusted semantics for one exact generic-tool or MCP-tool identity."""

    id: str
    kind: str
    tool: str
    effects: tuple[str, ...]
    runtime: str | None = None
    server: str | None = None
    resources: tuple[ToolResourceBinding, ...] = ()

    def __post_init__(self) -> None:
        _required_text(self.id, name="tool semantics rule id")
        _required_text(self.tool, name="tool semantics tool")
        if self.kind not in {"tool", "mcp"}:
            raise ValueError("tool semantics kind must be 'tool' or 'mcp'")
        if self.kind == "tool":
            if self.runtime is None:
                raise ValueError("generic tool semantics require an exact runtime identity")
            _required_text(self.runtime, name="tool semantics runtime")
            if self.server is not None:
                raise ValueError("generic tool semantics cannot declare an MCP server")
        else:
            if self.server is None:
                raise ValueError("MCP tool semantics require an exact server identity")
            _required_text(self.server, name="tool semantics server")
            if self.runtime is not None:
                raise ValueError("MCP tool semantics cannot declare a generic runtime")
        if not self.effects:
            raise ValueError("tool semantics require at least one typed effect")
        if len(self.effects) > MAX_TOOL_EFFECTS:
            raise ValueError(f"tool semantics support at most {MAX_TOOL_EFFECTS} effects")
        for effect in self.effects:
            _required_text(effect, name="tool semantics effect")
        if len(self.resources) > MAX_RESOURCE_BINDINGS:
            raise ValueError(f"tool semantics support at most {MAX_RESOURCE_BINDINGS} resources")

    @property
    def identity(self) -> ToolIdentity:
        scope = self.runtime if self.kind == "tool" else self.server
        assert scope is not None
        return (self.kind, scope, self.tool)

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ToolSemanticRule":
        _reject_unknown(
            payload,
            {"id", "kind", "runtime", "server", "tool", "effects", "resources"},
            "tool semantics rule",
        )
        rule_id = payload.get("id")
        kind = payload.get("kind")
        runtime = payload.get("runtime")
        server = payload.get("server")
        tool = payload.get("tool")
        effects_raw = payload.get("effects")
        resources_raw = payload.get("resources", [])
        if not isinstance(rule_id, str) or not isinstance(kind, str) or not isinstance(tool, str):
            raise ValueError("tool semantics rule requires string id, kind, and tool")
        if runtime is not None and not isinstance(runtime, str):
            raise ValueError("tool semantics runtime must be a string or null")
        if server is not None and not isinstance(server, str):
            raise ValueError("tool semantics server must be a string or null")
        if not isinstance(effects_raw, list) or any(
            not isinstance(item, str) for item in effects_raw
        ):
            raise ValueError("tool semantics effects must be an array of strings")
        if len(effects_raw) > MAX_TOOL_EFFECTS:
            raise ValueError(f"tool semantics effects support at most {MAX_TOOL_EFFECTS} items")
        if not isinstance(resources_raw, list):
            raise ValueError("tool semantics resources must be an array")
        if len(resources_raw) > MAX_RESOURCE_BINDINGS:
            raise ValueError(
                f"tool semantics resources support at most {MAX_RESOURCE_BINDINGS} items"
            )
        resources: list[ToolResourceBinding] = []
        for item in resources_raw:
            if not isinstance(item, Mapping):
                raise ValueError("tool resource bindings must be JSON objects")
            resources.append(ToolResourceBinding.from_dict(item))
        return cls(
            id=rule_id,
            kind=kind,
            runtime=runtime,
            server=server,
            tool=tool,
            effects=tuple(dict.fromkeys(effects_raw)),
            resources=tuple(resources),
        )

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "tool": self.tool,
            "effects": list(self.effects),
            "resources": [binding.as_dict() for binding in self.resources],
        }
        if self.runtime is not None:
            payload["runtime"] = self.runtime
        if self.server is not None:
            payload["server"] = self.server
        return payload


@dataclass(frozen=True)
class ToolSemanticsRegistry:
    registry_id: str
    version: str
    rules: tuple[ToolSemanticRule, ...]
    schema_version: str = TOOL_SEMANTICS_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != TOOL_SEMANTICS_SCHEMA_VERSION:
            raise ValueError(f"unsupported tool semantics schema: {self.schema_version!r}")
        _required_text(self.registry_id, name="tool semantics registry_id")
        _required_text(self.version, name="tool semantics version")
        if len(self.rules) > MAX_TOOL_SEMANTICS_RULES:
            raise ValueError(f"tool semantics support at most {MAX_TOOL_SEMANTICS_RULES} rules")

        catalog = load_effect_catalog()
        identities: set[ToolIdentity] = set()
        rule_ids: set[str] = set()
        for rule in self.rules:
            if rule.id in rule_ids:
                raise ValueError(f"duplicate tool semantics rule id: {rule.id}")
            rule_ids.add(rule.id)
            if rule.identity in identities:
                raise ValueError(f"duplicate tool semantics identity: {rule.identity!r}")
            identities.add(rule.identity)
            unknown = sorted(effect for effect in rule.effects if effect not in catalog)
            if unknown:
                raise ValueError("unknown effects in tool semantics: " + ", ".join(unknown))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ToolSemanticsRegistry":
        _reject_unknown(
            payload,
            {"schema_version", "registry_id", "version", "rules"},
            "tool semantics registry",
        )
        schema_version = payload.get("schema_version")
        registry_id = payload.get("registry_id")
        version = payload.get("version")
        rules_raw = payload.get("rules")
        if schema_version != TOOL_SEMANTICS_SCHEMA_VERSION:
            raise ValueError(f"unsupported tool semantics schema: {schema_version!r}")
        if not isinstance(registry_id, str) or not isinstance(version, str):
            raise ValueError("tool semantics registry requires string registry_id and version")
        if not isinstance(rules_raw, list):
            raise ValueError("tool semantics rules must be an array")
        if len(rules_raw) > MAX_TOOL_SEMANTICS_RULES:
            raise ValueError(
                f"tool semantics rules support at most {MAX_TOOL_SEMANTICS_RULES} items"
            )
        rules: list[ToolSemanticRule] = []
        for item in rules_raw:
            if not isinstance(item, Mapping):
                raise ValueError("tool semantics rules must be JSON objects")
            rules.append(ToolSemanticRule.from_dict(item))
        return cls(registry_id=registry_id, version=version, rules=tuple(rules))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "registry_id": self.registry_id,
            "version": self.version,
            "rules": [rule.as_dict() for rule in self.rules],
        }

    def compile(self) -> "CompiledToolSemanticsRegistry":
        return compile_tool_semantics(self)


@dataclass(frozen=True)
class CompiledToolSemanticRule:
    rule: ToolSemanticRule
    risk: str
    reasons: tuple[str, ...]
    safer_next_step: str | None


@dataclass(frozen=True)
class CompiledToolSemanticsRegistry:
    registry: ToolSemanticsRegistry
    by_identity: Mapping[ToolIdentity, CompiledToolSemanticRule]

    def find(self, action: ActionEnvelope) -> CompiledToolSemanticRule | None:
        identity = _action_identity(action)
        return self.by_identity.get(identity) if identity is not None else None


@lru_cache(maxsize=32)
def compile_tool_semantics(registry: ToolSemanticsRegistry) -> CompiledToolSemanticsRegistry:
    """Compile exact identities and static effect metadata once."""

    catalog = load_effect_catalog()
    compiled: dict[ToolIdentity, CompiledToolSemanticRule] = {}
    for rule in registry.rules:
        risk = "low"
        reasons: list[str] = []
        safer_next_step: str | None = None
        for effect in rule.effects:
            definition = catalog[effect]
            effect_risk = definition.get("risk")
            if isinstance(effect_risk, str):
                risk = max_risk(risk, effect_risk)
            reason = definition.get("reason")
            if isinstance(reason, str) and reason not in reasons:
                reasons.append(reason)
            candidate = definition.get("safer_next_step")
            if safer_next_step is None and isinstance(candidate, str):
                safer_next_step = candidate
        compiled[rule.identity] = CompiledToolSemanticRule(
            rule=rule,
            risk=risk,
            reasons=tuple(reasons),
            safer_next_step=safer_next_step,
        )
    return CompiledToolSemanticsRegistry(
        registry=registry,
        by_identity=MappingProxyType(compiled),
    )


def load_tool_semantics(path: str | Path) -> CompiledToolSemanticsRegistry:
    payload = load_configuration(
        path, label="tool semantics file", maximum=MAX_TOOL_SEMANTICS_FILE_BYTES
    )
    from .schema import validate_named_schema

    errors = validate_named_schema("tool_semantics", dict(payload))
    if errors:
        raise ValueError("tool semantics schema validation failed: " + "; ".join(errors))
    return ToolSemanticsRegistry.from_dict(payload).compile()


def review_tool_action(
    action: ActionEnvelope,
    registry: ToolSemanticsRegistry | CompiledToolSemanticsRegistry,
) -> ActionReview:
    """Apply trusted local semantics to one already-normalized tool action."""

    compiled = registry.compile() if isinstance(registry, ToolSemanticsRegistry) else registry
    match = compiled.find(action)
    if match is None:
        identity = _action_identity(action)
        return ActionReview(
            action=action,
            decision="ask",
            risk="unknown",
            reasons=[f"no trusted local semantics are registered for tool identity {identity!r}"],
            safer_next_step="Require explicit approval or configure trusted local tool semantics.",
            effects=[],
            resources=[],
            adapter=None,
        )

    resources = _resources_from_bindings(action, match.rule.resources)
    return ActionReview(
        action=action,
        decision=decision_for_risk(match.risk),
        risk=match.risk,
        reasons=list(match.reasons)
        or [f"trusted local tool semantics rule {match.rule.id} matched"],
        safer_next_step=match.safer_next_step,
        effects=list(match.rule.effects),
        resources=resources,
        adapter=f"tool-semantics:{match.rule.id}",
    )


def _action_identity(action: ActionEnvelope) -> ToolIdentity | None:
    if action.operation != "call" or action.kind not in {"tool", "mcp"}:
        return None
    tool = action.parameters.get("tool")
    if not isinstance(tool, str) or not tool:
        return None
    if action.kind == "tool":
        runtime = action.parameters.get("runtime")
        if not isinstance(runtime, str) or not runtime:
            return None
        return ("tool", runtime, tool)
    server = action.parameters.get("server")
    if not isinstance(server, str) or not server:
        return None
    return ("mcp", server, tool)


def _resources_from_bindings(
    action: ActionEnvelope,
    bindings: tuple[ToolResourceBinding, ...],
) -> list[ActionResource]:
    arguments = action.parameters.get("arguments")
    if not isinstance(arguments, Mapping):
        return []
    resources: list[ActionResource] = []
    seen: set[tuple[str, str]] = set()
    for binding in bindings:
        value = _lookup_argument(arguments, binding.argument)
        if not isinstance(value, str) or not value:
            continue
        key = (binding.type, value)
        if key in seen:
            continue
        seen.add(key)
        resources.append(ActionResource(type=binding.type, value=value))
    return resources


def _lookup_argument(arguments: Mapping[str, Any], path: str) -> Any:
    current: Any = arguments
    for part in path.split("."):
        if not isinstance(current, Mapping) or part not in current:
            return None
        current = current[part]
    return current
