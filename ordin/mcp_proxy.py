from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import queue
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, IO, Mapping, Sequence

from .action_policy import load_action_policy
from .adapters import MCPAdapter
from .agent import AgentDecision, AgentGate
from .api import Ordin
from .audit import JsonlAuditSink
from .context import ExecutionContext
from .execution import ActionObservation
from .policy import FailThreshold, ReviewPolicy
from .tool_calls import load_tool_semantics


MCP_PROXY_RUNTIME = "mcp-proxy"
MAX_MCP_MESSAGE_BYTES = 10 * 1024 * 1024
MAX_MCP_JSON_DEPTH = 64
MAX_LOCAL_EVENT_BYTES = 1_048_576
MAX_PROXY_REASON_LENGTH = 4096
APPROVAL_REQUIRED_CODE = -32040
BLOCKED_CODE = -32041
INVALID_REQUEST_CODE = -32600
PARSE_ERROR_CODE = -32700


def _request_id_key(value: Any) -> str | int | float | None:
    if isinstance(value, bool) or value is None or not isinstance(value, (str, int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    # JSON has one Number type. Native numeric equality matches 1 with 1.0
    # without conflating string IDs or rounding large integer IDs to floats.
    return value


def _request_id_digest(value: Any) -> str:
    material = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _action_id(*, server_id: str, request_id: Any, tool: str, sequence: int) -> str:
    material = json.dumps(
        [server_id, request_id, tool, sequence],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return "mcp:" + hashlib.sha256(material).hexdigest()


def _jsonrpc_error(
    request_id: Any,
    *,
    code: int,
    message: str,
    data: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": message[:MAX_PROXY_REASON_LENGTH],
        },
    }
    if data:
        payload["error"]["data"] = dict(data)
    return payload


def _decision_data(decision: AgentDecision) -> dict[str, Any]:
    review = decision.review
    reasons = getattr(review, "reasons", [])
    safe_reasons = [str(reason)[:MAX_PROXY_REASON_LENGTH] for reason in list(reasons)[:4]]
    data: dict[str, Any] = {
        "ordin": {
            "decision": review.decision,
            "risk": review.risk,
            "reasons": safe_reasons,
        }
    }
    action = getattr(review, "action", None)
    action_id = getattr(action, "action_id", None)
    if isinstance(action_id, str):
        data["ordin"]["action_id"] = action_id
    safer = getattr(review, "safer_next_step", None)
    if isinstance(safer, str) and safer:
        data["ordin"]["safer_next_step"] = safer[:MAX_PROXY_REASON_LENGTH]
    return data


def _append_private_jsonl(path: str | Path, payload: Mapping[str, Any]) -> None:
    line = (
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")
    if len(line) > MAX_LOCAL_EVENT_BYTES:
        raise ValueError(f"local event exceeds maximum size {MAX_LOCAL_EVENT_BYTES} bytes")
    target = Path(path)
    if not target.parent.exists():
        raise ValueError(f"local evidence directory does not exist: {target.parent}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
    fd = os.open(target, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("local evidence append made no progress")
            view = view[written:]
    finally:
        os.close(fd)


@dataclass(frozen=True)
class MCPClientMessageDecision:
    forward: bool
    response: dict[str, Any] | None = None
    action_id: str | None = None


@dataclass(frozen=True)
class _PendingToolCall:
    action_id: str
    tool: str
    request_id_digest: str


class MCPStdioSafetyProxy:
    """Review MCP `tools/call` requests while transparently relaying stdio JSON-RPC.

    The proxy owns transport forwarding only. Tool execution, server credentials,
    result semantics, retries, and approval UI remain owned by the MCP client and
    upstream server.
    """

    def __init__(
        self,
        *,
        server_id: str,
        gate: AgentGate | None = None,
        shell_tools: frozenset[str] = frozenset(),
        context: ExecutionContext | None = None,
        observations_path: str | Path | None = None,
    ) -> None:
        self.adapter = MCPAdapter(server=server_id, shell_tools=shell_tools)
        self.server_id = self.adapter.server
        self.gate = gate if gate is not None else AgentGate()
        self.context = context or ExecutionContext(
            cwd=os.getcwd(),
            agent=f"{MCP_PROXY_RUNTIME}:{self.server_id}",
        )
        self.observations_path = Path(observations_path) if observations_path is not None else None
        self._sequence = 0
        self._pending: dict[str | int | float, _PendingToolCall] = {}
        self._lock = threading.Lock()

    def process_client_message(self, message: Mapping[str, Any]) -> MCPClientMessageDecision:
        """Review a client JSON-RPC message and decide whether to forward it."""

        if not isinstance(message, Mapping):
            return MCPClientMessageDecision(
                forward=False,
                response=_jsonrpc_error(
                    None,
                    code=INVALID_REQUEST_CODE,
                    message="MCP proxy requires one JSON-RPC object per line",
                ),
            )
        if message.get("jsonrpc") != "2.0":
            return MCPClientMessageDecision(
                forward=False,
                response=_jsonrpc_error(
                    message.get("id"),
                    code=INVALID_REQUEST_CODE,
                    message="invalid JSON-RPC version",
                ),
            )
        if message.get("method") != "tools/call":
            return MCPClientMessageDecision(forward=True)

        request_id = message.get("id")
        request_key = _request_id_key(request_id)
        if request_key is None:
            return MCPClientMessageDecision(
                forward=False,
                response=_jsonrpc_error(
                    None,
                    code=INVALID_REQUEST_CODE,
                    message="tools/call requires a string or numeric JSON-RPC id",
                ),
            )
        params = message.get("params")
        if not isinstance(params, Mapping):
            return MCPClientMessageDecision(
                forward=False,
                response=_jsonrpc_error(
                    request_id,
                    code=INVALID_REQUEST_CODE,
                    message="tools/call params must be a JSON object",
                ),
            )
        tool = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(tool, str) or not tool.strip():
            return MCPClientMessageDecision(
                forward=False,
                response=_jsonrpc_error(
                    request_id,
                    code=INVALID_REQUEST_CODE,
                    message="tools/call requires a non-empty tool name",
                ),
            )
        if not isinstance(arguments, Mapping):
            return MCPClientMessageDecision(
                forward=False,
                response=_jsonrpc_error(
                    request_id,
                    code=INVALID_REQUEST_CODE,
                    message="tools/call arguments must be a JSON object",
                ),
            )

        with self._lock:
            if request_key in self._pending:
                return MCPClientMessageDecision(
                    forward=False,
                    response=_jsonrpc_error(
                        request_id,
                        code=INVALID_REQUEST_CODE,
                        message="duplicate in-flight JSON-RPC id",
                    ),
                )
            self._sequence += 1
            sequence = self._sequence

        action_id = _action_id(
            server_id=self.server_id,
            request_id=request_id,
            tool=tool,
            sequence=sequence,
        )
        try:
            decision = self.gate.evaluate_mcp(
                self.adapter,
                tool,
                arguments,
                context=self.context,
                action_id=action_id,
            )
        except ValueError as exc:
            return MCPClientMessageDecision(
                forward=False,
                response=_jsonrpc_error(
                    request_id,
                    code=INVALID_REQUEST_CODE,
                    message=f"Ordin rejected malformed tools/call input: {exc}",
                ),
                action_id=action_id,
            )

        if decision.may_execute:
            with self._lock:
                self._pending[request_key] = _PendingToolCall(
                    action_id=action_id,
                    tool=tool,
                    request_id_digest=_request_id_digest(request_id),
                )
            return MCPClientMessageDecision(forward=True, action_id=action_id)

        if decision.denied:
            return MCPClientMessageDecision(
                forward=False,
                response=_jsonrpc_error(
                    request_id,
                    code=BLOCKED_CODE,
                    message="Ordin blocked MCP tool call",
                    data=_decision_data(decision),
                ),
                action_id=action_id,
            )
        return MCPClientMessageDecision(
            forward=False,
            response=_jsonrpc_error(
                request_id,
                code=APPROVAL_REQUIRED_CODE,
                message="Ordin requires approval for MCP tool call",
                data=_decision_data(decision),
            ),
            action_id=action_id,
        )

    def observe_server_message(self, message: Mapping[str, Any]) -> ActionObservation | None:
        """Create a redacted observation for a terminal upstream tool response."""

        if not isinstance(message, Mapping) or message.get("jsonrpc") != "2.0":
            return None
        if "id" not in message or ("result" not in message and "error" not in message):
            return None
        request_key = _request_id_key(message.get("id"))
        if request_key is None:
            return None
        with self._lock:
            pending = self._pending.pop(request_key, None)
        if pending is None:
            return None

        exit_code: int | None
        status: str
        result_type: str | None = None
        if "error" in message:
            exit_code = None
            status = "protocol_error"
        else:
            result = message.get("result")
            if isinstance(result, Mapping):
                candidate = result.get("resultType")
                if isinstance(candidate, str):
                    result_type = candidate
                if result_type == "task":
                    exit_code = None
                    status = "task_accepted"
                elif result_type == "input_required":
                    exit_code = None
                    status = "input_required"
                elif result.get("isError") is True:
                    exit_code = 1
                    status = "tool_error"
                else:
                    exit_code = 0
                    status = "success"
            else:
                exit_code = 0
                status = "success"

        metadata: dict[str, Any] = {
            "runtime": MCP_PROXY_RUNTIME,
            "server": self.server_id,
            "tool": pending.tool,
            "request_id_sha256": pending.request_id_digest,
            "status": status,
        }
        if result_type is not None:
            metadata["result_type"] = result_type
        observation = ActionObservation(
            action_id=pending.action_id,
            exit_code=exit_code,
            metadata=metadata,
        )
        if self.observations_path is not None:
            _append_private_jsonl(self.observations_path, observation.as_dict())
        return observation

    @property
    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)


def build_mcp_proxy(
    *,
    server_id: str,
    semantics_path: str | Path | None = None,
    policy_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    observations_path: str | Path | None = None,
    fail_on: FailThreshold = "warn",
    shell_tools: frozenset[str] = frozenset(),
    cwd: str | None = None,
) -> MCPStdioSafetyProxy:
    semantics = load_tool_semantics(semantics_path) if semantics_path is not None else None
    action_policy = load_action_policy(policy_path) if policy_path is not None else None
    audit = JsonlAuditSink(audit_path) if audit_path is not None else None
    ordin = Ordin(
        policy=ReviewPolicy(fail_on=fail_on),
        action_policy=action_policy,
        tool_semantics=semantics,
        audit=audit,
    )
    context = ExecutionContext(
        cwd=cwd or os.getcwd(),
        agent=f"{MCP_PROXY_RUNTIME}:{server_id}",
    )
    return MCPStdioSafetyProxy(
        server_id=server_id,
        gate=AgentGate(ordin),
        shell_tools=shell_tools,
        context=context,
        observations_path=observations_path,
    )


def _read_bounded_line(stream: IO[bytes]) -> bytes | None:
    line = stream.readline(MAX_MCP_MESSAGE_BYTES + 1)
    if not line:
        return None
    if len(line) <= MAX_MCP_MESSAGE_BYTES:
        return line
    while line and not line.endswith(b"\n"):
        line = stream.readline(MAX_MCP_MESSAGE_BYTES + 1)
    raise ValueError(f"MCP stdio message exceeds maximum size {MAX_MCP_MESSAGE_BYTES} bytes")


def _unique_json_members(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("MCP JSON contains duplicate object members")
        result[key] = value
    return result


def _finite_json_number(text: str) -> float:
    number = float(text)
    if not math.isfinite(number):
        raise ValueError("MCP JSON numbers must be finite")
    return number


def _validate_json_depth(payload: Mapping[str, Any]) -> None:
    pending: list[tuple[Any, int]] = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if not isinstance(value, (dict, list)):
            continue
        if depth > MAX_MCP_JSON_DEPTH:
            raise ValueError(f"MCP JSON nesting exceeds maximum depth {MAX_MCP_JSON_DEPTH}")
        children = value.values() if isinstance(value, dict) else value
        pending.extend((child, depth + 1) for child in children)


def _parse_jsonrpc_line(line: bytes) -> Mapping[str, Any]:
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("MCP stdio message must be UTF-8") from exc
    try:
        payload = json.loads(
            text,
            object_pairs_hook=_unique_json_members,
            parse_constant=_finite_json_number,
            parse_float=_finite_json_number,
        )
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid MCP JSON at line {exc.lineno} column {exc.colno}") from exc
    except RecursionError as exc:
        raise ValueError("MCP JSON nesting is too deep") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("MCP stdio requires one JSON-RPC object per line")
    _validate_json_depth(payload)
    return payload


def _encode_jsonrpc(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode("utf-8")


def _relay_upstream_stdout(
    process: subprocess.Popen[bytes],
    proxy: MCPStdioSafetyProxy,
    client_stdout: IO[bytes],
    output_lock: threading.Lock,
    failed: threading.Event,
) -> None:
    assert process.stdout is not None
    try:
        while True:
            line = _read_bounded_line(process.stdout)
            if line is None:
                return
            message = _parse_jsonrpc_line(line)
            proxy.observe_server_message(message)
            with output_lock:
                client_stdout.write(line)
                client_stdout.flush()
    except (OSError, ValueError) as exc:
        print(f"ordin-mcp-proxy: upstream protocol error: {exc}", file=sys.stderr)
        failed.set()
        try:
            process.terminate()
        except OSError:
            pass


def _relay_upstream_stderr(process: subprocess.Popen[bytes], failed: threading.Event) -> None:
    assert process.stderr is not None
    try:
        while True:
            chunk = process.stderr.read(65536)
            if not chunk:
                return
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()
    except OSError:
        failed.set()


def _read_client_stdin(
    stream: IO[bytes],
    messages: queue.Queue[bytes | ValueError | OSError | None],
    stopped: threading.Event,
) -> None:
    # Use an owned, unbuffered descriptor so an idle daemon reader cannot hold
    # sys.stdin's buffered lock during interpreter shutdown.
    with stream:
        while not stopped.is_set():
            item: bytes | ValueError | OSError | None
            try:
                item = _read_bounded_line(stream)
            except (ValueError, OSError) as exc:
                item = exc
            while not stopped.is_set():
                try:
                    messages.put(item, timeout=0.1)
                    break
                except queue.Full:
                    continue
            if item is None or isinstance(item, OSError):
                return


def run_stdio_proxy(
    proxy: MCPStdioSafetyProxy,
    command: Sequence[str],
    *,
    shutdown_timeout: float = 5.0,
) -> int:
    if not command:
        raise ValueError("upstream MCP command must not be empty")
    if shutdown_timeout <= 0:
        raise ValueError("shutdown timeout must be greater than zero")

    process = subprocess.Popen(
        list(command),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert process.stdin is not None
    output_lock = threading.Lock()
    failed = threading.Event()
    stdout_thread = threading.Thread(
        target=_relay_upstream_stdout,
        args=(process, proxy, sys.stdout.buffer, output_lock, failed),
        name="ordin-mcp-upstream-stdout",
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_relay_upstream_stderr,
        args=(process, failed),
        name="ordin-mcp-upstream-stderr",
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()

    messages: queue.Queue[bytes | ValueError | OSError | None] = queue.Queue(maxsize=1)
    stopped = threading.Event()

    exit_code = 0
    try:
        client_stream = os.fdopen(os.dup(sys.stdin.fileno()), "rb", buffering=0)
        stdin_thread = threading.Thread(
            target=_read_client_stdin,
            args=(client_stream, messages, stopped),
            name="ordin-mcp-client-stdin",
            daemon=True,
        )
        stdin_thread.start()
        while not failed.is_set() and process.poll() is None:
            try:
                item = messages.get(timeout=0.1)
            except queue.Empty:
                continue
            if isinstance(item, OSError):
                print(f"ordin-mcp-proxy: client read failed: {item}", file=sys.stderr)
                failed.set()
                break
            if isinstance(item, ValueError):
                error = _jsonrpc_error(None, code=PARSE_ERROR_CODE, message=str(item))
                with output_lock:
                    sys.stdout.buffer.write(_encode_jsonrpc(error))
                    sys.stdout.buffer.flush()
                continue
            line = item
            if line is None:
                break
            try:
                message = _parse_jsonrpc_line(line)
            except ValueError as exc:
                error = _jsonrpc_error(None, code=PARSE_ERROR_CODE, message=str(exc))
                with output_lock:
                    sys.stdout.buffer.write(_encode_jsonrpc(error))
                    sys.stdout.buffer.flush()
                continue
            decision = proxy.process_client_message(message)
            if decision.forward:
                try:
                    process.stdin.write(line)
                    process.stdin.flush()
                except OSError as exc:
                    print(f"ordin-mcp-proxy: upstream write failed: {exc}", file=sys.stderr)
                    failed.set()
                    exit_code = 1
                    break
            elif decision.response is not None:
                with output_lock:
                    sys.stdout.buffer.write(_encode_jsonrpc(decision.response))
                    sys.stdout.buffer.flush()
    finally:
        stopped.set()
        try:
            process.stdin.close()
        except OSError:
            pass
        try:
            child_code = process.wait(timeout=shutdown_timeout)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                child_code = process.wait(timeout=shutdown_timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                child_code = process.wait()
        stdout_thread.join(timeout=shutdown_timeout)
        stderr_thread.join(timeout=shutdown_timeout)

    if failed.is_set():
        return 1
    if exit_code:
        return exit_code
    return child_code


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ordin-mcp-proxy",
        description="Local stdio MCP safety proxy powered by Ordin.",
    )
    parser.add_argument(
        "--server-id", required=True, help="Stable identity for the upstream MCP server"
    )
    parser.add_argument("--semantics", help="Optional exact tool-semantics JSON file")
    parser.add_argument("--policy", help="Optional declarative Ordin action-policy JSON file")
    parser.add_argument(
        "--fail-on",
        choices=("warn", "ask", "block"),
        default="warn",
        help="Execution threshold; default permits only Ordin allow decisions",
    )
    parser.add_argument(
        "--shell-tool",
        action="append",
        default=[],
        help="Exact MCP tool name whose documented contract is shell execution",
    )
    parser.add_argument("--audit", help="Optional local redacted decision-audit JSONL path")
    parser.add_argument("--observations", help="Optional local redacted observation JSONL path")
    parser.add_argument("--cwd", help="Explicit execution context working directory")
    parser.add_argument(
        "--shutdown-timeout",
        type=float,
        default=5.0,
        help="Seconds to allow the upstream subprocess to exit after client EOF",
    )
    parser.add_argument(
        "command", nargs=argparse.REMAINDER, help="Upstream MCP server command after --"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("ordin-mcp-proxy: upstream MCP server command is required after --", file=sys.stderr)
        return 2
    try:
        proxy = build_mcp_proxy(
            server_id=args.server_id,
            semantics_path=args.semantics,
            policy_path=args.policy,
            audit_path=args.audit,
            observations_path=args.observations,
            fail_on=args.fail_on,
            shell_tools=frozenset(args.shell_tool),
            cwd=args.cwd,
        )
        return run_stdio_proxy(proxy, command, shutdown_timeout=args.shutdown_timeout)
    except (OSError, ValueError) as exc:
        print(f"ordin-mcp-proxy: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
