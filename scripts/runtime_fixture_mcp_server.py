from __future__ import annotations

import json
import sys
from typing import Any, Mapping


def _response(message: Mapping[str, Any]) -> dict[str, Any]:
    request_id = message.get("id")
    method = message.get("method")
    if method == "tools/call":
        params = message.get("params")
        if not isinstance(params, Mapping):
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32602, "message": "invalid params"},
            }
        name = params.get("name")
        if name == "read_file":
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "content": [{"type": "text", "text": "synthetic runtime fixture response"}],
                    "isError": False,
                },
            }
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": -32601, "message": "unknown fixture tool"},
        }
    if method == "tools/list":
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "result": {
                "tools": [
                    {
                        "name": "read_file",
                        "description": "Deterministic runtime evaluation fixture",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    }
                ]
            },
        }
    return {"jsonrpc": "2.0", "id": request_id, "result": {}}


def main() -> int:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            print(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": None,
                        "error": {"code": -32700, "message": "parse error"},
                    },
                    separators=(",", ":"),
                ),
                flush=True,
            )
            continue
        if not isinstance(payload, Mapping):
            continue
        print(
            json.dumps(_response(payload), sort_keys=True, separators=(",", ":")),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
