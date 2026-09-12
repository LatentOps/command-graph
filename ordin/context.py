from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Any, Mapping

from . import REVIEW_REQUEST_SCHEMA_VERSION
from .trace import ActionTrace
from ._json_contracts import reject_unknown


@dataclass(frozen=True)
class ExecutionContext:
    cwd: str | None = None
    shell: str | None = None
    euid: int | None = None
    interactive: bool | None = None
    repo_root: str | None = None
    agent: str | None = None

    def __post_init__(self) -> None:
        for name in ("cwd", "shell", "repo_root", "agent"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"context.{name} must be a string or null")
        if self.euid is not None and (
            isinstance(self.euid, bool) or not isinstance(self.euid, int) or self.euid < 0
        ):
            raise ValueError("context.euid must be a non-negative integer or null")
        if self.interactive is not None and not isinstance(self.interactive, bool):
            raise ValueError("context.interactive must be boolean or null")

    def as_dict(self) -> dict[str, Any]:
        return {
            "cwd": self.cwd,
            "shell": self.shell,
            "euid": self.euid,
            "interactive": self.interactive,
            "repo_root": self.repo_root,
            "agent": self.agent,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any] | None) -> "ExecutionContext | None":
        if payload is None:
            return None
        if not isinstance(payload, Mapping):
            raise ValueError("context must be a JSON object")
        reject_unknown(
            payload, {"cwd", "shell", "euid", "interactive", "repo_root", "agent"}, "context"
        )

        euid = payload.get("euid")
        if euid is not None and (isinstance(euid, bool) or not isinstance(euid, int)):
            raise ValueError("context.euid must be an integer or null")
        interactive = payload.get("interactive")
        if interactive is not None and not isinstance(interactive, bool):
            raise ValueError("context.interactive must be a boolean or null")

        string_fields = {}
        for key in ("cwd", "shell", "repo_root", "agent"):
            value = payload.get(key)
            if value is not None and not isinstance(value, str):
                raise ValueError(f"context.{key} must be a string or null")
            string_fields[key] = value

        return cls(
            cwd=string_fields["cwd"],
            shell=string_fields["shell"],
            euid=euid,
            interactive=interactive,
            repo_root=string_fields["repo_root"],
            agent=string_fields["agent"],
        )

    @property
    def is_elevated(self) -> bool | None:
        if self.euid is None:
            return None
        return self.euid == 0

    def resolve_path(self, target: str) -> str | None:
        if not target or target in {"filesystem.target", "<unknown>"}:
            return None
        if target.startswith("~"):
            return None
        if posixpath.isabs(target):
            return posixpath.normpath(target)
        if not self.cwd or not posixpath.isabs(self.cwd):
            return None
        return posixpath.normpath(posixpath.join(self.cwd, target))

    def path_within_repo(self, path: str) -> bool | None:
        if not self.repo_root or not posixpath.isabs(self.repo_root) or not posixpath.isabs(path):
            return None
        normalized_path = posixpath.normpath(path)
        normalized_root = posixpath.normpath(self.repo_root)
        try:
            return posixpath.commonpath([normalized_path, normalized_root]) == normalized_root
        except ValueError:
            return False


@dataclass(frozen=True)
class ReviewRequest:
    command: str
    intent: str | None = None
    context: ExecutionContext | None = None
    trace: ActionTrace | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("review request requires non-empty command text")
        if self.intent is not None and not isinstance(self.intent, str):
            raise ValueError("review request intent must be a string or null")
        if self.context is not None and not isinstance(self.context, ExecutionContext):
            raise ValueError("review request context must be ExecutionContext or null")
        if self.trace is not None and not isinstance(self.trace, ActionTrace):
            raise ValueError("review request trace must be ActionTrace or null")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REVIEW_REQUEST_SCHEMA_VERSION,
            "command": self.command,
            "intent": self.intent,
            "context": self.context.as_dict() if self.context else None,
            "trace": self.trace.as_dict() if self.trace else None,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ReviewRequest":
        if not isinstance(payload, Mapping):
            raise ValueError("review request must be a JSON object")
        reject_unknown(
            payload, {"schema_version", "command", "intent", "context", "trace"}, "review request"
        )
        schema_version = payload.get("schema_version")
        if schema_version is not None and schema_version != REVIEW_REQUEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported review request schema: {schema_version!r}")
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip():
            raise ValueError("review request requires non-empty command text")
        intent = payload.get("intent")
        if intent is not None and not isinstance(intent, str):
            raise ValueError("review request intent must be a string or null")
        return cls(
            command=command,
            intent=intent,
            context=ExecutionContext.from_dict(payload.get("context")),
            trace=ActionTrace.from_dict(payload.get("trace")),
        )
