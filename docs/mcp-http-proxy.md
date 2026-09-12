# MCP Streamable HTTP proxy

The [offline quickstart](quickstart.md) includes an installed-wheel loopback
check. No external MCP service or credentials are needed for that fixture.

`ordin-mcp-http` adds a local HTTP boundary around the existing MCP review engine.
It shares exact tool identities, `AgentGate`, policies, bounded session history,
contract pins, and redacted observations with the stdio proxy. It reviews
`tools/call` before forwarding; the upstream server owns execution.

The upstream working directory is unknown by default. Supply `--cwd` only when
the host can establish it; the proxy never assumes its own local directory is the
remote execution directory.

## Start a local proxy

```bash
ordin-mcp-http --server-id workspace \
  --upstream https://mcp.example.com/mcp \
  --semantics reviewed-semantics.json \
  --contract-lock reviewed-contracts.json \
  --port 8766
```

Point the MCP client at `http://127.0.0.1:8766/mcp`. The default listener is
loopback-only. Remote upstreams require HTTPS unless the operator explicitly uses
`--allow-insecure-upstream` for a separately protected network. Redirects are never
followed. Upstream URLs cannot embed credentials, query strings, or fragments.

To exercise a non-executing local fixture in two terminals:

```bash
python -m ordin.http_evaluation --port 8787
ordin-mcp-http --server-id fixture --upstream http://127.0.0.1:8787/mcp \
  --semantics examples/integrations/http-semantics.json --shell-tool shell --port 8766
```

The fixture returns a fixed result; it never executes the quoted shell command.
The client first initializes the connection, receives a proxy session ID, and uses
that ID on subsequent requests. With pins enabled, it must also complete `tools/list`.

## Supported transport profile

The proxy implements [MCP Streamable HTTP 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)
with the following explicit profile:

| Behavior | Support |
| --- | --- |
| POST JSON-RPC request and JSON result | Supported |
| POST SSE result, including initialization | Supported; event JSON is validated before forwarding |
| Notifications and client responses | Forwarded; upstream must return 202 with no body |
| GET SSE notifications/server requests | Supported when the upstream supports GET; otherwise 405 propagates |
| GET resumption with `Last-Event-ID` | Forwarded to the same bound upstream session; no automatic tool retry |
| Resumed initialization | Supported; tool calls remain unavailable until a valid initialize result arrives |
| Explicit cancellation notifications | Forwarded; disconnecting does not mean cancellation |
| DELETE session | Closes local state and requests upstream termination; local close still works if upstream returns 405 |
| Stateless upstream | Supported; the proxy still provides bounded downstream sessions |
| Chunked request bodies | Supported with bounds; trailers are rejected |
| Task-augmented calls / `tasks/*` | Rejected before forwarding; task capability is removed from initialization |
| Legacy 2024 HTTP+SSE transport | Not implemented |
| Compressed bodies or protocol upgrades | Not implemented |

Clients must accept both `application/json` and `text/event-stream` for POST, and
`text/event-stream` for GET. They should send the negotiated
`MCP-Protocol-Version: 2025-11-25`. Unsupported revisions are rejected. The endpoint
is exactly `/mcp`; query routes are not interpreted.

Asynchronous task/input-required results outside this profile fail conservatively.
The proxy does not claim a task acceptance is completed execution. Unrecognized
upstream result-type strings are omitted from observations rather than persisted.

## Identity, authorization, and state

Each downstream session receives a cryptographically random token, separate from
the upstream session ID. It binds one fixed upstream endpoint, exact server label,
and forwarded header values. Sessions cannot transfer between proxy instances or
different credentials. Initialization also works with an upstream that does not
issue a session ID. Raw session IDs are held in memory, not written to evidence.

Authorization forwarding is opt-in:

```bash
ordin-mcp-http --server-id workspace --upstream https://mcp.example.com/mcp \
  --semantics reviewed-semantics.json --forward-authorization
```

The client supplies its own `Authorization` header. Ordin does not discover or store
tokens, refresh OAuth credentials, or manage cookies. A supplied authorization
header is rejected if forwarding is disabled. `--forward-header NAME` permits
additional stable per-session context/auth headers; reserved routing, framing, and
cookie headers cannot be forwarded. Changing forwarded values requires a new session.
They are hashed for session binding and absent from default audit/diagnostics.

The proxy validates `Host` and `Origin` before processing requests. Browser preflight
is limited to permitted methods and headers, with exact allowed origins. Public
binding requires both `--allow-public` and explicit `--allow-host` entries; browser
origins can be added with `--allow-origin`. These controls are not an authentication
provider. Protect any non-loopback deployment with transport security, upstream
authentication, and host/network access controls. Loopback does not isolate local
operating-system users from one another.

Every session has its own shared 32-action review window and linked observations.
At most 32 tool calls and 32 other protocol requests can be in flight per session.
Unknown/malformed tool calls fail closed, warnings escalate according to
`--fail-on`, and explicit blocks never reach upstream. Upstream transport/protocol failures during JSON exchanges make
the session unavailable for further tool calls until reset; cancellation and
deletion remain possible. SSE correlation survives a resumable disconnect.

## Bounds and evidence

- 32 concurrent HTTP connections and 64 live sessions.
- 16 KiB aggregate headers, 4 KiB request line, 10 MiB JSON body/event.
- 64 MiB maximum data per SSE stream and 10,000 request chunks.
- Idle session expiry defaults to 1,800 seconds, configurable with `--session-ttl`.
- Socket and upstream exchange deadlines default to 30 seconds, configurable with
  `--timeout` up to 300 seconds. The client connection has one second of additional
  grace for an error response. Timers stop slow-drip input/streams; the OS resolver
  still owns DNS lookup timing. Calls are not sent after an expired connect deadline.

The proxy neither buffers an entire SSE stream nor keeps an unbounded request map.
Session capacity, malformed JSON, duplicate headers/members, unsupported framing,
and unknown correlations cannot silently turn into allowed execution.

Optional `--audit` and `--observations` use the existing local evidence contracts.
HTTP observations identify runtime `mcp-http` and correlate to reviewed action IDs.
Raw arguments, tool output, authorization, and cookies are absent from that evidence.
Upstream application payloads are relayed to the requesting client; they can themselves
be sensitive. Access logs and raw transport error bodies are not persisted. Proxy
errors expose bounded error codes without upstream stack traces or credentials.

MCP contract pinning verifies advertised metadata, not server implementation behavior.
TOCTOU, an upstream that lies about its contract, and side effects already started
before a timeout still require host controls and caller review. Closing a connection
does not undo an upstream action.

## Validation and performance

Permanent tests use only local HTTP fixtures and cover JSON/SSE, chunked bodies,
initialization/resumption, authorization isolation, contract drift, cancellation,
timeouts, malformed input, bounds, and evidence redaction. Conformance runs real
loopback controls, and runtime evaluation reports HTTP separately from hook/proxy
subprocess startup:

```bash
python scripts/run_runtime_evaluation.py --repetitions 5 --json-out runtime-report.json
```

`http_transport.latency_ms` reports core review, HTTP round trip, upstream work when
forwarded, and an estimate of transport/session overhead. Artificial upstream delay
is measured separately. These local measurements do not include model inference or
claim cross-machine performance.
