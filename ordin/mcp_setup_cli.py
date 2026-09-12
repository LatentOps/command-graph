from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Sequence

from .mcp_contracts import (
    MCPContractLock,
    load_contract_json,
    semantics_binding_digest,
    tool_contract_digest,
)
from .mcp_inspection import (
    inspect_mcp,
    load_inventory,
    scaffold,
    validate_semantics,
    write_new_json,
)
from .tool_calls import load_tool_semantics


def mcp_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ordin mcp")
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect", help="Collect tools/list metadata without calling tools")
    inspect.add_argument("--server-id", required=True)
    inspect.add_argument("--timeout", type=float, default=10)
    inspect.add_argument("--output", type=Path)
    inspect.add_argument("--json", action="store_true")
    inspect.add_argument("server_command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.server_command[1:] if args.server_command[:1] == ["--"] else args.server_command
    try:
        inventory = inspect_mcp(args.server_id, command, timeout=args.timeout)
        if args.output:
            write_new_json(args.output, inventory)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "tools": len(inventory["tools"]),
                        "output": str(args.output),
                        "trusted": False,
                    }
                )
            )
        else:
            print(json.dumps(inventory, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": "mcp_inspection_failed", "message": str(exc)}))
        return 2


def semantics_main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ordin semantics")
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser(
        "scaffold", help="Create empty trusted semantics and a separate review worksheet"
    )
    draft.add_argument("inventory", type=Path)
    draft.add_argument("--output", type=Path, required=True)
    draft.add_argument("--shell-tool", action="append", default=[])
    draft.add_argument("--json", action="store_true")
    for name in ("validate", "diff", "lock"):
        command = commands.add_parser(name)
        command.add_argument("semantics", type=Path)
        command.add_argument("--inventory", type=Path)
        command.add_argument("--server-id")
        command.add_argument("--timeout", type=float, default=10)
        command.add_argument("--contract-lock", type=Path)
        command.add_argument("--shell-tool", action="append", default=[])
        command.add_argument("--json", action="store_true")
        command.add_argument("--inspect-command", nargs=argparse.REMAINDER)
        if name == "lock":
            command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        shells = frozenset(args.shell_tool)
        if args.command == "scaffold":
            inventory = load_inventory(args.inventory)
            draft_registry, worksheet = scaffold(inventory, shell_tools=shells)
            review_path = args.output.with_suffix(".review.json")
            if args.output.exists() or review_path.exists():
                raise ValueError(
                    "scaffold outputs already exist; refusing to overwrite reviewed files"
                )
            write_new_json(args.output, draft_registry)
            write_new_json(review_path, worksheet)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "trusted_rules": 0,
                        "requires_review": True,
                        "semantics": str(args.output),
                        "review": str(review_path),
                        "shell_tools": sorted(shells),
                    }
                )
            )
            return 0
        registry = load_tool_semantics(args.semantics)
        if args.inventory and args.inspect_command:
            raise ValueError("choose an inventory file or live inspection, not both")
        if args.inspect_command:
            inventory = inspect_mcp(args.server_id, args.inspect_command, timeout=args.timeout)
        elif args.inventory:
            inventory = load_inventory(args.inventory)
            if args.server_id is not None and args.server_id != inventory["server_id"]:
                raise ValueError("exact server identity mismatch")
        elif args.command == "validate":
            if args.contract_lock or shells:
                raise ValueError("contract and shell mapping validation requires an inventory")
            print(
                json.dumps(
                    {
                        "ok": True,
                        "registry_id": registry.registry.registry_id,
                        "rules": len(registry.registry.rules),
                    }
                )
            )
            return 0
        else:
            raise ValueError("provide --inventory or --server-id with --inspect-command")
        lock = (
            MCPContractLock.from_dict(load_contract_json(args.contract_lock))
            if args.contract_lock
            else None
        )
        report = validate_semantics(registry, inventory, shell_tools=shells, lock=lock)
        if args.command == "lock":
            if not report["ok"]:
                print(json.dumps(report, indent=2))
                return 1
            server = inventory["server_id"]
            pinned = MCPContractLock(
                semantics_binding_digest(registry, shells),
                {(server, tool["name"]): tool_contract_digest(tool) for tool in inventory["tools"]},
                inventory["protocol_revision"],
            )
            write_new_json(args.output, pinned.as_dict())
            report["output"] = str(args.output)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": "invalid_semantics_setup", "message": str(exc)}))
        return 2
