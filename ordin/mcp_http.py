"""Local Streamable HTTP transport around the shared MCP review session."""

from __future__ import annotations

from .trace_capture import TraceRecorder

import argparse
import hashlib
import http.client
import ipaddress
import json
import math
import re
import secrets
import socket
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping, Sequence, cast
from urllib.parse import urlsplit

from .agent import AgentGate
from .context import ExecutionContext
from .policy import FailThreshold
from .mcp_contracts import MCPContractLock, load_contract_json
from .mcp_proxy import (
    MAX_MCP_MESSAGE_BYTES,
    MCPStdioSafetyProxy,
    _append_private_jsonl,
    _jsonrpc_error,
    _parse_jsonrpc_line,
    _request_id_key,
    build_mcp_proxy,
)


MCP_HTTP_PROTOCOL = "2025-11-25"
MAX_HTTP_HEADERS = 16_384
MAX_HTTP_SESSIONS = 64
MAX_HTTP_CONNECTIONS = 32
_HEADER_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_RESERVED_HEADERS = {
    "authorization",
    "host",
    "cookie",
    "set-cookie",
    "content-length",
    "transfer-encoding",
    "connection",
    "proxy-authorization",
    "proxy-authenticate",
    "te",
    "trailer",
    "upgrade",
    "origin",
    "mcp-session-id",
    "mcp-protocol-version",
    "content-type",
    "accept",
    "last-event-id",
}


class HTTPBoundaryError(ValueError):
    def __init__(self, status: int, code: str) -> None:
        self.status, self.code = status, code
        super().__init__(code)


@dataclass(frozen=True)
class MCPHTTPConfig:
    server_id: str
    upstream_url: str
    host: str = "127.0.0.1"
    port: int = 8766
    timeout: float = 30.0
    session_ttl: float = 1800.0
    allow_public: bool = False
    allow_insecure_upstream: bool = False
    allowed_hosts: frozenset[str] = frozenset()
    allowed_origins: frozenset[str] = frozenset()
    forward_authorization: bool = False
    forward_headers: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.allow_insecure_upstream, bool):
            raise ValueError("upstream transport override must be boolean")
        if not isinstance(self.allow_public, bool) or not isinstance(
            self.forward_authorization, bool
        ):
            raise ValueError("HTTP exposure and authorization flags must be boolean")
        if not isinstance(self.host, str) or not self.host or len(self.host) > 256:
            raise ValueError("invalid HTTP listen host")
        if not isinstance(self.upstream_url, str):
            raise ValueError("upstream URL must be text")
        for name in ("allowed_hosts", "allowed_origins", "forward_headers"):
            values = getattr(self, name)
            if (
                not isinstance(values, (set, frozenset, list, tuple))
                or len(values) > 32
                or any(not isinstance(value, str) for value in values)
            ):
                raise ValueError("HTTP header/origin policies require bounded string collections")
            object.__setattr__(self, name, frozenset(values))
        if (
            not isinstance(self.server_id, str)
            or not self.server_id.strip()
            or len(self.server_id) > 256
        ):
            raise ValueError("HTTP MCP requires an exact bounded server identity")
        target = urlsplit(self.upstream_url)
        if (
            target.scheme not in {"http", "https"}
            or not target.hostname
            or target.username is not None
            or target.password is not None
            or target.query
            or target.fragment
        ):
            raise ValueError(
                "upstream requires a fixed HTTP(S) URL without credentials, query, or fragment"
            )
        if len(self.upstream_url) > 4096 or any(ord(c) < 33 for c in self.upstream_url):
            raise ValueError("invalid upstream URL")
        if target.port is not None and not 1 <= target.port <= 65535:
            raise ValueError("invalid upstream port")
        try:
            upstream_loopback = ipaddress.ip_address(target.hostname).is_loopback
        except ValueError:
            upstream_loopback = target.hostname == "localhost"
        if target.scheme == "http" and not upstream_loopback and not self.allow_insecure_upstream:
            raise ValueError("remote upstreams require HTTPS or explicit --allow-insecure-upstream")
        if (
            isinstance(self.port, bool)
            or not isinstance(self.port, int)
            or not 0 <= self.port <= 65535
        ):
            raise ValueError("invalid HTTP listen port")
        try:
            loopback = ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            loopback = self.host == "localhost"
        if not loopback and (not self.allow_public or not self.allowed_hosts):
            raise ValueError(
                "public listeners require --allow-public and explicit --allow-host values"
            )
        if (
            isinstance(self.timeout, bool)
            or not isinstance(self.timeout, (int, float))
            or not math.isfinite(self.timeout)
            or not 0 < self.timeout <= 300
        ):
            raise ValueError("HTTP timeout must be finite, positive, and at most 300 seconds")
        if (
            isinstance(self.session_ttl, bool)
            or not isinstance(self.session_ttl, (int, float))
            or not math.isfinite(self.session_ttl)
            or not 1 <= self.session_ttl <= 86400
        ):
            raise ValueError("HTTP session TTL must be between 1 and 86400 seconds")
        for name in self.forward_headers:
            if not _HEADER_NAME.fullmatch(name) or name.lower() in _RESERVED_HEADERS:
                raise ValueError("unsupported explicit forwarded header")
        if (
            len({name.lower() for name in self.forward_headers}) != len(self.forward_headers)
            or len(self.forward_headers) > 32
        ):
            raise ValueError("forwarded header names must be unique and bounded")
        for value in self.allowed_hosts | self.allowed_origins:
            if not value or len(value) > 1024 or any(ord(c) < 33 for c in value):
                raise ValueError("invalid explicit HTTP host/origin policy")
        for origin in self.allowed_origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("allowed origins must be exact HTTP(S) origins without paths")

    @property
    def endpoint_identity(self) -> str:
        target = urlsplit(self.upstream_url)
        return hashlib.sha256(
            json.dumps(
                [
                    target.scheme,
                    target.hostname,
                    target.port or (443 if target.scheme == "https" else 80),
                    target.path or "/",
                ]
            ).encode()
        ).hexdigest()


