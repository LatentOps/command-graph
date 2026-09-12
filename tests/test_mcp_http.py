import http.client
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ordin import AgentGate, Ordin
from ordin.audit import JsonlAuditSink
from ordin.mcp_contracts import MCPContractLock, semantics_binding_digest, tool_contract_digest
from ordin.mcp_http import MCPHTTPConfig, MCPHTTPServer, MCP_HTTP_PROTOCOL
from ordin.mcp_proxy import APPROVAL_REQUIRED_CODE, BLOCKED_CODE, MAX_MCP_MESSAGE_BYTES
from ordin.tool_calls import ToolResourceBinding, ToolSemanticRule, ToolSemanticsRegistry


def _contract():
    return {
        "name": "read_file",
        "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }


def _registry():
    return ToolSemanticsRegistry(
        "http-test",
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


@contextmanager
def _running(server):
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@contextmanager
def _upstream(mode="json"):
    state = {
        "calls": [],
        "headers": [],
        "notifications": [],
        "counter": 0,
        "mode": mode,
        "resume": None,
        "redirect": "http://127.0.0.1:1/never",
        "contracts": [_contract()],
    }
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def send_body(self, payload, *, status=200, session=None, sse=False, priming_only=False):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Connection", "close")
            if session:
                self.send_header("MCP-Session-Id", session)
            if sse:
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                body = b"id: fixture-cursor\ndata:\n\n"
                if not priming_only:
                    body += b"event: message\ndata: " + raw + b"\n\n"
            else:
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                body = raw
            self.end_headers()
            self.close_connection = True
            try:
                if sse:
                    for offset in range(0, len(body), 17):
                        chunk = body[offset : offset + 17]
                        self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def empty(self, status):
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

        def do_POST(self):
            message = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                state["headers"].append(dict(self.headers))
            method = message.get("method")
            session = self.headers.get("MCP-Session-Id")
            if method == "initialize":
                with lock:
                    state["counter"] += 1
                    session = None if mode == "stateless" else f"upstream-{state['counter']}"
                result = {
                    "protocolVersion": MCP_HTTP_PROTOCOL,
                    "capabilities": {"tools": {}, "tasks": {}},
                    "serverInfo": {"name": "fixture", "version": "1"},
                }
            elif method == "tools/list":
                result = {"tools": state["contracts"]}
            elif method == "tools/call":
                with lock:
                    state["calls"].append(message)
                if mode == "timeout":
                    time.sleep(0.3)
                if mode == "drip":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    self.close_connection = True
                    try:
                        for _ in range(30):
                            self.wfile.write(b"x")
                            self.wfile.flush()
                            time.sleep(0.02)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if mode == "redirect":
                    self.send_response(307)
                    self.send_header("Location", state["redirect"])
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if mode == "error":
                    self.send_body({"secret": self.headers.get("Authorization")}, status=401)
                    return
                result = {"content": [{"type": "text", "text": "fixture result"}]}
            elif isinstance(method, str) and method.startswith("notifications/"):
                state["notifications"].append(message)
                self.empty(202)
                return
            else:
                self.empty(202)
                return
            reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
            if mode == "bad-sse" and method == "tools/call":
                self.send_body({"jsonrpc": "2.0", "id": message["id"]}, sse=True)
                return
            if (mode == "resume" and method == "tools/call") or (
                mode == "init-resume" and method == "initialize"
            ):
                state["resume"] = reply
                self.send_body(reply, session=session, sse=True, priming_only=True)
            else:
                self.send_body(reply, session=session, sse=mode == "sse")

        def do_GET(self):
            state["last_event_id"] = self.headers.get("Last-Event-ID")
            if mode == "no-get":
                self.empty(405)
            else:
                reply = state["resume"] or {
                    "jsonrpc": "2.0",
                    "method": "notifications/tools/list_changed",
                }
                self.send_body(reply, sse=True)

        def do_DELETE(self):
            state["deleted"] = self.headers.get("MCP-Session-Id")
            self.empty(204)

    with _running(ThreadingHTTPServer(("127.0.0.1", 0), Handler)) as server:
        yield f"http://127.0.0.1:{server.server_port}/mcp", state


@contextmanager
def _proxy(url, *, auth=False, pins=False, audit=None, observations=None, timeout=2):
    registry = _registry()
    shells = frozenset({"shell"})
    contract_lock = (
        MCPContractLock(
            semantics_binding_digest(registry, shells),
            {("fixture", "read_file"): tool_contract_digest(_contract())},
        )
        if pins
        else None
    )
    server = MCPHTTPServer(
        MCPHTTPConfig("fixture", url, port=0, forward_authorization=auth, timeout=timeout),
        gate=AgentGate(Ordin(tool_semantics=registry, audit=audit)),
        shell_tools=shells,
        contract_lock=contract_lock,
        observations_path=observations,
    )
    with _running(server):
        yield server


def _request(
    server, payload=None, *, token=None, method="POST", headers=None, raw=None, chunked=False
):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
    fields = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
        "MCP-Protocol-Version": MCP_HTTP_PROTOCOL,
        **(headers or {}),
    }
    if token:
        fields["MCP-Session-Id"] = token
    body = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
    if chunked:
        fields["Transfer-Encoding"] = "chunked"
        body = [body[:10], body[10:]]
    try:
        connection.request(method, "/mcp", body=body, headers=fields, encode_chunked=chunked)
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        connection.close()


def _initialize(server, *, headers=None):
    status, fields, raw = _request(
        server,
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_HTTP_PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        },
        headers=headers,
    )
    assert status == 200, raw
    token = fields["MCP-Session-Id"]
    assert not token.startswith("upstream-")
    assert b'"tasks"' not in raw
    return token


