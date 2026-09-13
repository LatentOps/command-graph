"""Previewable, owned-file setup; never merge or overwrite host configuration."""

from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import sys
import tempfile
from typing import Any, Iterator
from urllib.parse import urlsplit

from . import __version__
from ._json_contracts import load_configuration
from .mcp_contracts import MCPContractLock, load_contract_json
from .mcp_http import MCPHTTPConfig
from .mcp_inspection import load_inventory, validate_semantics
from .shell_integration import render_shell_init
from .tool_calls import load_tool_semantics


TARGETS = {
    "claude": ".claude/settings.local.json",
    "codex": ".codex/hooks.json",
    "cursor": ".cursor/hooks.json",
    "mcp": ".mcp.json",
    "mcp-http": ".ordin/mcp-http.json",
    "shell": ".ordin/shell-init.sh",
}
MODULES = {"claude": "claude_code", "codex": "codex", "cursor": "cursor"}
SETTINGS = {
    "integration",
    "config",
    "python",
    "state",
    "audit",
    "observations",
    "shell",
    "server_id",
    "command",
    "upstream",
    "port",
    "semantics",
    "inventory",
    "contract_lock",
}


class SetupError(ValueError):
    """A stable diagnostic code that does not include private configuration."""


def encoded(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _guard(path: Path, root: Path, *, private: bool = False) -> None:
    if not path.is_relative_to(root):
        raise SetupError("path_outside_setup_root")
    for item in (path, *path.parents):
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                raise SetupError("symlink_or_reparse_path")
            if item.is_relative_to(root):
                if os.name == "posix" and (info.st_uid != os.geteuid() or info.st_mode & 0o022):
                    raise SetupError("unsafe_owner_or_write_permissions")
                if item == path and private and os.name == "posix" and info.st_mode & 0o077:
                    raise SetupError("private_path_permissions_required")
                if stat.S_ISREG(info.st_mode) and info.st_nlink != 1:
                    raise SetupError("multiply_linked_file")


def _relative(value: str) -> str:
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {"..", "."} for part in path.parts):
        raise SetupError("config_requires_relative_path")
    if len(value) > 4096 or "\x00" in value:
        raise SetupError("invalid_config_path")
    return path.as_posix()


def validate_settings(settings: dict[str, Any], *, validate_contracts: bool = True) -> None:
    if set(settings) != SETTINGS or settings["integration"] not in TARGETS:
        raise SetupError("invalid_setup_settings")
    relative = _relative(settings["config"])
    if any(
        relative == directory or relative.startswith(directory + "/")
        for directory in (".ordin/setup", ".ordin/private")
    ):
        raise SetupError("config_conflicts_with_setup_receipt")
    if not isinstance(settings["python"], str) or not Path(settings["python"]).is_absolute():
        raise SetupError("absolute_python_required")
    for key in ("state", "audit", "observations"):
        if not isinstance(settings[key], bool):
            raise SetupError("invalid_evidence_option")
    if settings["shell"] not in {"bash", "zsh"}:
        raise SetupError("unsupported_shell")
    command = settings["command"]
    if (
        not isinstance(command, list)
        or len(command) > 64
        or any(
            not isinstance(arg, str) or not arg or len(arg) > 4096 or any(ord(c) < 32 for c in arg)
            for arg in command
        )
    ):
        raise SetupError("invalid_server_command")
    if any(
        re.search(
            r"(?i)(password|passwd|token|api[_-]?key|authorization|cookie|secret|://|=|\bsk-|\bghp_)",
            arg,
        )
        for arg in command
    ):
        raise SetupError("credential_arguments_require_manual_configuration")
    integration = settings["integration"]
    if integration not in MODULES and settings["state"]:
        raise SetupError("persistent_state_option_requires_agent_hooks")
    if integration in {"mcp", "mcp-http"}:
        if not isinstance(settings["server_id"], str) or not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,128}", settings["server_id"]
        ):
            raise SetupError("invalid_server_identity")
        if integration == "mcp" and not command:
            raise SetupError("server_command_required")
        if integration == "mcp-http":
            if type(settings["port"]) is not int or not 1 <= settings["port"] <= 65535:
                raise SetupError("http_port_requires_fixed_nonzero_port")
            if command or not isinstance(settings["upstream"], str):
                raise SetupError("http_upstream_required")
            url = urlsplit(settings["upstream"])
            if url.query or url.fragment or url.username or url.password:
                raise SetupError("credential_url_requires_manual_configuration")
            MCPHTTPConfig(settings["server_id"], settings["upstream"], port=settings["port"])
        supplied = [bool(settings[key]) for key in ("semantics", "inventory", "contract_lock")]
        if any(supplied) and not all(supplied):
            raise SetupError("reviewed_inventory_semantics_and_lock_required_together")
        if all(supplied) and validate_contracts:
            registry = load_tool_semantics(settings["semantics"])
            inventory = load_inventory(settings["inventory"])
            lock = MCPContractLock.from_dict(load_contract_json(settings["contract_lock"]))
            if (
                inventory["server_id"] != settings["server_id"]
                or not validate_semantics(registry, inventory, lock=lock)["ok"]
            ):
                raise SetupError("mcp_contract_validation_failed")
    elif (
        command
        or settings["upstream"]
        or any(settings[key] for key in ("semantics", "inventory", "contract_lock", "server_id"))
    ):
        raise SetupError("mcp_options_require_mcp_integration")
    if integration == "shell" and any(settings[key] for key in ("state", "audit", "observations")):
        raise SetupError("shell_evidence_requires_manual_configuration")


