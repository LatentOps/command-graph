# Integration troubleshooting

Use Ordin diagnostics to understand integration failures without weakening the safety decision or exposing raw action payloads.

## 1. Verify first-party integration health

Run:

```bash
python scripts/diagnose_integration.py health
```

For CI or tooling:

```bash
python scripts/diagnose_integration.py health --json
```

This executes the same shared conformance contract used by the maintained Claude Code and MCP integrations. A failure identifies the integration and invariant that did not hold, for example identity binding, malformed-input fail-closed behavior, decision mapping, or observation linkage.

This is a local deterministic check. It does not contact a hosted Ordin service or require credentials.

## 2. Inspect a reviewed generic action

Save an `ordin.action_envelope.v1` payload and run:

```bash
python scripts/diagnose_integration.py action --file action.json
```

Or pipe it through stdin:

```bash
cat action.json | python scripts/diagnose_integration.py action --stdin --json
```

The diagnostic reports:

- action kind and operation;
- runtime/server/tool identity when present;
- parameter *names*, never raw parameter values;
- final decision, risk and uncertainty;
- effects and resource types, never resource values;
- the semantic adapter that matched, if any;
- trajectory and policy contributors;
- capability recommendation summary;
- provenance record count and final provenance decision;
- remediation appropriate to the result.

## 3. Read the failure category correctly

### `uncertain_semantics`

Ordin could not establish enough deterministic semantics. For a tool or MCP action, check the exact runtime/server/tool identity and the configured trusted semantics. Do not solve this by lowering the review threshold.

### `untrusted_tool_identity`

No trusted tool-semantics rule matched the exact identity. This is expected for a new or mutated integration identity. Register reviewed semantics for the correct identity or route the action through caller-owned approval.

### `approval_required`

The action is understood but its effects or policy require review. The integrating runtime owns the approval UI and execution decision after approval.

### `blocked_action`

Do not execute the action. Change the action or satisfy the safety requirement. Diagnostics never provide a bypass path for `block`.

## 4. Separate the layers

When debugging, keep these sources distinct:

1. **Core safety finding**: the effects, resources, risk and base decision Ordin inferred.
2. **Declarative/temporal policy**: explicit caller policy that preserved or strengthened the decision.
3. **Integration identity/adapter**: whether trusted runtime/tool semantics matched.
4. **Runtime failure**: transport, process, server, credential or execution errors outside Ordin's review engine.
5. **Post-action observation**: caller-supplied evidence about what actually happened after execution.

Do not reinterpret a transport failure as a safe action, and do not reinterpret an `allow` review as proof that an external effect succeeded.

## 5. Redaction boundary

The diagnostic format intentionally excludes raw action parameter values and resource values. This means command strings, file paths supplied through arguments, tool output, request bodies and credentials are not copied into the diagnostic payload.

Integration identity names and action IDs are included because they are needed to diagnose routing. Callers that consider those identifiers sensitive should apply their own local log policy.

The redaction boundary is a safety aid, not a complete secret scanner. Continue to avoid placing credentials in action metadata, fixtures or audit files.

## 6. If the behavior is a real Ordin bug

Promote the smallest sanitized reproducer through the failure replay workflow in [Failure replay and regression promotion](failure-regressions.md). The intended loop is:

```text
integration failure
-> redacted deterministic reproducer
-> failing regression
-> repair
-> permanent regression coverage
```

Run [Integration conformance](integration-conformance.md) and the safety benchmark again before merge.
