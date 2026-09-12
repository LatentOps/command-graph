from __future__ import annotations

import json
import sys


def main() -> int:
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "read_note",
                        "description": "Return a deterministic local fixture note.",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "fixture note"}]}
        else:
            result = {}
        response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
        print(json.dumps(response, separators=(",", ":")), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
