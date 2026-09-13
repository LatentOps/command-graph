"""Discoverable setup, status, rollback, and non-executing smoke verification."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any, Sequence

from . import __version__
from .agent import AgentGate
from .api import Ordin
from .http_evaluation import run_http_transport_evaluation
from .mcp_proxy import MCPStdioSafetyProxy
from .setup import (
    MODULES,
    TARGETS,
    SetupError,
    apply,
    config_content,
    hook_environment,
    load_owned,
    plan,
    remove,
    status,
)
from .tool_calls import ToolSemanticRule, ToolSemanticsRegistry


def default_root(integration: str, supplied: Path | None) -> Path:
    return (
        (supplied or (Path.home() if integration == "codex" else Path.cwd()))
        .expanduser()
        .absolute()
    )


def smoke(root: Path, integration: str) -> dict[str, Any]:
    settings = load_owned(root, integration)
    details = []
    if integration in MODULES:
        with tempfile.TemporaryDirectory(prefix="ordin-setup-smoke-") as directory:
            temporary = Path(directory)
            private = temporary / ".ordin/private" / integration
            private.mkdir(mode=0o700, parents=True)
            env = {key: value for key, value in os.environ.items() if not key.startswith("ORDIN_")}
            env.update(hook_environment(settings, temporary))
            cursor = integration == "cursor"
            payload: dict[str, Any] = {
                "hook_event_name": "preToolUse" if cursor else "PreToolUse",
                "session_id": "setup-smoke",
                "conversation_id": "setup-smoke",
                "generation_id": "turn",
                "turn_id": "turn",
                "cursor_version": "1.7.2",
                "tool_use_id": "read",
                "tool_name": "Shell" if cursor else "Bash",
                "tool_input": {"command": "git status --short"},
                "cwd": "/workspace",
                "workspace_roots": ["/workspace"],
                "permission_mode": "default",
            }

            def invoke(mode: str, message: dict[str, Any]) -> dict[str, Any]:
                if os.name == "posix":
                    hooks = json.loads(config_content(settings, temporary))["hooks"]
                    group = hooks[message["hook_event_name"]][0]
                    launcher = group["command"] if cursor else group["hooks"][0]["command"]
                    invocation = ["/bin/sh", "-c", launcher]
                else:
                    invocation = [
                        settings["python"],
                        "-I",
                        "-m",
                        "ordin." + MODULES[integration],
                        mode,
                    ]
                result = subprocess.run(
                    invocation,
                    input=json.dumps(message),
                    text=True,
                    capture_output=True,
                    env=env,
                    cwd=temporary,
                    timeout=15,
                )
                if result.returncode != 0:
                    raise SetupError("installed_hook_smoke_failed")
                return json.loads(result.stdout) if result.stdout.strip() else {}

            invoke(
                "session-start",
                {**payload, "hook_event_name": "sessionStart" if cursor else "SessionStart"},
            )
            output = invoke("pre", payload)
            permission = (
                output.get("permission")
                if cursor
                else output.get("hookSpecificOutput", {}).get("permissionDecision")
            )
            if permission != "allow":
                raise SetupError("benign_hook_not_allowed")
            invoke(
                "post",
                {
                    **payload,
                    "hook_event_name": "postToolUse" if cursor else "PostToolUse",
                    "tool_output": '{"exitCode":0}',
                    "tool_response": {"exit_code": 0},
                },
            )
            invoke(
                "session-end",
                {**payload, "hook_event_name": "sessionEnd" if cursor else "SessionEnd"},
            )
            details.append("installed_hook_lifecycle")
    elif integration == "mcp-http":
        if run_http_transport_evaluation(repetitions=1).errors:
            raise SetupError("loopback_http_smoke_failed")
        details.append("loopback_http_fixture")
    elif integration == "mcp":
        gate = AgentGate(
            Ordin(
                tool_semantics=ToolSemanticsRegistry(
                    "setup-smoke",
                    "1",
                    (
                        ToolSemanticRule(
                            id="read",
                            kind="mcp",
                            server="fixture",
                            tool="read",
                            effects=("filesystem.read",),
                        ),
                    ),
                )
            )
        )
        proxy = MCPStdioSafetyProxy(server_id="fixture", gate=gate)
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "read", "arguments": {}},
        }
        allowed = proxy.process_client_message(payload)
        observation = proxy.observe_server_message({"jsonrpc": "2.0", "id": 1, "result": {}})
        denied = proxy.process_client_message(
            {**payload, "id": 2, "params": {"name": "unknown", "arguments": {}}}
        )
        if (
            not allowed.forward
            or observation is None
            or observation.action_id != allowed.action_id
            or denied.forward
        ):
            raise SetupError("local_mcp_fixture_failed")
        details.append("local_mcp_gate_and_observation")
    else:
        if os.name == "posix":
            shell = shutil.which(settings["shell"])
            if (
                shell is None
                or subprocess.run(
                    [shell, "-n", str(root / settings["config"])], capture_output=True, timeout=5
                ).returncode
            ):
                raise SetupError("shell_launcher_unavailable_or_invalid")
        if not AgentGate().evaluate("git status --short").may_execute:
            raise SetupError("shell_review_failed")
        details.append("benign_shell_review")
    return {
        "ok": True,
        "integration": integration,
        "checks": details,
        "tool_execution": False,
        "host_enablement_verified": False,
        "upstream_server_contacted": False,
        "friction_stage": "smoke",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ordin setup",
        description="Preview, create, verify and remove Ordin-owned integration configuration.",
    )
    commands = parser.add_subparsers(dest="operation", required=True)
    for integration in TARGETS:
        command = commands.add_parser(
            integration, help="Create new " + integration + " configuration"
        )
        command.add_argument("--root", type=Path)
        command.add_argument("--config", help="Relative new config path inside root")
        command.add_argument("--dry-run", action="store_true")
        command.add_argument("--json", action="store_true")
        command.add_argument(
            "--state", action="store_true", help="Enable persistent agent-hook history"
        )
        command.add_argument("--audit", action="store_true", help="Enable private redacted audit")
        command.add_argument(
            "--observations", action="store_true", help="Enable private post-action evidence"
        )
        if integration == "shell":
            command.add_argument("--shell", choices=("bash", "zsh"), default="bash")
        if integration.startswith("mcp"):
            command.add_argument("--server-id", required=True)
            command.add_argument("--semantics", type=Path)
            command.add_argument("--inventory", type=Path)
            command.add_argument("--contract-lock", type=Path)
            if integration == "mcp-http":
                command.add_argument("--upstream", required=True)
                command.add_argument("--port", type=int, default=8766)
            else:
                command.add_argument("server_command", nargs=argparse.REMAINDER)
    for name in ("status", "doctor", "remove", "smoke", "launch"):
        command = commands.add_parser(name)
        command.add_argument(
            "integration",
            nargs="?" if name in {"status", "doctor"} else None,
            choices=tuple(TARGETS),
        )
        command.add_argument("--root", type=Path)
        command.add_argument("--json", action="store_true")
        if name == "remove":
            command.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.operation in {"status", "doctor"}:
            names = [args.integration] if args.integration else list(TARGETS)
            results = [status(default_root(name, args.root), name) for name in names]
            output: dict[str, Any] = {
                "ok": all(
                    item["code"] in {"not_configured", "configured_host_enablement_unverified"}
                    for item in results
                ),
                "ordin_version": __version__,
                "integrations": results,
            }
        elif args.operation in {"remove", "smoke", "launch"}:
            root = default_root(args.integration, args.root)
            if args.operation == "remove":
                output = remove(root, args.integration, dry_run=args.dry_run)
            elif args.operation == "smoke":
                output = smoke(root, args.integration)
            else:
                if args.integration != "mcp-http":
                    raise SetupError("launch_is_for_http_profile_only")
                settings = load_owned(root, args.integration)
                config = json.loads(config_content(settings, root))
                return subprocess.call([config["command"], *config["args"]])
        else:
            integration = args.operation
            root = default_root(integration, args.root)
            server_command = getattr(args, "server_command", [])
            if server_command[:1] == ["--"]:
                server_command = server_command[1:]
            settings = {
                "integration": integration,
                "config": args.config or TARGETS[integration],
                "python": sys.executable,
                "state": args.state,
                "audit": args.audit,
                "observations": args.observations,
                "shell": getattr(args, "shell", "bash"),
                "server_id": getattr(args, "server_id", None),
                "command": server_command,
                "upstream": getattr(args, "upstream", None),
                "port": getattr(args, "port", 8766),
                **{
                    name: str(getattr(args, name).absolute()) if getattr(args, name, None) else None
                    for name in ("semantics", "inventory", "contract_lock")
                },
            }
            if args.dry_run:
                output = plan(settings, root)
            else:
                if os.name != "posix":
                    raise SetupError("installation_requires_supported_linux_or_macos")
                output = apply(settings, root)
            output["next_step"] = shlex.join(
                ["ordin", "setup", "smoke", integration, "--root", str(root)]
            )
            if integration.startswith("mcp") and not settings["semantics"]:
                output["review_handoff"] = [
                    "ordin mcp inspect --server-id ID --output inventory.json -- SERVER_COMMAND",
                    "ordin semantics scaffold inventory.json --output semantics.json",
                    "Review effects and resource bindings manually; discovery is not trust",
                    "ordin semantics validate semantics.json --inventory inventory.json",
                    "ordin semantics lock semantics.json --inventory inventory.json --output contract-lock.json",
                    "Remove the owned draft and rerun setup with --inventory, --semantics, and --contract-lock",
                ]
        print(json.dumps(output, indent=2, sort_keys=True))
        return 0 if output["ok"] else 1
    except (
        ValueError,
        OSError,
        TypeError,
        KeyError,
        RecursionError,
        subprocess.SubprocessError,
    ) as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "code": str(exc)
                    if isinstance(exc, SetupError)
                    else "invalid_or_unavailable_setup",
                    "friction_stage": "configuration",
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