def _call(request_id=1, name="read_file", arguments=None):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {"path": "README.md"}},
    }


@pytest.mark.parametrize("mode", ["json", "sse", "stateless"])
def test_json_sse_and_stateless_upstreams_preserve_gate_decisions(mode):
    with _upstream(mode) as (url, state), _proxy(url) as server:
        token = _initialize(server)
        status, _, body = _request(server, _call(), token=token)
        assert status == 200 and b"fixture result" in body
        blocked = _request(server, _call(2, "shell", {"command": "rm -rf /"}), token=token)
        assert json.loads(blocked[2])["error"]["code"] == BLOCKED_CODE
        unknown = _request(server, _call(3, "unknown"), token=token)
        assert json.loads(unknown[2])["error"]["code"] == APPROVAL_REQUIRED_CODE
        assert len(state["calls"]) == 1
        assert server._sessions[token].reviewer.pending_count == 0
        assert server._sessions[token].reviewer.context.cwd is None


def test_contract_pinning_applies_to_http_discovery_and_drift():
    with _upstream() as (url, state), _proxy(url, pins=True) as server:
        token = _initialize(server)
        assert b"error" in _request(server, _call(), token=token)[2]
        listing = {"jsonrpc": "2.0", "id": "list", "method": "tools/list"}
        assert _request(server, listing, token=token)[0] == 200
        assert b"fixture result" in _request(server, _call(2), token=token)[2]
        state["contracts"][0]["inputSchema"]["properties"]["path"]["type"] = "integer"
        _request(server, listing, token=token)
        changed = json.loads(_request(server, _call(3), token=token)[2])
        assert changed["error"]["data"]["ordin"]["contract"]["status"] == "changed"
        assert len(state["calls"]) == 1


def test_authorization_is_explicit_bound_to_session_and_absent_from_evidence(tmp_path):
    audit = tmp_path / "audit.jsonl"
    observations = tmp_path / "observations.jsonl"
    headers = {"Authorization": "Bearer synthetic-private-token"}
    with (
        _upstream() as (url, state),
        _proxy(url, auth=True, audit=JsonlAuditSink(audit), observations=observations) as server,
    ):
        token = _initialize(server, headers=headers)
        assert (
            _request(
                server, _call(arguments={"path": "/private/example"}), token=token, headers=headers
            )[0]
            == 200
        )
        assert state["headers"][-1]["Authorization"] == headers["Authorization"]
        mismatch = _request(
            server, _call(2), token=token, headers={"Authorization": "Bearer different"}
        )
        assert mismatch[0] == 403
        serialized = audit.read_text() + observations.read_text()
        assert "synthetic-private-token" not in serialized and "/private/example" not in serialized
        assert json.loads(observations.read_text())["metadata"]["runtime"] == "mcp-http"
    with _upstream() as (url, state), _proxy(url) as server:
        result = _request(
            server, {"jsonrpc": "2.0", "id": 1, "method": "initialize"}, headers=headers
        )
        assert result[0] == 403 and not state["headers"]


def test_http_sessions_and_concurrent_request_ids_do_not_cross_correlate():
    with _upstream() as (url, state), _proxy(url) as server, _proxy(url) as other:
        first, second = _initialize(server), _initialize(server)
        assert first != second
        assert _request(other, _call(), token=first)[0] == 404
        with ThreadPoolExecutor(max_workers=4) as pool:
            responses = list(
                pool.map(
                    lambda item: _request(server, _call(item[1]), token=item[0]),
                    [(first, 1), (first, 2), (second, 1), (second, 2)],
                )
            )
        assert all(status == 200 and b"fixture result" in body for status, _, body in responses)
        for token in (first, second):
            snapshot = server._sessions[token].reviewer.session.snapshot()
            assert (
                len(snapshot["history"]["actions"])
                == len(snapshot["observations"]["observations"])
                == 2
            )
        assert len(state["calls"]) == 4


