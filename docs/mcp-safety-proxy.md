# MCP safety proxy

Start with [inspection and semantics setup](mcp-semantics-setup.md) to obtain a
reviewable local draft without granting trust to discovery metadata.

For remote or local HTTP servers, use the [Streamable HTTP proxy](mcp-http-proxy.md).
Both transports use the same action review, policy, contract, and session engine.

Use [`--contract-lock`](mcp-contract-pinning.md) to require a reviewed live tool
contract before an exact tool identity can inherit trusted semantics.

Each proxy connection carries bounded in-memory action and observation history.
See [live integration sessions](integration-sessions.md) for temporal review, reset,
correlation limits, and state ownership.

Ordin can run as a local stdio proxy between an MCP client and an upstream MCP server:

```text
MCP client
    |
    | newline-delimited JSON-RPC
    v
ordin-mcp-proxy
    |
    +-- tools/call --> Ordin review --> forward / escalate / deny
    |
    +-- other MCP messages -------------------------------> forward unchanged
    |
    v
upstream MCP server
```

The proxy is intentionally transport-small. It supports the standard local **stdio** transport first and does not add an MCP SDK dependency, hosted service, credential store, or public listener.

The current MCP protocol revision (`2026-07-28`) moved the core toward stateless self-describing requests, while maintained stdio SDKs continue to support both modern and legacy connection eras. Ordin does not terminate or reinterpret that lifecycle. It forwards non-tool JSON-RPC messages unchanged and reviews only `tools/call` before the upstream server can execute the tool.

Protocol references:

- <https://modelcontextprotocol.io/>
- <https://blog.modelcontextprotocol.io/posts/2026-07-28/>

## Basic usage

Point an MCP client at Ordin instead of directly at the upstream server:

```bash
ordin-mcp-proxy \
  --server-id filesystem-local \
  --semantics ./filesystem-semantics.json \
  -- \
  python -m my_mcp_server
```

Everything after `--` is the upstream stdio server command. The proxy starts that process, forwards its stderr to the caller's stderr, and keeps stdout reserved for valid MCP JSON-RPC messages.

The proxy exits when its upstream server exits, even if the client keeps stdin
open. It preserves the server's exit code; an upstream protocol failure returns
exit code `1`. Client EOF still gives the server the configured
`--shutdown-timeout` to finish before it is terminated.

`--server-id` is required and is part of the safety identity. Trusted semantics match an exact `(kind="mcp", server, tool)` tuple. A semantics file for another server identity does not transfer trust to this proxy instance.

Identities retain their original whitespace. For example, `read_file` and
` read_file ` are different tools; the latter cannot inherit the former's
trusted semantics or shell mapping.

## Tool semantics

Without trusted local semantics, a generic MCP tool remains uncertain and Ordin returns an approval-required error instead of forwarding it.

Example:

```json
{
  "schema_version": "ordin.tool_semantics.v1",
  "registry_id": "filesystem-local",
  "version": "1",
  "rules": [
    {
      "id": "read-file",
      "kind": "mcp",
      "server": "filesystem-local",
      "tool": "read_file",
      "effects": ["filesystem.read"],
      "resources": [
        {"argument": "path", "type": "path"}
      ]
    }
  ]
}
```

Run it with:

```bash
ordin-mcp-proxy \
  --server-id filesystem-local \
  --semantics ./filesystem-semantics.json \
  -- \
  python -m my_mcp_server
```

The semantics loader uses Ordin's existing versioned schema, effect catalog, bounds, and exact identity matching.

## Shell-execution tools

If an MCP server has a tool whose documented contract is exactly shell command execution, opt it in by exact name:

```bash
ordin-mcp-proxy \
  --server-id local-shell \
  --shell-tool execute_command \
  -- \
  python -m shell_mcp_server
```

Only `execute_command` is unwrapped. Its command text goes through Ordin's existing shell parser and safety engine. Similar-looking names are not inferred.

## Decision behavior

By default the proxy uses `--fail-on warn`, so only Ordin `allow` decisions are forwarded upstream.

| Ordin review | Default proxy behavior |
| --- | --- |
| `allow` | forward the original `tools/call` byte-for-byte |
| `warn` | return approval-required JSON-RPC error |
| `ask` | return approval-required JSON-RPC error |
| `block` | return blocked JSON-RPC error |

The local errors use implementation-defined JSON-RPC server codes:

- `-32040`: Ordin approval required
- `-32041`: Ordin blocked the tool call

Their `error.data.ordin` object includes the decision, risk, bounded reasons, and action ID, but never copies tool arguments into the error.

