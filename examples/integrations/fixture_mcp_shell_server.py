"""Shell-capable contract fixture; records no credentials and executes no command."""

import json
import sys


def main():
    for line in sys.stdin:
        request = json.loads(line)
        method = request.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-11-25",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "ordin-shell-fixture", "version": "1"},
            }
        elif isinstance(method, str) and method.startswith("notifications/"):
            continue
        elif method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "execute",
                        "inputSchema": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    }
                ]
            }
        elif method == "tools/call":
            result = {
                "content": [
                    {"type": "text", "text": "Fixture only: no shell command was executed."}
                ]
            }
        else:
            result = {}
        print(json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": result}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