def hook_environment(settings: dict[str, Any], root: Path) -> dict[str, str]:
    prefix = "ORDIN_" + settings["integration"].upper()
    directory = root / ".ordin/private" / settings["integration"]
    return {
        prefix + "_" + key: str(directory / filename) if enabled else ""
        for key, filename, enabled in (
            ("STATE", "state.db", settings["state"]),
            ("AUDIT", "audit.jsonl", settings["audit"]),
            (
                "OBSERVATIONS",
                "observations.jsonl",
                settings["observations"] and settings["integration"] != "cursor",
            ),
            ("TRACE", "trace.db", settings["observations"] and settings["integration"] == "cursor"),
        )
    } | {prefix + "_TRACE_RAW": "0"}


def config_content(
    settings: dict[str, Any], root: Path, *, validate_contracts: bool = True
) -> bytes:
    validate_settings(settings, validate_contracts=validate_contracts)
    integration = settings["integration"]
    python = settings["python"]
    if integration in MODULES:
        events = {
            "SessionStart": "session-start",
            "PreToolUse": "pre",
            "PostToolUse": "post",
            "SessionEnd": "session-end",
        }
        if integration == "codex":
            events["PermissionRequest"] = "permission"
        else:
            events["PostToolUseFailure"] = "post-failure"
        if integration == "cursor":
            events = {event[0].lower() + event[1:]: mode for event, mode in events.items()}
            events.update(subagentStart="subagent-start", subagentStop="subagent-stop")
        hooks = {}
        environment = " ".join(
            f"{key}={shlex.quote(value)}" for key, value in hook_environment(settings, root).items()
        )
        for event, mode in events.items():
            command = f"{environment} {shlex.quote(python)} -I -m ordin.{MODULES[integration]} {mode} || exit 2"
            handler = {"command": command, "timeout": 30}
            hooks[event] = (
                [handler]
                if integration == "cursor"
                else [{"hooks": [{"type": "command", **handler}]}]
            )
        return encoded(
            {"version": 1, "hooks": hooks} if integration == "cursor" else {"hooks": hooks}
        )
    if integration == "shell":
        return (
            render_shell_init(settings["shell"])
            .replace("command ordin", f"command {shlex.quote(python)} -I -m ordin")
            .encode()
        )
    args = [
        "-I",
        "-m",
        "ordin.mcp_proxy" if integration == "mcp" else "ordin.mcp_http",
        "--server-id",
        settings["server_id"],
    ]
    if settings["semantics"]:
        args.extend(
            ["--semantics", settings["semantics"], "--contract-lock", settings["contract_lock"]]
        )
    directory = root / ".ordin/private" / integration
    if settings["audit"]:
        args.extend(["--audit", str(directory / "audit.jsonl")])
    if settings["observations"]:
        args.extend(["--observations", str(directory / "observations.jsonl")])
    if integration == "mcp":
        args.extend(["--", *settings["command"]])
        return encoded({"mcpServers": {settings["server_id"]: {"command": python, "args": args}}})
    args.extend(["--upstream", settings["upstream"], "--port", str(settings["port"])])
    return encoded(
        {
            "command": python,
            "args": args,
            "client": {
                "mcpServers": {
                    settings["server_id"]: {"url": f"http://127.0.0.1:{settings['port']}/mcp"}
                }
            },
        }
    )


