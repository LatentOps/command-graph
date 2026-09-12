"""Bounded, local MCP discovery and reviewable semantics setup."""

from __future__ import annotations

import json
import math
import os
import queue
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import __version__
from .mcp_contracts import (
    MAX_CONTRACT_BYTES,
    MAX_CONTRACT_TOOLS,
    MCPContractLock,
    canonical_json,
    canonical_tool_contract,
    load_contract_json,
    semantics_binding_digest,
    tool_contract_digest,
)
from .mcp_proxy import _parse_jsonrpc_line, _request_id_key
from .schema import validate_named_schema
from .tool_calls import CompiledToolSemanticsRegistry, ToolSemanticsRegistry


MCP_INVENTORY_SCHEMA_VERSION = "ordin.mcp_inventory.v1"
_SECRET_PREFIXES = ("sk-", "ghp_", "github_pat_", "Bearer ", "AKIA")


def _sensitive_name(name: str) -> bool:
    compact = "".join(character for character in name.casefold() if character.isalnum())
    return compact.endswith(
        (
            "password",
            "passwd",
            "token",
            "secret",
            "apikey",
            "authorization",
            "cookie",
            "privatekey",
            "credential",
            "credentials",
        )
    )


def _safe_contract(value: Any, *, schema: bool = False, sensitive: bool = False) -> None:
    """Reject common credential literals, without treating schema field names as values."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            private = _sensitive_name(key)
            if (
                schema
                and key in {"properties", "patternProperties", "$defs", "definitions"}
                and isinstance(item, Mapping)
            ):
                for name, child in item.items():
                    private_field = _sensitive_name(name)
                    _safe_contract(child, schema=True, sensitive=private_field)
            elif key in {"inputSchema", "outputSchema"}:
                _safe_contract(item, schema=True)
            else:
                if item and (
                    (not schema and private)
                    or (schema and sensitive and key in {"default", "enum", "const"})
                ):
                    raise ValueError(
                        "tool metadata contains a potentially sensitive value; inspection output withheld"
                    )
                _safe_contract(item, schema=schema, sensitive=sensitive)
    elif isinstance(value, list):
        for item in value:
            _safe_contract(item, schema=schema, sensitive=sensitive)
    elif isinstance(value, str) and (
        value.startswith(_SECRET_PREFIXES) or "-----BEGIN " in value and "PRIVATE KEY-----" in value
    ):
        raise ValueError(
            "tool metadata contains a potentially sensitive literal; inspection output withheld"
        )


def inspect_mcp(server_id: str, command: Sequence[str], *, timeout: float = 10.0) -> dict[str, Any]:
    if not isinstance(server_id, str) or not server_id.strip() or len(server_id) > 256:
        raise ValueError("inspection requires an exact bounded server identity")
    if (
        not command
        or len(command) > 128
        or any(not isinstance(part, str) or not part or len(part) > 4096 for part in command)
    ):
        raise ValueError("inspection requires a bounded explicit server command")
    if not math.isfinite(timeout) or not 0 < timeout <= 300:
        raise ValueError("inspection timeout must be finite and between 0 and 300 seconds")
    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
        start_new_session=os.name == "posix",
    )
    assert process.stdin is not None and process.stdout is not None
    stdin, stdout = process.stdin, process.stdout
    incoming: queue.Queue[bytes | Exception | None] = queue.Queue(maxsize=1)
    outgoing: queue.Queue[bytes] = queue.Queue(maxsize=1)
    stopped = threading.Event()
    deadline = time.monotonic() + timeout

    def publish(value: bytes | Exception | None) -> None:
        while not stopped.is_set():
            try:
                incoming.put(value, timeout=0.05)
                return
            except queue.Full:
                continue

    def read() -> None:
        try:
            buffered = bytearray()
            while not stopped.is_set():
                chunk = stdout.read(65536)
                if not chunk:
                    if buffered:
                        publish(bytes(buffered))
                    publish(None)
                    return
                buffered.extend(chunk)
                while (boundary := buffered.find(b"\n")) >= 0:
                    if boundary + 1 > MAX_CONTRACT_BYTES:
                        raise ValueError("MCP inspection response exceeds byte limit")
                    publish(bytes(buffered[: boundary + 1]))
                    del buffered[: boundary + 1]
                if len(buffered) > MAX_CONTRACT_BYTES:
                    raise ValueError("MCP inspection response exceeds byte limit")
        except (OSError, ValueError) as exc:
            publish(exc)

    def write() -> None:
        try:
            while not stopped.is_set():
                try:
                    payload = outgoing.get(timeout=0.05)
                except queue.Empty:
                    continue
                view = memoryview(payload)
                while view and not stopped.is_set():
                    count = stdin.write(view)
                    if count is None or count <= 0:
                        raise OSError("MCP inspection write failed")
                    view = view[count:]
        except (OSError, ValueError) as exc:
            publish(exc)

    threads = [
        threading.Thread(target=read, daemon=True),
        threading.Thread(target=write, daemon=True),
    ]
    for thread in threads:
        thread.start()

    def remaining() -> float:
        value = deadline - time.monotonic()
        if value <= 0:
            raise ValueError("MCP inspection timed out")
        return value

    def send(payload: Mapping[str, Any]) -> None:
        try:
            outgoing.put(
                json.dumps(dict(payload), separators=(",", ":")).encode() + b"\n",
                timeout=remaining(),
            )
        except queue.Full as exc:
            raise ValueError("MCP inspection server stopped reading requests") from exc

    messages = 0

    def exchange(request_id: str, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        nonlocal messages
        send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)})
        while True:
            try:
                raw = incoming.get(timeout=remaining())
            except queue.Empty as exc:
                raise ValueError("MCP inspection timed out") from exc
            if raw is None or isinstance(raw, Exception):
                raise ValueError("MCP inspection server ended or sent an oversized response")
            messages += 1
            if messages > 512:
                raise ValueError("MCP inspection exceeded its message limit")
            message = _parse_jsonrpc_line(raw)
            if message.get("jsonrpc") != "2.0":
                raise ValueError("invalid inspection JSON-RPC version")
            if "method" in message:
                # Discovery never performs a tool call, sampling request, or
                # other server-initiated client action.
                if "id" in message:
                    key = _request_id_key(message["id"])
                    if key is None:
                        raise ValueError("invalid server request identity during inspection")
                    send(
                        {
                            "jsonrpc": "2.0",
                            "id": key,
                            "error": {
                                "code": -32601,
                                "message": "inspection does not perform client-side actions",
                            },
                        }
                    )
                continue
            if (
                message.get("id") != request_id
                or "error" in message
                or not isinstance(message.get("result"), Mapping)
            ):
                raise ValueError("MCP inspection received an uncorrelated or unsuccessful response")
            return message["result"]

    try:
        initialized = exchange(
            "ordin-inspect-init",
            "initialize",
            {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "ordin-inspect", "version": __version__},
            },
        )
        if initialized.get("protocolVersion") != "2025-11-25":
            raise ValueError("inspection requires MCP protocol 2025-11-25")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools: list[dict[str, Any]] = []
        seen: set[str] = set()
        cursors: set[str] = set()
        cursor = None
        for page in range(MAX_CONTRACT_TOOLS):
            result = exchange(
                f"ordin-inspect-{page}", "tools/list", {"cursor": cursor} if cursor else {}
            )
            values = result.get("tools")
            if not isinstance(values, list):
                raise ValueError("tools/list inspection requires a tools array")
            for value in values:
                contract = canonical_tool_contract(value)
                _safe_contract(contract)
                name = contract["name"]
                if name in seen or len(tools) >= MAX_CONTRACT_TOOLS:
                    raise ValueError("duplicate or excessive tool identities during inspection")
                seen.add(name)
                tools.append(contract)
            cursor = result.get("nextCursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or not cursor or len(cursor) > 4096 or cursor in cursors:
                raise ValueError("invalid inspection pagination cursor")
            cursors.add(cursor)
        else:
            raise ValueError("inspection exceeds page limit")
        inventory = {
            "schema_version": MCP_INVENTORY_SCHEMA_VERSION,
            "server_id": server_id,
            "protocol_revision": "2025-11-25",
            "tools": tools,
        }
        canonical_json(inventory)
        return inventory
    finally:
        stopped.set()
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        elif process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait(timeout=1)
        for thread in threads:
            thread.join(timeout=1)
        stdin.close()
        stdout.close()


def load_inventory(path: str | Path) -> dict[str, Any]:
    payload = dict(load_contract_json(path))
    if validate_named_schema("mcp_inventory", payload):
        raise ValueError("invalid MCP inventory schema")
    names: set[str] = set()
    for tool in payload["tools"]:
        canonical_tool_contract(tool)
        _safe_contract(tool)
        if tool["name"] in names:
            raise ValueError("duplicate tool identity in MCP inventory")
        names.add(tool["name"])
    return payload


def argument_paths(schema: Mapping[str, Any], prefix: tuple[str, ...] = ()) -> list[str]:
    paths: list[str] = []
    if len(prefix) >= 16:
        return paths
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return paths
    for name, child in properties.items():
        if not isinstance(name, str) or "." in name or not name or not isinstance(child, Mapping):
            continue
        path = (*prefix, name)
        if child.get("type") == "string":
            paths.append(".".join(path))
        elif child.get("type") == "object":
            paths.extend(argument_paths(child, path))
    return sorted(paths)


def write_new_json(path: str | Path, payload: Mapping[str, Any]) -> None:
    canonical_json(payload)
    text = json.dumps(dict(payload), indent=2, ensure_ascii=False) + "\n"
    if len(text.encode("utf-8")) > MAX_CONTRACT_BYTES:
        raise ValueError("formatted MCP setup file exceeds byte limit")
    target = Path(path)
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def scaffold(
    inventory: Mapping[str, Any], *, shell_tools: frozenset[str] = frozenset()
) -> tuple[dict[str, Any], dict[str, Any]]:
    registry = ToolSemanticsRegistry("ordin.mcp.draft", "draft-1", ())
    known = {tool["name"]: tool for tool in inventory["tools"]}
    if not shell_tools.issubset(known):
        raise ValueError("explicit shell mapping names a tool absent from discovery")
    for name in shell_tools:
        if "command" not in argument_paths(known[name]["inputSchema"]):
            raise ValueError("explicit shell mappings require a declared string command argument")
    review = {
        "server_id": inventory["server_id"],
        "requires_review": True,
        "shell_tools": sorted(shell_tools),
        "tools": [
            {
                "server": inventory["server_id"],
                "tool": tool["name"],
                "contract_digest": tool_contract_digest(tool),
                "argument_paths": argument_paths(tool["inputSchema"]),
                "effects": None,
                "resources": None,
                "instruction": "Supply reviewed effects and resource bindings; discovery grants no permission.",
            }
            for tool in inventory["tools"]
        ],
    }
    return registry.as_dict(), review


def validate_semantics(
    registry: CompiledToolSemanticsRegistry,
    inventory: Mapping[str, Any],
    *,
    shell_tools: frozenset[str] = frozenset(),
    lock: MCPContractLock | None = None,
) -> dict[str, Any]:
    server = inventory["server_id"]
    live = {tool["name"]: tool for tool in inventory["tools"]}
    rules = {
        rule.tool: rule
        for rule in registry.registry.rules
        if rule.kind == "mcp" and rule.server == server
    }
    findings = []
    pinned_names = {tool for scope, tool in lock.pins if scope == server} if lock else set()
    for name in sorted(set(live) | set(rules) | set(shell_tools) | pinned_names):
        codes = []
        contract = live.get(name)
        rule = rules.get(name)
        observed_digest = tool_contract_digest(contract) if contract is not None else None
        if contract is None:
            codes.append("tool_removed")
        elif rule is None and name not in shell_tools:
            codes.append("semantics_missing")
        if contract is not None:
            paths = argument_paths(contract["inputSchema"])
            if rule is not None and any(
                binding.argument not in paths for binding in rule.resources
            ):
                codes.append("resource_binding_unresolved")
            if name in shell_tools and "command" not in paths:
                codes.append("shell_contract_unresolved")
            if lock is not None:
                expected = lock.pins.get((server, name))
                if expected is None:
                    codes.append("contract_pin_missing")
                elif expected != observed_digest:
                    codes.append("contract_changed")
        findings.append(
            {
                "server": server,
                "tool": name,
                "codes": codes,
                "contract_digest": observed_digest,
            }
        )
    errors = [f"{item['tool']}: {code}" for item in findings for code in item["codes"]]
    if lock is not None and lock.semantics_digest != semantics_binding_digest(
        registry, shell_tools
    ):
        errors.append("semantics_binding_changed")
    if not rules and not shell_tools and registry.registry.rules:
        errors.append("exact_server_identity_mismatch")
    return {"ok": not errors, "server_id": server, "tools": findings, "errors": errors}
