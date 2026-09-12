from __future__ import annotations

import argparse
import json
from typing import Sequence

from .mcp_contracts import (
    MCPContractLock,
    MCPContractObserver,
    load_contract_json,
    semantics_binding_digest,
    tool_contract_digest,
)
from .tool_calls import load_tool_semantics


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ordin contracts", description="Inspect and validate local reviewed MCP contract pins."
    )
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate", help="Validate a data-only reviewed lockfile")
    validate.add_argument("lock")
    validate.add_argument("--json", action="store_true")
    digest = commands.add_parser(
        "digest", help="Show observed contract digests without granting trust"
    )
    digest.add_argument("inventory", help="Local tools/list result JSON")
    digest.add_argument("--json", action="store_true")
    diff = commands.add_parser("diff", help="Compare reviewed pins with a local tools/list result")
    diff.add_argument("lock")
    diff.add_argument("inventory")
    diff.add_argument("--server-id", required=True)
    diff.add_argument("--semantics")
    diff.add_argument("--shell-tool", action="append", default=[])
    diff.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            lock = MCPContractLock.from_dict(load_contract_json(args.lock))
            output = {"ok": True, "pins": len(lock.pins), "semantics_digest": lock.semantics_digest}
        else:
            inventory = load_contract_json(args.inventory)
            tools = inventory.get("tools")
            if not isinstance(tools, list):
                raise ValueError("inventory must be a tools/list result object")
            if args.command == "digest":
                names: set[str] = set()
                observed = []
                if len(tools) > 256 or inventory.get("nextCursor") is not None:
                    raise ValueError("provide a complete bounded tool inventory")
                for tool in tools:
                    digest_value = tool_contract_digest(tool)
                    if tool["name"] in names:
                        raise ValueError("duplicate tool name in contract inventory")
                    names.add(tool["name"])
                    observed.append({"tool": tool["name"], "digest": digest_value})
                output = {"ok": True, "trusted": False, "contracts": observed}
            else:
                lock = MCPContractLock.from_dict(load_contract_json(args.lock))
                semantics = load_tool_semantics(args.semantics) if args.semantics else None
                observer = MCPContractObserver(
                    args.server_id,
                    lock,
                    semantics_binding_digest(semantics, frozenset(args.shell_tool)),
                )
                observer.request(1, {})
                observer.response(1, {"result": inventory})
                pinned = {tool for server, tool in lock.pins if server == args.server_id}
                checks = [observer.check(tool) for tool in sorted(pinned | set(observer.live))]
                output = {
                    "ok": bool(pinned)
                    and observer.status == "complete"
                    and all(check.verified for check in checks),
                    "server_pinned": bool(pinned),
                    "inventory_status": observer.status,
                    "contracts": [check.as_dict() for check in checks],
                }
        print(json.dumps(output, indent=2 if args.json else None, sort_keys=True))
        return 0 if output["ok"] else 1
    except (OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": "invalid_mcp_contract", "message": str(exc)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