def _read(path: Path, root: Path) -> bytes:
    _guard(path, root)
    if not path.is_file() or path.stat().st_size > 1048576:
        raise SetupError("invalid_or_oversized_setup_file")
    return path.read_bytes()


def receipt_path(root: Path, integration: str) -> Path:
    if integration not in TARGETS:
        raise SetupError("unknown_integration")
    return root / ".ordin/setup" / (integration + ".json")


def planned_files(
    settings: dict[str, Any], root: Path, *, validate_contracts: bool = True
) -> dict[Path, bytes]:
    config = root / _relative(settings["config"])
    content = config_content(settings, root, validate_contracts=validate_contracts)
    receipt = {
        "schema_version": "ordin.setup_receipt.v1",
        "settings": settings,
        "config_sha256": digest(content),
    }
    return {receipt_path(root, settings["integration"]): encoded(receipt), config: content}


def plan(settings: dict[str, Any], root: Path) -> dict[str, Any]:
    files = planned_files(settings, root)
    receipt = receipt_path(root, settings["integration"])
    owned = receipt.exists() and _read(receipt, root) == files[receipt]
    changes = []
    for path, content in files.items():
        _guard(path, root)
        exists = path.exists()
        operation = (
            "unchanged"
            if exists and owned and _read(path, root) == content
            else "conflict"
            if exists
            else "create"
        )
        changes.append(
            {
                "path": str(path),
                "operation": operation,
                "content": content.decode(),
                "sha256": digest(content),
            }
        )
    return {
        "schema_version": "ordin.setup_plan.v1",
        "ok": all(item["operation"] != "conflict" for item in changes),
        "integration": settings["integration"],
        "changes": changes,
        "host_enablement_verified": False,
        "mcp_review_required": settings["integration"].startswith("mcp")
        and not settings["semantics"],
        "rollback": "ordin setup remove "
        + settings["integration"]
        + " --root "
        + shlex.quote(str(root)),
    }


@contextmanager
def _locked(root: Path) -> Iterator[None]:
    directory = root / ".ordin/setup"
    _guard(directory, root)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _guard(directory, root, private=True)
    lock = directory / ".lock"
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise SetupError("setup_locked_review_before_retry") from exc
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink()