Caller-controlled enforcement can be changed explicitly:

```bash
ordin-mcp-proxy --server-id example --fail-on ask -- python -m server
```

`--fail-on ask` permits `warn` but still escalates `ask`; `--fail-on block` permits both `warn` and `ask`. An explicit Ordin `block` is always denied regardless of threshold.

The proxy does not provide an approval UI. Returning `-32040` is the escalation boundary for a client or host that owns human approval. A caller that deliberately chooses a less conservative threshold owns that policy decision.

## Declarative policy

An existing Ordin policy can strengthen tool decisions:

```bash
ordin-mcp-proxy \
  --server-id filesystem-local \
  --semantics ./filesystem-semantics.json \
  --policy ./policy.json \
  -- \
  python -m my_mcp_server
```

Policy remains data-only and cannot weaken a stronger core safety result.

## Protocol transparency

For valid stdio MCP messages, the proxy forwards the following without changing their JSON bytes:

- tool/resource/prompt discovery;
- legacy initialization and modern discovery messages;
- notifications;
- cancellation traffic;
- responses and server-to-client messages used by compatible protocol eras;
- task and extension methods it does not itself interpret.

Only `tools/call` is intercepted before upstream execution. Allowed tool calls are forwarded using the original line received from the client, not a reserialized replacement.

The proxy accepts the current individual-message stdio framing: one UTF-8 JSON-RPC object per newline, with no embedded newline. It rejects invalid JSON, non-object messages, malformed `tools/call` requests, duplicate in-flight tool request IDs, and messages above the 10 MiB transport bound instead of forwarding ambiguous input.

Both transport directions reject duplicate JSON object members, non-finite
numbers (including overflow to infinity), and nesting deeper than 64 levels
below the outer object. This prevents the review from selecting one duplicate
value while a different parser selects another from the forwarded bytes.
Malformed client messages receive a parse error, and later valid messages can
continue. Malformed upstream output stops the proxy without forwarding it.

## Post-action observations

For an allowed `tools/call`, Ordin records an in-memory correlation from the JSON-RPC request ID to the action ID. When the upstream server returns the matching result, the proxy can emit `ordin.action_observation.v1` evidence.

Numeric IDs match by value: `1` and `1.0` identify the same pending request,
including for duplicate detection. String IDs such as `"1"` remain distinct,
and integer IDs retain their precision.

Persistence is disabled by default. To opt in:

```bash
mkdir -p .ordin
ordin-mcp-proxy \
  --server-id filesystem-local \
  --semantics ./filesystem-semantics.json \
  --observations "$PWD/.ordin/mcp-observations.jsonl" \
  -- \
  python -m my_mcp_server
```

Observation metadata includes only bounded identity/status facts such as server, tool, hashed request ID, and result class. It does **not** persist tool result content or protocol error text.

Result handling is conservative:

- ordinary result -> `success` / exit code `0`;
- MCP tool result with `isError: true` -> `tool_error` / exit code `1`;
- JSON-RPC protocol error -> `protocol_error` / unknown exit code;
- asynchronous task handle -> `task_accepted` / unknown exit code rather than a false completion claim.

Task polling continues transparently through the client and server. The first proxy version does not claim the eventual task result is the same event as initial task acceptance.

## Optional audit evidence

Decision audit persistence is also explicit:

```bash
mkdir -p .ordin
ordin-mcp-proxy \
  --server-id filesystem-local \
  --audit "$PWD/.ordin/mcp-audit.jsonl" \
  -- \
  python -m my_mcp_server
```

This uses the existing redacted `JsonlAuditSink`. Ordin never uploads audit or observation data.

## Security boundary

The proxy deliberately does not:

- bind a public network port;
- implement OAuth or store bearer tokens;
- inspect credential environment variables;
- execute tool logic itself;
- retry an uncertain or blocked call;
- invent semantics from tool descriptions or names;
- treat discovery output as trusted execution semantics;
- persist raw arguments, results, or protocol errors by default.

The proxy process necessarily launches and transports bytes to the configured upstream stdio server. That transport role does not move credential or tool-execution authority into Ordin's review engine. The upstream command and environment remain caller configuration.

## Current limitations

- stdio only; Streamable HTTP is not proxied yet;
- one upstream server process per proxy instance;
- no built-in approval user interface;
- no automatic trust derivation from `tools/list` metadata;
- asynchronous task acceptance is observed, but eventual task completion is not yet correlated back to the initiating action;
- malformed upstream stdout is treated as a protocol failure and is never copied to client stdout.

These limits keep the first proxy auditable and fail closed while preserving current MCP traffic that Ordin does not need to understand.