def test_resumption_cancellation_and_session_deletion_are_forwarded_without_reexecution():
    with _upstream("resume") as (url, state), _proxy(url) as server:
        token = _initialize(server)
        status, _, body = _request(server, _call(), token=token)
        assert status == 200 and b"fixture-cursor" in body and b"fixture result" not in body
        assert server._sessions[token].reviewer.pending_count == 1
        resumed = _request(
            server, token=token, method="GET", headers={"Last-Event-ID": "fixture-cursor"}
        )
        assert b"fixture result" in resumed[2] and state["last_event_id"] == "fixture-cursor"
        assert server._sessions[token].reviewer.pending_count == 0
        cancelled = _request(
            server,
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}},
            token=token,
        )
        assert cancelled[0] == 202 and cancelled[2] == b""
        assert state["notifications"][-1]["method"] == "notifications/cancelled"
        assert _request(server, token=token, method="DELETE")[0] == 204
        assert state["deleted"].startswith("upstream-")
        assert _request(server, _call(2), token=token)[0] == 404
        assert len(state["calls"]) == 1


def test_malformed_json_framing_origin_and_protocol_fail_before_upstream():
    with _upstream() as (url, state), _proxy(url) as server:
        token = _initialize(server)
        for raw in (
            b'{"jsonrpc":"2.0","id":1,"id":2,"method":"tools/call"}',
            b'{"value":NaN}',
            b"[",
        ):
            assert _request(server, raw=raw, token=token)[0] == 400
        assert (
            _request(server, _call(), token=token, headers={"Origin": "https://untrusted.example"})[
                0
            ]
            == 403
        )
        assert (
            _request(server, _call(), token=token, headers={"Host": "untrusted.example"})[0] == 403
        )
        assert (
            _request(server, _call(), token=token, headers={"MCP-Protocol-Version": "invalid"})[0]
            == 400
        )
        assert (
            _request(server, _call(), token=token, headers={"Accept": "application/json"})[0] == 406
        )
        assert (
            _request(server, _call(), token=token, headers={"Content-Type": "text/plain"})[0] == 415
        )
        assert (
            _request(
                server,
                raw=b"{}",
                token=token,
                headers={"Content-Length": str(MAX_MCP_MESSAGE_BYTES + 1)},
            )[0]
            == 413
        )
        assert not state["calls"]
        assert _request(server, _call(), token=token, chunked=True)[0] == 200


def test_task_augmentation_is_explicitly_rejected():
    with _upstream() as (url, state), _proxy(url) as server:
        token = _initialize(server)
        call = _call()
        call["params"]["task"] = {"ttl": 1000}
        assert _request(server, call, token=token)[0] == 400
        assert (
            _request(
                server,
                {"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"taskId": "opaque"}},
                token=token,
            )[0]
            == 400
        )
        assert not state["calls"]


def test_redirect_and_upstream_error_bodies_never_leak_authorization():
    for mode in ("redirect", "error"):
        with _upstream(mode) as (url, state), _proxy(url, auth=True) as server:
            auth = {"Authorization": "Bearer synthetic-credential"}
            token = _initialize(server, headers=auth)
            result = _request(server, _call(), token=token, headers=auth)
            assert result[0] == (502 if mode == "redirect" else 401)
            assert b"synthetic-credential" not in result[2]


def test_redirect_destination_receives_no_request_or_credentials():
    with (
        _upstream("redirect") as (url, state),
        _upstream() as (destination, target),
        _proxy(url, auth=True) as server,
    ):
        state["redirect"] = destination
        auth = {"Authorization": "Bearer synthetic-credential"}
        token = _initialize(server, headers=auth)
        assert _request(server, _call(), token=token, headers=auth)[0] == 502
        assert target["headers"] == [] and target["calls"] == []


def test_timeout_marks_the_session_unavailable_without_automatic_retry():
    with _upstream("timeout") as (url, state), _proxy(url, timeout=0.05) as server:
        token = _initialize(server)
        assert _request(server, _call(), token=token)[0] == 504
        assert _request(server, _call(2), token=token)[0] == 409
        assert len(state["calls"]) == 1


def test_slow_drip_upstream_cannot_extend_absolute_timeout():
    with _upstream("drip") as (url, state), _proxy(url, timeout=0.1) as server:
        token = _initialize(server)
        start = time.monotonic()
        status, _, body = _request(server, _call(), token=token)
        assert status == 200 and body == b""
        assert time.monotonic() - start < 0.5
        assert _request(server, _call(2), token=token)[0] == 409


def test_initialization_can_resume_before_tool_calls_are_allowed():
    with _upstream("init-resume") as (url, state), _proxy(url) as server:
        token = _initialize(server)
        assert not server._sessions[token].initialized
        assert _request(server, _call(), token=token)[0] == 400
        resumed = _request(
            server, token=token, method="GET", headers={"Last-Event-ID": "fixture-cursor"}
        )
        assert b"protocolVersion" in resumed[2]
        assert server._sessions[token].initialized
        assert b"fixture result" in _request(server, _call(2), token=token)[2]


def test_header_bounds_duplicate_lengths_and_cors_preflight():
    with _upstream() as (url, state), _proxy(url) as server:
        token = _initialize(server)
        assert _request(server, _call(), token=token, headers={"X-Padding": "x" * 17000})[0] == 431
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        connection.putrequest("POST", "/mcp")
        for name, value in [
            ("Accept", "application/json, text/event-stream"),
            ("Content-Type", "application/json"),
            ("MCP-Session-Id", token),
            ("Content-Length", "2"),
            ("Content-Length", "2"),
        ]:
            connection.putheader(name, value)
        connection.endheaders(b"{}")
        assert connection.getresponse().status == 400
        connection.close()
        origin = f"http://127.0.0.1:{server.server_port}"
        status, headers, _ = _request(
            server,
            method="OPTIONS",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type,mcp-session-id",
            },
        )
        assert status == 204 and headers["Access-Control-Allow-Origin"] == origin