def _write_new(path: Path, content: bytes, root: Path) -> None:
    _guard(path, root)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _guard(path.parent, root)
    fd, name = tempfile.mkstemp(prefix=".ordin-new-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic publication of a complete new file; an existing target always wins.
        os.link(temporary, path)
    finally:
        temporary.unlink()


def apply(settings: dict[str, Any], root: Path) -> dict[str, Any]:
    if not plan(settings, root)["ok"]:
        raise SetupError("existing_config_conflict_use_manual_merge_or_owned_removal")
    with _locked(root):
        preview = plan(settings, root)
        if not preview["ok"]:
            raise SetupError("existing_config_conflict_use_manual_merge_or_owned_removal")
        if any(settings[key] for key in ("state", "audit", "observations")):
            directory = root / ".ordin/private" / settings["integration"]
            _guard(directory, root)
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            _guard(directory, root, private=True)
        created = []
        try:
            for path, content in planned_files(settings, root).items():
                if not path.exists():
                    _write_new(path, content, root)
                    created.append((path, content))
        except (OSError, ValueError):
            for path, content in reversed(created):
                if path.exists() and _read(path, root) == content:
                    path.unlink()
            raise
        return {
            "ok": True,
            "integration": settings["integration"],
            "applied": [str(path) for path, _ in created],
            "idempotent": not created,
            "host_enablement_verified": False,
            "mcp_review_required": preview["mcp_review_required"],
            "rollback": preview["rollback"],
        }


def load_owned(root: Path, integration: str, *, validate_contracts: bool = True) -> dict[str, Any]:
    path = receipt_path(root, integration)
    _guard(path, root, private=True)
    receipt = load_configuration(path, label="setup receipt", maximum=1048576)
    if (
        set(receipt) != {"schema_version", "settings", "config_sha256"}
        or receipt["schema_version"] != "ordin.setup_receipt.v1"
        or not isinstance(receipt["settings"], dict)
    ):
        raise SetupError("invalid_setup_receipt")
    settings = receipt["settings"]
    if settings.get("integration") != integration:
        raise SetupError("setup_receipt_identity_mismatch")
    files = planned_files(settings, root, validate_contracts=validate_contracts)
    if files[path] != _read(path, root):
        raise SetupError("setup_receipt_changed")
    for target, content in files.items():
        if not target.exists() or _read(target, root) != content:
            raise SetupError("owned_configuration_missing_or_changed")
    return settings


def remove(root: Path, integration: str, *, dry_run: bool = False) -> dict[str, Any]:
    def perform() -> dict[str, Any]:
        # Rollback still works if a referenced inventory or lock has drifted.
        # Only the unchanged owned config and receipt are removed.
        settings = load_owned(root, integration, validate_contracts=False)
        paths = list(planned_files(settings, root, validate_contracts=False))
        if not dry_run:
            for path in reversed(paths):
                path.unlink()
        return {
            "ok": True,
            "integration": integration,
            "removed" if not dry_run else "would_remove": [str(path) for path in reversed(paths)],
            "evidence_retained": True,
        }

    if dry_run:
        return perform()
    with _locked(root):
        return perform()


def status(root: Path, integration: str) -> dict[str, Any]:
    try:
        receipt = receipt_path(root, integration)
        if not receipt.exists():
            target = root / TARGETS[integration]
            _guard(target, root)
            return {
                "integration": integration,
                "configured": False,
                "code": "foreign_configuration" if target.exists() else "not_configured",
            }
        settings = load_owned(root, integration)
        if not Path(settings["python"]).is_file():
            raise SetupError("configured_python_missing")
        if integration == "mcp" and not shutil.which(settings["command"][0]):
            raise SetupError("upstream_executable_missing")
        if (root / ".ordin/setup/.lock").exists():
            raise SetupError("setup_locked_review_before_retry")
        if any(settings[key] for key in ("state", "audit", "observations")):
            directory = root / ".ordin/private" / integration
            if not directory.is_dir():
                raise SetupError("private_evidence_directory_missing")
            _guard(directory, root, private=True)
            for name in ("state.db", "audit.jsonl", "observations.jsonl", "trace.db"):
                _guard(directory / name, root, private=True)
        return {
            "integration": integration,
            "configured": True,
            "code": "configured_host_enablement_unverified",
            "host_enablement_verified": False,
            "host_contract": "fixture_validated_host_version_unverified",
            "mcp_review_required": integration.startswith("mcp") and not settings["semantics"],
        }
    except (ValueError, OSError, TypeError, KeyError, RecursionError) as exc:
        return {
            "integration": integration,
            "configured": False,
            "code": str(exc)
            if isinstance(exc, SetupError)
            else "invalid_configuration_or_contract",
        }
