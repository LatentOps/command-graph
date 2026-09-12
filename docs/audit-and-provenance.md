# Decision provenance and local audit evidence

Hash-chain verification checks internal consistency. For concurrent writers,
private-file requirements, trusted head checkpoints, and the limits of local
evidence, see the [audit threat model](threat-model.md#local-evidence).

Ordin can explain how a generic action review reached its final decision and can optionally append that evidence to a local JSONL audit file. These features do not give Ordin execution authority and do not add telemetry or a hosted control plane.

## First-class decision provenance

Every review returned by `Ordin.review_action()` includes `review.provenance` using the versioned `ordin.provenance.v1` contract.

```python
from ordin import ActionEnvelope, Ordin

review = Ordin().review_action(
    ActionEnvelope.shell("git status --short")
)

for record in review.provenance.records:
    print(record.source, record.code, record.rule_id, record.effect)
```

Provenance records are derived from the same structured outputs that drive the review. Ordin does not parse its human-readable reason strings to reconstruct an explanation afterward.

Current provenance sources include:

- adapter classification;
- typed semantic effects and resources;
- matched risk rules;
- context findings that strengthen risk;
- intent mismatch findings;
- caller-supplied observations;
- temporal policy matches, including matched history indices;
- declarative action-policy matches;
- explicit decision-merge records.

The final provenance object repeats the final decision and risk so consumers can verify that the explanation and the returned review agree.

## Opt-in local JSONL audit

No audit sink is configured by default. Normal `Ordin()` review performs no audit writes.

```python
from ordin import ActionEnvelope, JsonlAuditSink, Ordin

sink = JsonlAuditSink(
    "ordin-audit.jsonl",
    hash_chain=True,
)

gate = Ordin(audit=sink)
gate.review_action(ActionEnvelope.shell("git status --short"))
```

The sink is local and append-only. It never uploads an action, contacts a remote service, or executes the reviewed action.

If an explicitly configured sink cannot write, the failure is surfaced to the caller. Ordin does not silently claim that audit evidence was recorded when it was not.

## Privacy defaults

Audit events use `ordin.audit_event.v1` and record an SHA-256 digest of the canonical action envelope rather than the raw action parameters.

By default the JSONL sink also:

- hashes provenance resource values;
- omits provenance summaries, which may contain paths or other caller data;
- omits caller-supplied action IDs;
- does not record caller observation metadata.

A caller that explicitly needs more local evidence may opt in:

```python
sink = JsonlAuditSink(
    "ordin-audit.jsonl",
    include_resource_values=True,
    include_summaries=True,
    include_action_ids=True,
)
```

Those options should only be enabled when the audit file is protected appropriately for the data it may contain. Resource hashing is deterministic redaction, not encryption; low-entropy values may still be guessable, so callers should protect the audit file as security-sensitive evidence.

## Hash chaining

`hash_chain=True` links each event to the previous event hash. Ordin verifies an existing chain before appending to it.

```python
from ordin import verify_audit_jsonl

verification = verify_audit_jsonl(
    "ordin-audit.jsonl",
    require_hash_chain=True,
)

if not verification.ok:
    print(verification.errors)
```

Verification detects modified events, invalid event hashes, broken links, reordered events, and removal of an event from the middle of a chain when later events remain.

Hash chaining is not a digital signature. Without separately anchoring the latest hash, it cannot prove that the entire tail of a local file was not truncated. Key management and remote attestation are intentionally outside Ordin core.

## Persistence and concurrency boundary

The sink opens the configured file in append mode, writes one canonical JSON object per line, and can `fsync` after each event. `fsync=True` is the default.

A `JsonlAuditSink` instance serializes its own writes with a local lock. Applications that need coordinated multi-process writers should provide that coordination outside Ordin rather than assuming the in-process lock is a distributed lock.

## Schemas

The machine contracts are:

- `ordin.provenance.v1` in `provenance.v1.schema.json`;
- `ordin.audit_event.v1` in `audit-event.v1.schema.json`;
- additive `provenance` output on `ordin.action_review.v1`.

Source schemas and packaged schemas are kept identical and are checked by `ordin doctor` and CI.
