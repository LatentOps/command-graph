# JSON Schema Contracts

Ordin publishes machine-readable JSON Schema contracts for its stable JSON surfaces. The checked-in schemas use JSON Schema Draft 2020-12 and are available under `schemas/`; installed packages carry the same files under `ordin/resources/schemas/`.

## Published contracts

- `command-card.v1.schema.json`
- `search-result.v1.schema.json`
- `risk-review.v1.schema.json`
- `review-result.v1.schema.json`
- `review-request.v1.schema.json`
- `action-envelope.v1.schema.json`
- `action-history.v1.schema.json`
- `action-review.v1.schema.json`
- `execution-capabilities.v1.schema.json`
- `action-observation.v1.schema.json`
- `observation-history.v1.schema.json`
- `provenance.v1.schema.json`
- `audit-event.v1.schema.json`
- `policy-set.v1.schema.json`
- `temporal-policy-set.v1.schema.json`
- `tool-semantics.v1.schema.json`
- `action-trace.v1.schema.json`
- `trace-event.v1.schema.json` — bounded private capture records
- `trace-candidate.v1.schema.json` — reviewed semantic reconstructions using existing regression contracts
- `risk-rules.v1.schema.json`
- `effect-catalog.v1.schema.json`
- `effect-graph.v1.schema.json`
- `command-pack.v1.schema.json`
- `pack-list.v1.schema.json`

## Versioning policy

A schema version identifies a machine contract, not the package release. Backward-compatible additions should be optional or otherwise preserve existing consumers. Removing fields, changing field types, changing required semantics, or reinterpreting an existing field requires a new schema version.

Command review requests use `ordin.review_request.v1`. Generic actions use `ordin.action_envelope.v1` and return `ordin.action_review.v1`. Generic bounded history uses `ordin.action_history.v1`. Execution capability recommendations use `ordin.execution_capabilities.v1`. Caller-supplied runtime evidence uses `ordin.action_observation.v1` inside `ordin.observation_history.v1`. Decision explanations use `ordin.provenance.v1`. Optional local audit entries use `ordin.audit_event.v1`. Declarative action policies use `ordin.policy_set.v1`. Temporal sequence policies use `ordin.temporal_policy_set.v1`. Trusted local tool semantics use `ordin.tool_semantics.v1`.

The generic action schema deliberately permits future action-kind identifiers. Schema acceptance does not imply semantic confidence: if Ordin has no deterministic adapter for an accepted kind/operation, review returns `ask` rather than `allow`.

Policy provenance is additive on `ordin.action_review.v1`: `policy` and `policy_matches` are emitted only when an explicit action policy is evaluated. Capability recommendations are emitted through the `capabilities` field. The normal Ordin review path derives this profile from typed effects/resources; the field remains nullable for compatibility with manually constructed review objects.

The additive `provenance` field on `ordin.action_review.v1` contains the structured explanation produced by the normal generic review path. It remains nullable for compatibility with manually constructed `ActionReview` values.

Observation payloads are caller assertions, not independently verified facts. Ordin requires observations to match exactly one prior `action_id` before temporal review may consume them.

Audit events contain an action digest and a redacted provenance view by default. The audit schema does not contain raw action parameters.

## Doctor validation

`ordin doctor` validates repository/package metadata in layers:

1. schema files are present and declare Draft 2020-12;
2. command cards, risk rules, effect catalogs, pack manifests, policy schemas, temporal policy schemas, tool semantics schemas, execution-evidence schemas, provenance schemas, and audit schemas satisfy their contracts;
3. the bundled default temporal policy compiles successfully and remains bounded;
4. source and packaged temporal policy files are byte-identical;
5. risk rules, command templates, packs, typed effects, graph relationships, and public exports remain valid;
6. source data, schemas, and packaged resources remain consistent in a source checkout.

The runtime validator is dependency-free and implements the bounded JSON Schema keywords used by the checked-in contracts. External tooling may validate the same standard Draft 2020-12 schemas with a full JSON Schema implementation.

Any validation error makes `doctor` return a non-zero exit status.