def test_public_listener_and_credential_url_require_explicit_safe_configuration():
    with pytest.raises(ValueError, match="public"):
        MCPHTTPConfig("fixture", "http://127.0.0.1/mcp", host="0.0.0.0")
    for url in (
        "http://user:password@example.com/mcp",
        "http://example.com/mcp?token=value",
        "file:///tmp/server",
    ):
        with pytest.raises(ValueError):
            MCPHTTPConfig("fixture", url)
    with pytest.raises(ValueError, match="forwarded"):
        MCPHTTPConfig("fixture", "http://127.0.0.1/mcp", forward_headers=frozenset({"Host"}))
    with pytest.raises(ValueError, match="boolean"):
        MCPHTTPConfig("fixture", "http://127.0.0.1/mcp", forward_authorization="false")
    with pytest.raises(ValueError, match="origins"):
        MCPHTTPConfig("fixture", "http://127.0.0.1/mcp", allowed_origins=frozenset({"*"}))


def test_expired_and_full_session_state_fails_conservatively():
    with pytest.raises(ValueError, match="HTTPS"):
        MCPHTTPConfig("fixture", "http://remote.example/mcp")
    from ordin.mcp_http import HTTPBoundaryError, MAX_HTTP_SESSIONS

    with MCPHTTPServer(MCPHTTPConfig("fixture", "http://127.0.0.1:1/mcp", port=0)) as server:
        old = server.new_session("identity", 1)
        server.release(old)
        old.touched -= server.config.session_ttl + 1
        with pytest.raises(HTTPBoundaryError) as expired:
            server.session(old.token, "identity")
        assert expired.value.status == 404
        for index in range(MAX_HTTP_SESSIONS):
            session = server.new_session("identity", index)
            server.release(session)
        with pytest.raises(HTTPBoundaryError) as full:
            server.new_session("identity", "extra")
        assert full.value.status == 503


def test_http_evaluation_separates_core_transport_and_upstream_delay():
    from ordin.http_evaluation import run_http_transport_evaluation

    report = run_http_transport_evaluation(upstream_delay=0.01)
    assert report.errors == []
    timings = report.as_dict()["latency_ms"]
    assert timings["core_review_p50"] > 0
    assert timings["http_round_trip_p50"] > 0
    assert timings["upstream_work_when_forwarded_p50"] >= 8
    assert timings["transport_and_session_overhead_p50"] >= 0


def test_incomplete_sse_response_fails_without_forwarding_or_claiming_success():
    with _upstream("bad-sse") as (url, state), _proxy(url) as server:
        token = _initialize(server)
        status, _, body = _request(server, _call(), token=token)
        assert status == 200 and b'"jsonrpc"' not in body
        assert _request(server, _call(2), token=token)[0] == 409
        assert (
            server._sessions[token].reviewer.session.snapshot()["observations"]["observations"]
            == []
        )
