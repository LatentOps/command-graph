# Reviewed MCP contract pins

An exact server/tool name does not prove that a server still exposes the contract
you reviewed. `--contract-lock` adds a second check: a complete, correlated
`tools/list` inventory must match a local reviewed pin before a tool call can proceed.
Discovery supplies data to compare. It never grants effects or resource permissions.

The proxy reuses `AgentGate`, temporal history, action policies, audit, and the
existing MCP parser. A missing, unpinned, changed, ambiguous, or unverifiable
contract returns `ask` with JSON-RPC error `-32040`. `AgentGate` and `Ordin.allows()`
require escalation even with `fail_on="block"`. An existing explicit block remains
blocked. Audit records contain the final guarded decision, with a machine-readable
`mcp.contract.*` provenance record and expected/observed digests.

## Local workflow

The starter-kit fixture has reviewed semantics and matching contract data:

```bash
ordin contracts digest examples/integrations/mcp-tools.json --json
ordin contracts validate examples/integrations/mcp-contract-lock.json --json
ordin contracts diff examples/integrations/mcp-contract-lock.json \
  examples/integrations/mcp-tools.json --server-id starter-kit \
  --semantics examples/integrations/mcp-semantics.json --json
ordin-mcp-proxy --server-id starter-kit \
  --semantics examples/integrations/mcp-semantics.json \
  --contract-lock examples/integrations/mcp-contract-lock.json \
  -- python examples/integrations/fixture_mcp_server.py
```

These commands use local data and a local fixture. The MCP client must finish
`tools/list` before it calls `read_note`. Digest/validate/diff never invoke a tool
or change trust files. Diff exits 0 for a match, 1 for drift, and 2 for malformed
input. It reports missing and unpinned tools, inventory completeness, and a changed
semantics binding. Digests contain no call arguments.

For another server, review its exact input/output contracts and author its effect
and resource mappings first. A pin can then be created explicitly in Python:

```python
import json
from pathlib import Path
from ordin.mcp_contracts import (
    MCPContractLock, load_contract_json, semantics_binding_digest,
    tool_contract_digest,
)
from ordin.tool_calls import load_tool_semantics

inventory = load_contract_json("reviewed-tools.json")
semantics = load_tool_semantics("reviewed-semantics.json")
lock = MCPContractLock(
    semantics_binding_digest(semantics),
    {("my-exact-server", tool["name"]): tool_contract_digest(tool)
     for tool in inventory["tools"]},
)
Path("reviewed-contract-lock.json").write_text(
    json.dumps(lock.as_dict(), indent=2) + "\n", encoding="utf-8"
)
```

This example assumes the inventory has already been checked for completeness and
duplicate names with `ordin contracts digest`. If you explicitly configure shell
tools, pass the same `frozenset` of exact names to `semantics_binding_digest` and
`--shell-tool` to the proxy/diff command. A different semantics registry or shell
mapping cannot reuse the old lock's binding.

## What the digest covers

The versioned `ordin.mcp_contract_lock.v1` format preserves exact server/tool
identities and binds them to SHA-256 digests under `ordin.mcp_contract.v1`:

- the exact tool name;
- the input schema and optional output schema;
- behavioral annotations, including invocation hints;
- execution metadata, including task support;
- unknown future tool fields, conservatively.

Top-level descriptions, display titles, icons, and annotation titles are omitted.
Schema `description`, `title`, `$comment`, and `examples` are omitted only at schema
nodes. A property *named* `description` remains part of the contract. Prose cannot
become an effect mapping.

Object keys are sorted, required-property lists are sorted, and integral JSON
numbers normalize to integers. Other array order is retained. This is a conservative
structural digest, not a proof of JSON Schema equivalence. Adding even an optional
argument changes the digest. Different defaults, constraints, or types also change it.

The supported profile requires explicit object roots and bounded JSON Schema
2020-12 or draft-07 structures. Local object-pointer references are supported;
remote and dynamic references fail as unverifiable. The implementation validates
structural keyword types and bounds, without fetching schemas or executing regular
expressions. It does not validate call arguments against the entire JSON Schema
standard or prove that the upstream implementation follows the advertised contract.
Unsupported schema constructs require review; they never silently inherit a pin.

Each contract/file is bounded to 1 MiB, 32 nesting levels, and 1,024 entries per
object/array. A discovery inventory holds at most 256 tools and 256 pages. Floats
outside the canonical numeric range are rejected, and the strict MCP parser rejects
decimal precision loss, duplicate JSON members, nonfinite values, and excessive
nesting. JSON strings and exact identities are never case-folded or trimmed.

## Discovery and invalidation

The observer accepts only replies correlated to an outstanding client `tools/list`
request. Paginated results remain unverified until the final page. Duplicate tool
names, repeated/mismatched cursors, malformed schemas, or list-change notifications
invalidate the inventory. Old and unsolicited responses cannot establish trust.
The proxy reserves request IDs across protocol methods to avoid attributing a
discovery response to a tool execution.

The transport follows the [MCP tools discovery contract](https://modelcontextprotocol.io/specification/2025-11-25/server/tools).
The lock's protocol revision is review metadata, not an automatic protocol
negotiator. A server must report contract changes honestly; the pin does not attest
to server code, credentials, or behavior. Review/execution TOCTOU and a malicious
server advertising an unchanged schema still require host isolation and policy.

Without `--contract-lock`, existing explicit unpinned semantics retain their prior
behavior. Unknown tools still require approval. Supplying a lock enables strict
pinning for every tool in that proxy, including explicitly configured shell tools.

## Library and regression use

Embedders may pass a verified `MCPContractCheck` to `AgentGate.evaluate_action`,
`Ordin.review_action`, or `IntegrationSession.evaluate`. This is trusted runtime
evidence supplied separately from tool arguments. The check must bind the exact
action identity. Hosts must use the guarded disposition/`Ordin.allows()` result
instead of applying a permissive raw decision threshold themselves.

Trajectory and promoted-regression steps may include `contract_check` with the
same redacted fields. This preserves drift cases in the existing action model.
Conformance and integration evaluation exercise a matched contract followed by
schema drift through the maintained proxy, in addition to standalone boundary tests.
