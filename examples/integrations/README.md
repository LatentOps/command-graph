# Ordin integration starter kit

These examples are small, runnable starting points for real integrations. They deliberately keep execution, credentials, sandboxing, approval UI, retries, and runtime state outside Ordin.

## 1. Five-minute Python quickstart

From an installed Ordin checkout:

```bash
python examples/integrations/quickstart.py
```

Expected shape:

```text
decision=allow disposition=execute
ordin quickstart
observation=ordin.action_observation.v1 exit=0
```

The example reviews a shell action first. Only after Ordin returns `execute` does the caller invoke `sh`. The caller then records a linked `ActionObservation`. Ordin itself never executes the command.

## 2. Raw JSON / subprocess boundary

A non-Python runtime can use the versioned JSON interface instead of importing Ordin:

```bash
python examples/integrations/json_subprocess.py
```

The example sends an `ordin.action_envelope.v1` payload to:

```bash
ordin action --stdin --json
```

A TypeScript, Go, Rust, Java, or other runtime can use the same stdin/stdout contract.

## 3. MCP proxy

The fixture below requires no network access or credentials:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"read_note","arguments":{"path":"README.md"}}}' \
| ordin-mcp-proxy \
    --server-id starter-kit \
    --semantics examples/integrations/mcp-semantics.json \
    -- \
    python examples/integrations/fixture_mcp_server.py
```

The original `tools/call` reaches the fixture server only because its exact `(server, tool)` identity has reviewed local semantics and Ordin permits the action.

For a real MCP server, replace the fixture command and semantics file while keeping the same proxy boundary. See [`docs/mcp-safety-proxy.md`](../../docs/mcp-safety-proxy.md).

## 4. Shell or coding-agent gate

For direct Python embedding, `AgentGate` is the smallest boundary:

```python
from ordin import AgentGate

result = AgentGate().evaluate(
    "git status --short",
    intent="inspect repository state",
)

if result.may_execute:
    # Caller-owned execution layer.
    ...
elif result.requires_approval:
    # Caller-owned approval layer.
    ...
else:
    # Deny the action.
    ...
```

For Claude Code, use the maintained hook integration in [`docs/claude-code-integration.md`](../../docs/claude-code-integration.md).

## 5. CI gate

Use Ordin as a deterministic pre-execution check without executing the command:

```bash
python examples/integrations/ci_gate.py "git status --short"
```

The process exits `0` only when the configured `ReviewPolicy` permits automatic execution. `warn`, `ask`, and `block` therefore fail this conservative example.

## Optional local evidence

Persistence is off by default. Opt in explicitly only when you want local evidence:

```bash
mkdir -p .ordin
export ORDIN_CLAUDE_AUDIT="$PWD/.ordin/claude-audit.jsonl"
export ORDIN_CLAUDE_OBSERVATIONS="$PWD/.ordin/claude-observations.jsonl"
```

For MCP, pass `--audit` and/or `--observations` to `ordin-mcp-proxy`.

Audit output uses Ordin's redacted local JSONL contract. Do not treat audit persistence as a substitute for caller-owned execution logs or approval records.

## Capability recommendations

Generic action reviews expose `capabilities`, a deterministic recommendation for the minimum filesystem/network/process privileges the caller should consider granting. It is advisory: the caller's sandbox remains authoritative and may always grant less.

```python
review = result.review
if getattr(review, "capabilities", None) is not None:
    print(review.capabilities.as_dict())
```

## Safety rule

Every integration should preserve this control flow:

```text
agent proposes action
        |
        v
      Ordin
        |
        +--> execute only if caller policy permits
        +--> escalate on warn/ask when policy requires it
        +--> deny block
        |
        v
caller-owned runtime
```

Never turn an adapter exception, unknown tool, malformed identity, or missing context into implicit execution.
