from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from . import ACTION_TRACE_SCHEMA_VERSION
from ._json_contracts import reject_unknown


MAX_TRACE_ACTIONS = 32


@dataclass(frozen=True)
class TraceAction:
    command: str

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("trace action requires non-empty command text")

    def as_dict(self) -> dict[str, Any]:
        return {"command": self.command}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TraceAction":
        if not isinstance(payload, Mapping):
            raise ValueError("trace action must be a JSON object")
        reject_unknown(payload, {"command"}, "trace action")
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("trace action requires non-empty command text")
        return cls(command=command)


@dataclass(frozen=True)
class ActionTrace:
    actions: tuple[TraceAction, ...]

    def __post_init__(self) -> None:
        if len(self.actions) > MAX_TRACE_ACTIONS:
            raise ValueError(f"trace maximum is {MAX_TRACE_ACTIONS} actions")
        if any(not isinstance(action, TraceAction) for action in self.actions):
            raise ValueError("trace actions must be TraceAction values")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": ACTION_TRACE_SCHEMA_VERSION,
            "actions": [action.as_dict() for action in self.actions],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ActionTrace | None":
        if payload is None:
            return None
        if not isinstance(payload, Mapping):
            raise ValueError("trace must be a JSON object")
        reject_unknown(payload, {"schema_version", "actions"}, "action trace")
        schema_version = payload.get("schema_version")
        if schema_version is not None and schema_version != ACTION_TRACE_SCHEMA_VERSION:
            raise ValueError(f"unsupported action trace schema: {schema_version!r}")
        raw_actions = payload.get("actions")
        if not isinstance(raw_actions, list):
            raise ValueError("trace.actions must be an array")
        if len(raw_actions) > MAX_TRACE_ACTIONS:
            raise ValueError(
                f"trace contains {len(raw_actions)} actions; maximum is {MAX_TRACE_ACTIONS}"
            )
        return cls(actions=tuple(TraceAction.from_dict(action) for action in raw_actions))
