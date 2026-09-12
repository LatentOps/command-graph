"""Model-free HTTP fixture and transport-overhead evaluation."""

from __future__ import annotations

import argparse
import http.client
import json
import math
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from statistics import median
from typing import Any

from .agent import AgentGate
from .api import Ordin
from .mcp_http import MCPHTTPConfig, MCPHTTPServer, MCP_HTTP_PROTOCOL
from .mcp_proxy import APPROVAL_REQUIRED_CODE, BLOCKED_CODE
from .tool_calls import ToolResourceBinding, ToolSemanticRule, ToolSemanticsRegistry


def create_http_fixture(
    *, port: int = 0, delay: float = 0.0
) -> tuple[ThreadingHTTPServer, dict[str, Any]]:
    state: dict[str, Any] = {"calls": 0, "upstream_ns": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def do_POST(self) -> None:
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            method = request.get("method")
            result: dict[str, Any]
            if method == "initialize":
                result = {
                    "protocolVersion": MCP_HTTP_PROTOCOL,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "ordin-http-fixture", "version": "1"},
                }
            elif method == "tools/list":
                result = {
                    "tools": [
                        {
                            "name": "read_file",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"path": {"type": "string"}},
                            },
                        },
                        {
                            "name": "shell",
                            "inputSchema": {
                                "type": "object",
                                "properties": {"command": {"type": "string"}},
                            },
                        },
                    ]
                }
            elif method == "tools/call":
                start = time.perf_counter_ns()
                if delay:
                    time.sleep(delay)
                result = {
                    "content": [{"type": "text", "text": "fixture result; no command executed"}]
                }
                with lock:
                    state["calls"] += 1
                    state["upstream_ns"] += time.perf_counter_ns() - start
            else:
                self.send_response(202)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            raw = json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            self.send_response(405)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_DELETE(self) -> None:
            self.send_response(204)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return ThreadingHTTPServer(("127.0.0.1", port), Handler), state


@dataclass(frozen=True)
class HTTPTransportEvaluation:
    checks: tuple[dict[str, Any], ...]
    core_ns: tuple[int, ...] = ()
    round_trip_ns: tuple[int, ...] = ()
    upstream_ns: tuple[int, ...] = ()

    @property
    def errors(self) -> list[str]:
        return [
            f"HTTP transport {check['id']} failed" for check in self.checks if not check["passed"]
        ]

    def as_dict(self) -> dict[str, Any]:
        overhead = [
            max(0, total - core - upstream)
            for total, core, upstream in zip(self.round_trip_ns, self.core_ns, self.upstream_ns)
        ]
        ms = lambda values: round(median(values) / 1_000_000, 4) if values else 0.0
        return {
            "checks": list(self.checks),
            "errors": self.errors,
            "network_scope": "loopback only",
            "model_inference": False,
            "latency_ms": {
                "core_review_p50": ms(self.core_ns),
                "http_round_trip_p50": ms(self.round_trip_ns),
                "upstream_work_when_forwarded_p50": ms(
                    [value for value in self.upstream_ns if value > 0]
                ),
                "transport_and_session_overhead_p50": ms(overhead),
            },
        }


def run_http_transport_evaluation(
    *, repetitions: int = 1, upstream_delay: float = 0.0
) -> HTTPTransportEvaluation:
    if (
        isinstance(repetitions, bool)
        or not isinstance(repetitions, int)
        or not 1 <= repetitions <= 100
    ):
        raise ValueError("HTTP evaluation repetitions must be between 1 and 100")
    if not math.isfinite(upstream_delay) or not 0 <= upstream_delay <= 1:
        raise ValueError("HTTP fixture delay must be between 0 and 1 second")
    servers: list[ThreadingHTTPServer] = []
    threads = []
    checks: list[dict[str, Any]] = []
    core_samples: list[int] = []
    total_samples: list[int] = []
    upstream_samples: list[int] = []
    try:
        upstream, state = create_http_fixture(delay=upstream_delay)
        servers.append(upstream)
        registry = ToolSemanticsRegistry(
            "http-evaluation",
            "1",
            (
                ToolSemanticRule(
                    id="read",
                    kind="mcp",
                    server="fixture",
                    tool="read_file",
                    effects=("filesystem.read",),
                    resources=(ToolResourceBinding(argument="path", type="path"),),
                ),
            ),
        )
        gate = AgentGate(Ordin(tool_semantics=registry))
        proxy = MCPHTTPServer(
            MCPHTTPConfig("fixture", f"http://127.0.0.1:{upstream.server_port}/mcp", port=0),
            gate=gate,
            shell_tools=frozenset({"shell"}),
        )
        servers.append(proxy)
        for server in servers:
            thread = threading.Thread(
                target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
            )
            thread.start()
            threads.append(thread)

        def post(
            payload: dict[str, Any], token: str | None = None
        ) -> tuple[int, str | None, dict[str, Any]]:
            connection = http.client.HTTPConnection("127.0.0.1", proxy.server_port, timeout=5)
            headers = {
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "MCP-Protocol-Version": MCP_HTTP_PROTOCOL,
            }
            if token:
                headers["MCP-Session-Id"] = token
            try:
                connection.request("POST", "/mcp", body=json.dumps(payload), headers=headers)
                response = connection.getresponse()
                return (
                    response.status,
                    response.getheader("MCP-Session-Id"),
                    json.loads(response.read()),
                )
            finally:
                connection.close()

        status, token, _ = post(
            {
                "jsonrpc": "2.0",
                "id": "init",
                "method": "initialize",
                "params": {"protocolVersion": MCP_HTTP_PROTOCOL},
            }
        )
        checks.append({"id": "initialize", "passed": status == 200 and bool(token)})
        for index, (name, arguments, expected) in enumerate(
            (
                ("read_file", {"path": "README.md"}, "result"),
                ("unknown", {}, APPROVAL_REQUIRED_CODE),
                ("shell", {"command": "rm -rf /"}, BLOCKED_CODE),
            )
        ):
            passed = True
            for repetition in range(repetitions):
                request_id = index * repetitions + repetition + 1
                from .adapters import MCPAdapter

                action = MCPAdapter("fixture", shell_tools=frozenset({"shell"})).adapt(
                    name, arguments, action_id=f"http-eval-{request_id}"
                )
                start = time.perf_counter_ns()
                gate.evaluate_action(action)
                core_samples.append(time.perf_counter_ns() - start)
                previous = state["upstream_ns"]
                start = time.perf_counter_ns()
                status, _, reply = post(
                    {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                    token,
                )
                total_samples.append(time.perf_counter_ns() - start)
                upstream_samples.append(state["upstream_ns"] - previous)
                passed = (
                    passed
                    and status == 200
                    and (
                        "result" in reply
                        if expected == "result"
                        else reply.get("error", {}).get("code") == expected
                    )
                )
            checks.append({"id": name, "passed": passed})
        checks.append(
            {"id": "blocked_calls_never_reach_upstream", "passed": state["calls"] == repetitions}
        )
    except (OSError, ValueError, http.client.HTTPException):
        checks.append({"id": "fixture_available", "passed": False})
    finally:
        for server, thread in zip(servers, threads):
            server.shutdown()
            thread.join(timeout=2)
        for server in servers:
            server.server_close()
    return HTTPTransportEvaluation(
        tuple(checks), tuple(core_samples), tuple(total_samples), tuple(upstream_samples)
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the local non-executing MCP HTTP fixture")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    server, _ = create_http_fixture(port=args.port)
    print(json.dumps({"url": f"http://127.0.0.1:{server.server_port}/mcp"}), flush=True)
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
