# Integration conformance

Ordin integrations must preserve the same safety boundary even when their transport and runtime APIs differ.

The shared conformance suite exercises the maintained Claude Code and MCP integrations against one set of boundary invariants. It is intended to be the admission test for future first-party adapters as well.

Run it locally with:

```bash
python scripts/run_integration_conformance.py
```

Write a machine-readable result with:

```bash
python scripts/run_integration_conformance.py \
  --json-out integration-conformance-report.json
```

A non-zero exit means at least one integration invariant failed.

## Contract

A conforming integration must preserve, where the runtime exposes the data:

- action kind and operation;
- exact runtime/server/tool identity;
- structured arguments and resources;
- caller-supplied execution context;
- deterministic Ordin decision mapping;
- fail-closed handling for malformed or identity-mismatched inputs;
- explicit non-execution of `block` decisions;
- approval/escalation behavior for uncertain or mutating actions;
- trusted tool semantics only under the reviewed identity;
- execution-capability recommendations and decision provenance;
- post-action observation linkage without retaining raw sensitive tool output.

The suite also exercises non-effecting protocol forwarding where an integration acts as a transport proxy.

## Current first-party adapters

### Claude Code

The harness verifies a known read-only tool, a mutating write, a blocked shell command, malformed hook input, an intentionally mutated runtime identity, and a linked post-tool observation.

The identity mutation is important: trusted semantics registered for `claude-code` must not transfer to `claude-code-mutated` simply because the tool name and arguments are otherwise identical.

### MCP proxy

The harness verifies transparent non-effecting protocol forwarding, exact server/tool argument preservation, a known read-only tool, an unknown tool, a deliberately mismatched server identity, a blocked shell-backed tool, malformed `tools/call`, and redacted result observation.

A blocked or malformed call must never be placed in the proxy's pending/upstream execution set.

## Adding another adapter

A future first-party adapter should extend the shared conformance harness instead of copying these tests into an unrelated suite.

At minimum, add deterministic offline scenarios for:

1. one known low-risk action;
2. one mutating or uncertain action that escalates;
3. one known blocked action when the runtime can represent it;
4. malformed input;
5. identity mutation or mismatch;
6. argument/resource preservation;
7. execution context preservation where available;
8. capability/provenance propagation;
9. post-action observation linkage and redaction when the runtime exposes results.

If an integration cannot expose one of these dimensions, document the limitation instead of synthesizing evidence or silently declaring the invariant satisfied.

The conformance suite does not execute external actions, require network credentials, or turn adapter compliance into a claim of universal agent safety. It verifies that the integration preserves Ordin's declared boundary correctly.