@dataclass
class _HTTPSession:
    token: str
    credential_digest: str
    reviewer: MCPStdioSafetyProxy
    initialize_id: str | int | float
    upstream_id: str | None = None
    initialized: bool = False
    failed: bool = False
    active: int = 0
    touched: float = field(default_factory=time.monotonic)


class MCPHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(
        self,
        config: MCPHTTPConfig,
        *,
        gate: AgentGate | None = None,
        shell_tools: frozenset[str] = frozenset(),
        contract_lock: MCPContractLock | None = None,
        observations_path: str | Path | None = None,
        cwd: str | None = None,
        trace: TraceRecorder | None = None,
    ) -> None:
        self.config = config
        self.trace = trace
        self.gate = gate or AgentGate()
        self.shell_tools = shell_tools
        self.contract_lock = contract_lock
        self.observations_path = Path(observations_path) if observations_path else None
        # A remote server does not inherit the proxy process's working directory.
        self.cwd = cwd
        self._sessions: dict[str, _HTTPSession] = {}
        self._session_lock = threading.RLock()
        self._observation_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(MAX_HTTP_CONNECTIONS)
        host = "127.0.0.1" if config.host == "localhost" else config.host
        if ":" in host:
            self.address_family = socket.AF_INET6
        super().__init__((host, config.port), _MCPHTTPHandler)

    def process_request(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any
    ) -> None:
        if not isinstance(request, socket.socket):
            raise ValueError("HTTP proxy requires TCP connections")
        if not self._slots.acquire(blocking=False):
            try:
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(
        self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any
    ) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def new_session(self, credential_digest: str, request_id: str | int | float) -> _HTTPSession:
        with self._session_lock:
            self._expire()
            if len(self._sessions) >= MAX_HTTP_SESSIONS:
                raise HTTPBoundaryError(503, "session_capacity_reached")
            token = secrets.token_urlsafe(24)
            reviewer = MCPStdioSafetyProxy(
                server_id=self.config.server_id,
                gate=self.gate,
                shell_tools=self.shell_tools,
                context=ExecutionContext(cwd=self.cwd, agent=f"mcp-http:{self.config.server_id}"),
                session_id=f"{self.config.endpoint_identity}:{token}",
                contract_lock=self.contract_lock,
                runtime_id="mcp-http",
                trace=self.trace,
            )
            session = _HTTPSession(token, credential_digest, reviewer, request_id, active=1)
            self._sessions[token] = session
            return session

    def session(self, token: str, credential_digest: str) -> _HTTPSession:
        with self._session_lock:
            self._expire()
            session = self._sessions.get(token)
            if session is None:
                raise HTTPBoundaryError(404, "session_not_found")
            if not secrets.compare_digest(session.credential_digest, credential_digest):
                raise HTTPBoundaryError(403, "session_identity_mismatch")
            session.active += 1
            session.touched = time.monotonic()
            return session

    def release(self, session: _HTTPSession) -> None:
        with self._session_lock:
            session.active -= 1
            session.touched = time.monotonic()

    def drop(self, session: _HTTPSession) -> None:
        with self._session_lock:
            self._sessions.pop(session.token, None)
            session.failed = True

    def _expire(self) -> None:
        now = time.monotonic()
        for token, session in list(self._sessions.items()):
            if not session.active and now - session.touched > self.config.session_ttl:
                del self._sessions[token]

    def observe(self, session: _HTTPSession, message: Mapping[str, Any]) -> Mapping[str, Any]:
        if session.failed:
            raise HTTPBoundaryError(409, "session_closed")
        if message.get("jsonrpc") != "2.0" or ("result" in message and "error" in message):
            raise HTTPBoundaryError(502, "invalid_upstream_protocol")
        if "method" in message:
            if (
                not isinstance(message["method"], str)
                or "result" in message
                or "error" in message
                or ("id" in message and _request_id_key(message["id"]) is None)
            ):
                raise HTTPBoundaryError(502, "ambiguous_upstream_message")
        elif _request_id_key(message.get("id")) is None or not (
            "result" in message or "error" in message
        ):
            raise HTTPBoundaryError(502, "incomplete_upstream_response")
        result = message.get("result")
        if isinstance(result, Mapping) and (
            "task" in result or result.get("resultType") in ("task", "input_required")
        ):
            raise HTTPBoundaryError(502, "unsupported_upstream_async_result")
        if (
            not session.initialized
            and "method" not in message
            and _request_id_key(message.get("id")) == session.initialize_id
        ):
            if (
                not isinstance(result, Mapping)
                or result.get("protocolVersion") != MCP_HTTP_PROTOCOL
            ):
                raise HTTPBoundaryError(502, "upstream_protocol_version_mismatch")
            capabilities = result.get("capabilities", {})
            if not isinstance(capabilities, Mapping):
                raise HTTPBoundaryError(502, "invalid_upstream_capabilities")
            session.initialized = True
            if "tasks" in capabilities:
                message = {
                    **message,
                    "result": {
                        **result,
                        "capabilities": {
                            key: value for key, value in capabilities.items() if key != "tasks"
                        },
                    },
                }
        observation = session.reviewer.observe_server_message(message)
        if observation is not None and self.observations_path is not None:
            with self._observation_lock:
                _append_private_jsonl(self.observations_path, observation.as_dict())
        return message


