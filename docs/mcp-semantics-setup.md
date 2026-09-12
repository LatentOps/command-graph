# Inspect and configure MCP semantics

For the complete fixture flow from an installed wheel, start with the
[v0.3 quickstart](quickstart.md). This guide is the manual reference for
reviewing and configuring real servers.

The setup commands collect a server's tool contracts and create a local review
worksheet. They never infer permissions from tool names, descriptions, or discovery
hints. A generated semantics file starts with **zero trusted rules**; unknown tools
continue to require approval.

## Filesystem-like example

From an Ordin development checkout, create a scratch directory for these files and
inspect the included no-network server:

```bash
ordin mcp inspect --server-id starter-kit --output inventory.json \
  -- python examples/integrations/fixture_mcp_server.py
ordin semantics scaffold inventory.json --output semantics.json
```

This writes `semantics.json` plus `semantics.review.json`. The worksheet includes
exact server/tool identities, observed contract digests, string argument paths, and
unresolved `effects`/`resources` fields. It is a worksheet, not executable semantics.
The inventory preserves canonical input/output schemas and invocation metadata;
display prose is omitted under the [contract canonicalization policy](mcp-contract-pinning.md).

Review the actual tool implementation/contract before filling `semantics.json`.
For this supplied fixture, a completed mapping is:

```json
{
  "schema_version": "ordin.tool_semantics.v1",
  "registry_id": "reviewed-starter-kit",
  "version": "1",
  "rules": [{
    "id": "read-note",
    "kind": "mcp",
    "server": "starter-kit",
    "tool": "read_note",
    "effects": ["filesystem.read"],
    "resources": [{"argument": "path", "type": "path"}]
  }]
}
```

Validate, pin the reviewed mapping, and run the proxy:

```bash
ordin semantics validate semantics.json --inventory inventory.json --json
ordin semantics lock semantics.json --inventory inventory.json --output contracts.json
ordin-mcp-proxy --server-id starter-kit --semantics semantics.json \
  --contract-lock contracts.json -- python examples/integrations/fixture_mcp_server.py
```

The MCP client must complete `tools/list` before calling a pinned tool. The fixture
returns a fixed note and does not read a private file. The reviewed rule is specific
to this example; it is not inferred from the name `read_note`.

## Explicit shell example

The shell fixture advertises a `command` argument but never executes it. Shell
classification requires an explicit user selection:

```bash
ordin mcp inspect --server-id shell-fixture --output shell-inventory.json \
  -- python examples/integrations/fixture_mcp_shell_server.py
ordin semantics scaffold shell-inventory.json --output shell-semantics.json \
  --shell-tool execute
ordin semantics validate shell-semantics.json --inventory shell-inventory.json \
  --shell-tool execute
ordin semantics lock shell-semantics.json --inventory shell-inventory.json \
  --shell-tool execute --output shell-contracts.json
ordin-mcp-proxy --server-id shell-fixture --semantics shell-semantics.json \
  --contract-lock shell-contracts.json --shell-tool execute \
  -- python examples/integrations/fixture_mcp_shell_server.py
```

Here the semantics registry can remain empty: the explicitly selected tool uses
Ordin's existing shell adapter, which reviews the actual command argument. A tool
named `execute` receives no shell mapping unless the user supplies `--shell-tool`.
The selected contract must declare a string `command` argument. Other fields and
server behavior still require review; contract metadata is not proof of execution.

## Diff against the live server

Use an existing inventory for offline checks, or collect live metadata in the same
command:

```bash
ordin semantics diff semantics.json --contract-lock contracts.json \
  --server-id starter-kit --inspect-command python examples/integrations/fixture_mcp_server.py
```

Place `--inspect-command` last; its remaining arguments are passed as an argument
array with `shell=False`. Diff/validate report missing or removed tools, exact server
mismatches, unresolved resource bindings, missing/changed pins, and changed semantics
bindings. Unknown effects and invalid rule/binding fields fail schema validation.
Exit 0 means checks passed, 1 means mismatches need review, and 2 means invalid input
or inspection failure. All outputs are machine-readable JSON.

Resource candidates are declared string properties. Names alone never assign a
resource type. Nested object paths use the existing dotted binding syntax; property
names containing dots and array traversal are not guessed into bindings. Review
optional/default argument behavior and retain the host's sandbox boundaries.

## Bounds and privacy

Inspection sends `initialize`, `notifications/initialized`, and paginated
`tools/list`, following [MCP discovery](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).
It rejects wrong response IDs, duplicate identities, malformed contracts, repeated
cursors, excessive pages/messages, and oversized responses. Server-initiated client
requests receive an unsupported-method response; the inspector never sends a
`tools/call`. Reads and writes run through bounded queues so a server that stops
reading cannot bypass the whole-inspection timeout. Bulk pipe reads avoid one system
call per metadata byte.

The default deadline is 10 seconds and can be explicitly increased to 300 seconds.
The inventory is limited to 256 tools/pages and 1 MiB per file/response, with the
contract layer's nesting bounds. New output files use mode `0600` on POSIX and are
never overwritten. The server subprocess is stopped when inspection finishes.

Server stderr, command arguments, environment values, prompts, and tool-result
payloads are not recorded. Display prose is dropped. Common credential literals and
sensitive metadata/default fields cause output to be withheld rather than written.
These checks are not a universal secret detector: review metadata before sharing
it, and keep scratch inventories local. Only launch server programs you trust;
starting a server can itself have side effects and is not an Ordin sandbox.

The inspection, scaffold, and lock commands do not install policy or approve a
runtime call. Users explicitly edit mappings and choose which reviewed files to
load. Live contract pinning remains necessary because a captured inventory can
become stale between setup and execution.
