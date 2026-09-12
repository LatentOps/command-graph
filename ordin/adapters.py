from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .action import ActionEnvelope
from .context import ExecutionContext


MAX_ADAPTER_NAME_LENGTH = 128
MAX_TOOL_NAME_LENGTH = 256
MAX_SERVER_NAME_LENGTH = 256


def _required_text(value: str, *, name: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    # Identity matching must use the same text that the runtime will execute.
    if len(value) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return value


def _arguments(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("tool arguments must be a JSON object")
    # ActionEnvelope performs the shared bounded JSON/depth validation and copies
    # the mapping before it becomes part of a caller-visible action.
    return dict(value)


@dataclass(frozen=True)
class ToolCallAdapter:
    """Normalize caller-owned tool calls into conservative Ordin actions.

    By default a tool call remains a generic `tool.call`, which Ordin reviews as
    unknown until deterministic semantics are registered. Shell unwrapping is
    opt-in and exact-name based so adapter convenience cannot silently turn an
    arbitrary tool into trusted command execution semantics.
    """

    runtime: str
    shell_tools: frozenset[str] = field(default_factory=frozenset)
    command_argument: str = "command"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "runtime",
            _required_text(self.runtime, name="runtime", maximum=MAX_ADAPTER_NAME_LENGTH),
        )
        _required_text(
            self.command_argument,
            name="command argument",
            maximum=MAX_ADAPTER_NAME_LENGTH,
        )
        for tool in self.shell_tools:
            _required_text(tool, name="shell tool name", maximum=MAX_TOOL_NAME_LENGTH)

    def adapt(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        intent: str | None = None,
        context: ExecutionContext | None = None,
        action_id: str | None = None,
    ) -> ActionEnvelope:
        tool = _required_text(tool, name="tool name", maximum=MAX_TOOL_NAME_LENGTH)
        args = _arguments(arguments)
        if tool in self.shell_tools:
            command = args.get(self.command_argument)
            if not isinstance(command, str) or not command.strip():
                raise ValueError(
                    f"shell tool {tool!r} requires non-empty {self.command_argument!r} argument"
                )
            return ActionEnvelope(
                kind="shell",
                operation="execute",
                parameters={
                    "command": command,
                    "source_runtime": self.runtime,
                    "source_tool": tool,
                },
                intent=intent,
                context=context,
                action_id=action_id,
            )

        return ActionEnvelope(
            kind="tool",
            operation="call",
            parameters={
                "runtime": self.runtime,
                "tool": tool,
                "arguments": args,
            },
            intent=intent,
            context=context,
            action_id=action_id,
        )


@dataclass(frozen=True)
class MCPAdapter:
    """Normalize MCP tool calls without proxying, executing, or storing them."""

    server: str
    shell_tools: frozenset[str] = field(default_factory=frozenset)
    command_argument: str = "command"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "server",
            _required_text(self.server, name="MCP server", maximum=MAX_SERVER_NAME_LENGTH),
        )
        _required_text(
            self.command_argument,
            name="command argument",
            maximum=MAX_ADAPTER_NAME_LENGTH,
        )
        for tool in self.shell_tools:
            _required_text(tool, name="MCP shell tool name", maximum=MAX_TOOL_NAME_LENGTH)

    def adapt(
        self,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        intent: str | None = None,
        context: ExecutionContext | None = None,
        action_id: str | None = None,
    ) -> ActionEnvelope:
        tool = _required_text(tool, name="MCP tool", maximum=MAX_TOOL_NAME_LENGTH)
        args = _arguments(arguments)
        if tool in self.shell_tools:
            command = args.get(self.command_argument)
            if not isinstance(command, str) or not command.strip():
                raise ValueError(
                    f"MCP shell tool {tool!r} requires non-empty {self.command_argument!r} argument"
                )
            return ActionEnvelope(
                kind="shell",
                operation="execute",
                parameters={
                    "command": command,
                    "source_runtime": "mcp",
                    "source_server": self.server,
                    "source_tool": tool,
                },
                intent=intent,
                context=context,
                action_id=action_id,
            )

        return ActionEnvelope(
            kind="mcp",
            operation="call",
            parameters={
                "server": self.server,
                "tool": tool,
                "arguments": args,
            },
            intent=intent,
            context=context,
            action_id=action_id,
        )