class _HeaderReader:
    def __init__(self, stream: Any) -> None:
        self.stream, self.remaining = stream, MAX_HTTP_HEADERS

    def readline(self, limit: int = -1) -> bytes:
        line = self.stream.readline(
            min(self.remaining + 1, limit) if limit >= 0 else self.remaining + 1
        )
        self.remaining -= len(line)
        if self.remaining < 0:
            raise http.client.LineTooLong("HTTP headers")
        return line

    def __getattr__(self, name: str) -> Any:
        return getattr(self.stream, name)


class _BoundedHTTPResponse(http.client.HTTPResponse):
    def begin(self) -> None:
        stream = self.fp
        try:
            self.fp = cast(Any, _HeaderReader(stream))
            super().begin()
        finally:
            self.fp = stream


class _MCPHTTPHandler(BaseHTTPRequestHandler):
    server: MCPHTTPServer
    raw_requestline: bytes
    protocol_version = "HTTP/1.1"
    server_version = "Ordin"
    sys_version = ""

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.server.config.timeout)
        self._sent = False
        self._cors_origin: str | None = None
        self._resumable_seen = False
        self._upstream_socket: socket.socket | None = None
        self._upstream_timed_out = threading.Event()
        # An absolute client deadline also bounds slow-drip headers and bodies.
        # One second of grace permits a structured upstream timeout response.
        self._client_deadline = threading.Timer(self.server.config.timeout + 1, self._expire_client)
        self._client_deadline.daemon = True
        self._client_deadline.start()

    @staticmethod
    def _shutdown(stream: socket.socket | None) -> None:
        if stream is not None:
            try:
                stream.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def _expire_client(self) -> None:
        self._shutdown(self.connection)
        self._shutdown(self._upstream_socket)

    def _expire_upstream(self) -> None:
        self._upstream_timed_out.set()
        self._shutdown(self._upstream_socket)

    def finish(self) -> None:
        self._client_deadline.cancel()
        super().finish()

    def parse_request(self) -> bool:
        if len(self.raw_requestline) > 4096:
            self.requestline = ""
            self.request_version = "HTTP/1.1"
            self.command = ""
            self.send_error(414)
            return False
        stream = self.rfile
        try:
            self.rfile = cast(Any, _HeaderReader(stream))
            return super().parse_request()
        finally:
            self.rfile = stream

    def log_message(self, format: str, *args: Any) -> None:
        # BaseHTTPRequestHandler otherwise logs request paths and malformed data.
        return

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self._json(code, {"error": "invalid_http_request"})

    def _header(self, name: str, maximum: int = 8192) -> str | None:
        values = self.headers.get_all(name, [])
        if len(values) > 1:
            raise HTTPBoundaryError(400, "ambiguous_http_header")
        if not values:
            return None
        value = values[0]
        if len(value) > maximum or any((ord(c) < 32 and c != "\t") or ord(c) > 126 for c in value):
            raise HTTPBoundaryError(400, "invalid_http_header")
        return value

    def _headers(
        self,
        status: int,
        content_type: str,
        *,
        session: _HTTPSession | None = None,
        length: int | None = None,
        extra: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        if self._cors_origin is not None:
            self.send_header("Access-Control-Allow-Origin", self._cors_origin)
            self.send_header("Access-Control-Expose-Headers", "MCP-Session-Id")
            self.send_header("Vary", "Origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        if length is not None:
            self.send_header("Content-Length", str(length))
        if session is not None:
            self.send_header("MCP-Session-Id", session.token)
        self.end_headers()
        self.close_connection = True
        self._sent = True

    def _json(
        self, status: int, payload: Mapping[str, Any], *, session: _HTTPSession | None = None
    ) -> None:
        raw = json.dumps(dict(payload), separators=(",", ":"), allow_nan=False).encode()
        self._headers(status, "application/json", session=session, length=len(raw))
        self.wfile.write(raw)

    def _policy(self) -> dict[str, str]:
        if self.path != "/mcp":
            raise HTTPBoundaryError(404, "endpoint_not_found")
        port = self.server.server_port
        host = self.server.config.host
        authority = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        hosts = {authority, f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"} | set(
            self.server.config.allowed_hosts
        )
        if self._header("Host", 1024) not in hosts:
            raise HTTPBoundaryError(403, "host_not_allowed")
        origin = self._header("Origin", 1024)
        origins = {f"http://{value}" for value in hosts} | set(self.server.config.allowed_origins)
        if origin is not None and origin not in origins:
            raise HTTPBoundaryError(403, "origin_not_allowed")
        self._cors_origin = origin
        auth = self._header("Authorization")
        if auth is not None and not self.server.config.forward_authorization:
            raise HTTPBoundaryError(403, "authorization_forwarding_disabled")
        forwarded = {}
        if auth is not None:
            forwarded["Authorization"] = auth
        for name in sorted(self.server.config.forward_headers):
            value = self._header(name)
            if value is not None:
                forwarded[name] = value
        if self._header("Content-Encoding") not in {None, "identity"}:
            raise HTTPBoundaryError(415, "content_encoding_not_supported")
        accept = self._header("Accept") or ""
        accepted = set()
        for part in accept.split(","):
            fields = part.strip().split(";")
            quality = 1.0
            for parameter in fields[1:]:
                if parameter.strip().startswith("q="):
                    try:
                        quality = float(parameter.strip()[2:])
                    except ValueError:
                        quality = 0
            if math.isfinite(quality) and 0 < quality <= 1:
                accepted.add(fields[0].strip().lower())
        needed = (
            {"application/json", "text/event-stream"}
            if self.command == "POST"
            else {"text/event-stream"}
        )
        if self.command in {"POST", "GET"} and not needed.issubset(accepted):
            raise HTTPBoundaryError(406, "mcp_accept_types_required")
        return forwarded

    def _read_exact(self, count: int) -> bytes:
        result = self.rfile.read(count)
        if len(result) != count:
            raise HTTPBoundaryError(400, "incomplete_http_body")
        return result

    def _body(self) -> bytes:
        if (self._header("Content-Type") or "").split(";")[0].lower() != "application/json":
            raise HTTPBoundaryError(415, "json_content_type_required")
        length = self._header("Content-Length", 20)
        transfer = self._header("Transfer-Encoding", 64)
        if transfer is not None:
            if length is not None or transfer.lower() != "chunked":
                raise HTTPBoundaryError(400, "ambiguous_body_framing")
            chunks: list[bytes] = []
            total = 0
            for _ in range(10000):
                line = self.rfile.readline(129)
                raw_size = line.split(b";", 1)[0].strip()
                if (
                    len(line) > 128
                    or not line.endswith(b"\r\n")
                    or not re.fullmatch(rb"[0-9a-fA-F]+", raw_size)
                ):
                    raise HTTPBoundaryError(400, "invalid_chunk_framing")
                count = int(raw_size, 16)
                total += count
                if total > MAX_MCP_MESSAGE_BYTES:
                    raise HTTPBoundaryError(413, "body_limit_exceeded")
                if not count:
                    if self.rfile.readline(3) != b"\r\n":
                        raise HTTPBoundaryError(400, "request_trailers_not_supported")
                    return b"".join(chunks)
                chunks.append(self._read_exact(count))
                if self._read_exact(2) != b"\r\n":
                    raise HTTPBoundaryError(400, "invalid_chunk_framing")
            raise HTTPBoundaryError(413, "chunk_count_exceeded")
        if length is None:
            raise HTTPBoundaryError(411, "content_length_required")
        if not length.isdecimal():
            raise HTTPBoundaryError(400, "invalid_content_length")
        count = int(length)
        if count > MAX_MCP_MESSAGE_BYTES:
            raise HTTPBoundaryError(413, "body_limit_exceeded")
        return self._read_exact(count)

    def do_POST(self) -> None:
        self._handle()

    def do_GET(self) -> None:
        self._handle()

    def do_DELETE(self) -> None:
        self._handle()

    def do_OPTIONS(self) -> None:
        try:
            self._policy()
            if self._cors_origin is None or self._header("Access-Control-Request-Method") not in {
                "POST",
                "GET",
                "DELETE",
            }:
                raise HTTPBoundaryError(403, "invalid_preflight")
            headers = {
                "content-type",
                "mcp-protocol-version",
                "mcp-session-id",
                "last-event-id",
            } | {name.lower() for name in self.server.config.forward_headers}
            if self.server.config.forward_authorization:
                headers.add("authorization")
            requested = {
                value.strip().lower()
                for value in (self._header("Access-Control-Request-Headers") or "").split(",")
                if value.strip()
            }
            if not requested.issubset(headers):
                raise HTTPBoundaryError(403, "preflight_header_not_allowed")
            self._headers(
                204,
                "text/plain",
                length=0,
                extra={
                    "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": ", ".join(sorted(headers)),
                },
            )
        except HTTPBoundaryError as exc:
            self._json(exc.status, {"error": exc.code})

    def _handle(self) -> None:
        session = None
        upstream = None
        upstream_deadline = None
        initialize = False
        request_id = None
        try:
            forwarded = self._policy()
            credential_digest = hashlib.sha256(
                json.dumps(forwarded, sort_keys=True).encode()
            ).hexdigest()
            version = self._header("MCP-Protocol-Version", 64)
            if version not in {None, MCP_HTTP_PROTOCOL}:
                raise HTTPBoundaryError(400, "unsupported_protocol_version")
            token = self._header("MCP-Session-Id", 1024)
            message = None
            body = None
            if self.command == "POST":
                body = self._body()
                try:
                    message = _parse_jsonrpc_line(body)
                except ValueError as exc:
                    raise HTTPBoundaryError(400, "invalid_json_rpc_body") from exc
                if (
                    message.get("jsonrpc") != "2.0"
                    or ("method" in message and not isinstance(message["method"], str))
                    or ("method" in message and ("result" in message or "error" in message))
                    or ("result" in message and "error" in message)
                ):
                    raise HTTPBoundaryError(400, "ambiguous_json_rpc_message")
                request_id = _request_id_key(message.get("id"))
                if "id" in message and request_id is None:
                    raise HTTPBoundaryError(400, "invalid_request_id")
                if "method" not in message and (
                    request_id is None or not ("result" in message or "error" in message)
                ):
                    raise HTTPBoundaryError(400, "invalid_json_rpc_response")
                initialize = message.get("method") == "initialize"
                if str(message.get("method", "")).startswith("tasks/"):
                    raise HTTPBoundaryError(400, "task_augmentation_not_supported")
                if initialize:
                    params = message.get("params")
                    if (
                        token is not None
                        or request_id is None
                        or not isinstance(params, Mapping)
                        or params.get("protocolVersion") != MCP_HTTP_PROTOCOL
                    ):
                        raise HTTPBoundaryError(400, "invalid_initialization")
                    session = self.server.new_session(credential_digest, request_id)
                if (
                    message.get("method") == "tools/call"
                    and isinstance(message.get("params"), Mapping)
                    and "task" in message["params"]
                ):
                    raise HTTPBoundaryError(400, "task_augmentation_not_supported")
            if session is None:
                if token is None:
                    raise HTTPBoundaryError(400, "session_id_required")
                session = self.server.session(token, credential_digest)
            cancel = message is not None and message.get("method") == "notifications/cancelled"
            if session.failed and self.command != "DELETE" and not cancel:
                raise HTTPBoundaryError(409, "session_requires_reset")
            if (
                not initialize
                and not session.initialized
                and self.command != "DELETE"
                and not cancel
                and not (self.command == "GET" and self._header("Last-Event-ID", 1024))
                and not (message is not None and "method" not in message)
            ):
                raise HTTPBoundaryError(400, "session_initialization_incomplete")
            if message is not None:
                decision = session.reviewer.process_client_message(message)
                if not decision.forward:
                    self._json(
                        200,
                        decision.response
                        or _jsonrpc_error(request_id, code=-32600, message="invalid request"),
                        session=session,
                    )
                    return
            target = urlsplit(self.server.config.upstream_url)
            assert target.hostname is not None
            connection_type = (
                http.client.HTTPSConnection
                if target.scheme == "https"
                else http.client.HTTPConnection
            )
            upstream = connection_type(
                target.hostname, target.port, timeout=self.server.config.timeout
            )
            upstream.response_class = _BoundedHTTPResponse
            self._upstream_expires_at = time.monotonic() + self.server.config.timeout
            upstream_deadline = threading.Timer(self.server.config.timeout, self._expire_upstream)
            upstream_deadline.daemon = True
            upstream_deadline.start()
            upstream.connect()
            self._upstream_socket = upstream.sock
            if self._upstream_timed_out.is_set():
                raise TimeoutError("upstream deadline exceeded")
            headers = {
                "Accept": self._header("Accept") or "application/json, text/event-stream",
                "Accept-Encoding": "identity",
                "MCP-Protocol-Version": MCP_HTTP_PROTOCOL,
                **forwarded,
            }
            if session.upstream_id is not None:
                headers["MCP-Session-Id"] = session.upstream_id
            if self.command == "POST":
                headers["Content-Type"] = "application/json"
            event_id = self._header("Last-Event-ID", 1024)
            if event_id is not None:
                if self.command != "GET":
                    raise HTTPBoundaryError(400, "resume_requires_get")
                headers["Last-Event-ID"] = event_id
            upstream.request(self.command, target.path or "/", body=body, headers=headers)
            response = upstream.getresponse()
            self._check_upstream_deadline()
            self._validate_upstream_headers(response)
            if 300 <= response.status < 400:
                raise HTTPBoundaryError(502, "upstream_redirect_refused")
            if not 200 <= response.status < 300:
                if self.command == "DELETE" and response.status == 405:
                    self.server.drop(session)
                    self._headers(204, "application/json", length=0)
                    return
                if response.status == 404:
                    self.server.drop(session)
                elif self.command == "POST":
                    session.failed = True
                status = (
                    response.status
                    if response.status
                    in {400, 401, 403, 404, 405, 408, 409, 413, 415, 429, 500, 502, 503, 504}
                    else 502
                )
                self._json(
                    status, {"error": "upstream_http_error", "upstream_status": response.status}
                )
                return
            upstream_id = response.getheader("MCP-Session-Id")
            if upstream_id is not None:
                if (
                    not upstream_id
                    or len(upstream_id) > 1024
                    or any(not 33 <= ord(c) <= 126 for c in upstream_id)
                ):
                    raise HTTPBoundaryError(502, "invalid_upstream_session_id")
                if initialize or (not session.initialized and session.upstream_id is None):
                    session.upstream_id = upstream_id
                elif upstream_id != session.upstream_id:
                    raise HTTPBoundaryError(502, "upstream_session_changed")
            if self.command == "DELETE":
                self.server.drop(session)
                self._headers(204, "application/json", length=0)
                return
            is_request = message is not None and "method" in message and request_id is not None
            if self.command == "POST" and not is_request:
                if response.status != 202:
                    raise HTTPBoundaryError(502, "notification_requires_accepted_response")
                self._headers(202, "application/json", session=session, length=0)
                return
            content_type = (
                (response.getheader("Content-Type") or "").split(";", 1)[0].strip().lower()
            )
            if content_type == "text/event-stream":
                self._headers(200, "text/event-stream", session=session)
                self._stream(response, session, request_id=request_id if is_request else None)
            elif content_type == "application/json" and self.command == "POST":
                raw = response.read(MAX_MCP_MESSAGE_BYTES + 1)
                self._check_upstream_deadline()
                if len(raw) > MAX_MCP_MESSAGE_BYTES:
                    raise HTTPBoundaryError(502, "upstream_body_limit_exceeded")
                reply = _parse_jsonrpc_line(raw)
                if (
                    "method" in reply
                    or _request_id_key(reply.get("id")) != request_id
                    or not ("result" in reply or "error" in reply)
                ):
                    raise HTTPBoundaryError(502, "uncorrelated_upstream_response")
                reply = self.server.observe(session, reply)
                self._json(200, reply, session=session)
            else:
                raise HTTPBoundaryError(502, "unsupported_upstream_response")
        except HTTPBoundaryError as exc:
            if session is not None and exc.status >= 500:
                session.failed = True
            if not self._sent:
                self._json(exc.status, {"error": exc.code})
        except (TimeoutError, socket.timeout):
            if session is not None and self.command == "POST" and not self._resumable_seen:
                session.failed = True
            if not self._sent:
                self._json(504, {"error": "upstream_or_client_timeout"})
        except (BrokenPipeError, ConnectionResetError, http.client.IncompleteRead):
            # Stream disconnects are not cancellation. Keep correlation state
            # available for a GET resumption using the upstream event cursor.
            if not self._sent:
                if session is not None:
                    session.failed = True
                self._json(
                    504 if self._upstream_timed_out.is_set() else 502,
                    {"error": "upstream_connection_closed"},
                )
        except (OSError, ValueError, http.client.HTTPException):
            if session is not None:
                session.failed = True
            if not self._sent:
                self._json(
                    504 if self._upstream_timed_out.is_set() else 502,
                    {"error": "upstream_protocol_failure"},
                )
        finally:
            if upstream_deadline is not None:
                upstream_deadline.cancel()
            if upstream is not None:
                upstream.close()
            self._upstream_socket = None
            if session is not None:
                if initialize and (
                    session.failed or (not session.initialized and not self._resumable_seen)
                ):
                    self.server.drop(session)
                self.server.release(session)

    def _validate_upstream_headers(self, response: http.client.HTTPResponse) -> None:
        for name in (
            "Content-Length",
            "Transfer-Encoding",
            "Content-Type",
            "Content-Encoding",
            "MCP-Session-Id",
        ):
            if len(response.headers.get_all(name, [])) > 1:
                raise HTTPBoundaryError(502, "ambiguous_upstream_header")
        if (
            response.getheader("Content-Length") is not None
            and response.getheader("Transfer-Encoding") is not None
        ):
            raise HTTPBoundaryError(502, "ambiguous_upstream_framing")
        if response.getheader("Content-Encoding") not in {None, "identity"}:
            raise HTTPBoundaryError(502, "upstream_encoding_not_supported")
        if response.getheader("Transfer-Encoding") not in {None, "chunked"}:
            raise HTTPBoundaryError(502, "upstream_transfer_encoding_not_supported")
        length = response.getheader("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) > MAX_MCP_MESSAGE_BYTES):
            raise HTTPBoundaryError(502, "upstream_body_limit_exceeded")

    def _check_upstream_deadline(self) -> None:
        if self._upstream_timed_out.is_set() or time.monotonic() >= self._upstream_expires_at:
            self._upstream_timed_out.set()
            raise TimeoutError("upstream deadline exceeded")

    def _stream(
        self,
        response: http.client.HTTPResponse,
        session: _HTTPSession,
        *,
        request_id: str | int | float | None,
    ) -> None:
        event = bytearray()
        data: list[bytes] = []
        total = 0
        while True:
            self._check_upstream_deadline()
            line = response.readline(MAX_MCP_MESSAGE_BYTES + 1)
            self._check_upstream_deadline()
            if not line:
                if request_id is not None and not self._resumable_seen:
                    raise HTTPBoundaryError(502, "upstream_stream_ended_without_result")
                return
            total += len(line)
            if total > 64 * 1024 * 1024:
                raise HTTPBoundaryError(502, "upstream_stream_byte_limit_exceeded")
            if len(line) > MAX_MCP_MESSAGE_BYTES or len(event) + len(line) > MAX_MCP_MESSAGE_BYTES:
                raise HTTPBoundaryError(502, "upstream_event_limit_exceeded")
            if line.startswith(b":"):
                self.wfile.write(line)
                self.wfile.flush()
                continue
            event.extend(line)
            stripped = line.rstrip(b"\r\n")
            if stripped.startswith(b"data:"):
                value = stripped[5:]
                data.append(value[1:] if value.startswith(b" ") else value)
            if stripped:
                continue
            if any(
                part.startswith(b"id:") and part[3:].strip() for part in bytes(event).splitlines()
            ):
                self._resumable_seen = True
            terminal = False
            raw = b"\n".join(data)
            if raw:
                message = _parse_jsonrpc_line(raw)
                forwarded = self.server.observe(session, message)
                if forwarded is not message:
                    metadata = b"".join(
                        part
                        for part in bytes(event).splitlines(keepends=True)
                        if part.rstrip(b"\r\n") and not part.startswith(b"data:")
                    )
                    event = bytearray(
                        metadata
                        + b"data: "
                        + json.dumps(dict(forwarded), separators=(",", ":")).encode()
                        + b"\n\n"
                    )
                terminal = (
                    request_id is not None
                    and "method" not in message
                    and _request_id_key(message.get("id")) == request_id
                )
            self.wfile.write(event)
            self.wfile.flush()
            event.clear()
            data.clear()
            if terminal:
                return


def build_http_server(
    config: MCPHTTPConfig,
    *,
    semantics_path: str | Path | None = None,
    policy_path: str | Path | None = None,
    contract_lock_path: str | Path | None = None,
    audit_path: str | Path | None = None,
    observations_path: str | Path | None = None,
    shell_tools: frozenset[str] = frozenset(),
    fail_on: FailThreshold = "warn",
    cwd: str | None = None,
    trace_path: str | Path | None = None,
    raw_local: bool = False,
) -> MCPHTTPServer:
    template = build_mcp_proxy(
        server_id=config.server_id,
        semantics_path=semantics_path,
        policy_path=policy_path,
        contract_lock_path=contract_lock_path,
        audit_path=audit_path,
        trace_path=trace_path,
        raw_local=raw_local,
        runtime_id="mcp-http",
        shell_tools=shell_tools,
        fail_on=fail_on,
        cwd=cwd,
    )
    lock = (
        MCPContractLock.from_dict(load_contract_json(contract_lock_path))
        if contract_lock_path
        else None
    )
    return MCPHTTPServer(
        config,
        gate=template.gate,
        trace=template.trace,
        shell_tools=shell_tools,
        contract_lock=lock,
        observations_path=observations_path,
        cwd=cwd,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ordin-mcp-http", description="Local Streamable HTTP MCP safety proxy"
    )
    parser.add_argument("--server-id", required=True)
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--timeout", type=float, default=30)
    parser.add_argument("--session-ttl", type=float, default=1800)
    parser.add_argument("--allow-public", action="store_true")
    parser.add_argument("--allow-insecure-upstream", action="store_true")
    parser.add_argument("--allow-host", action="append", default=[])
    parser.add_argument("--allow-origin", action="append", default=[])
    parser.add_argument("--forward-authorization", action="store_true")
    parser.add_argument("--forward-header", action="append", default=[])
    parser.add_argument("--semantics")
    parser.add_argument("--policy")
    parser.add_argument("--contract-lock")
    parser.add_argument("--audit")
    parser.add_argument("--trace", help="Opt-in private local trace database")
    parser.add_argument(
        "--trace-raw-local", action="store_true", help="Capture raw actions; unsafe to share"
    )
    parser.add_argument("--observations")
    parser.add_argument("--shell-tool", action="append", default=[])
    parser.add_argument("--fail-on", choices=("warn", "ask", "block"), default="warn")
    parser.add_argument("--cwd")
    args = parser.parse_args(argv)
    server = None
    try:
        config = MCPHTTPConfig(
            args.server_id,
            args.upstream,
            host=args.host,
            port=args.port,
            timeout=args.timeout,
            session_ttl=args.session_ttl,
            allow_public=args.allow_public,
            allow_insecure_upstream=args.allow_insecure_upstream,
            allowed_hosts=frozenset(args.allow_host),
            allowed_origins=frozenset(args.allow_origin),
            forward_authorization=args.forward_authorization,
            forward_headers=frozenset(args.forward_header),
        )
        server = build_http_server(
            config,
            semantics_path=args.semantics,
            policy_path=args.policy,
            contract_lock_path=args.contract_lock,
            audit_path=args.audit,
            trace_path=args.trace,
            raw_local=args.trace_raw_local,
            observations_path=args.observations,
            shell_tools=frozenset(args.shell_tool),
            fail_on=args.fail_on,
            cwd=args.cwd,
        )
        print(
            json.dumps(
                {
                    "server_id": config.server_id,
                    "host": config.host,
                    "port": server.server_port,
                    "endpoint": "/mcp",
                }
            ),
            flush=True,
        )
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        return 0
    except (OSError, ValueError):
        print(json.dumps({"error": "invalid_or_unavailable_http_proxy_configuration"}), flush=True)
        return 2
    finally:
        if server is not None:
            server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
