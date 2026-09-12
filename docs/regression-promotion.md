# Failure replay and regression promotion

Real integration failures should become permanent deterministic regressions instead of one-off patches.

The extended promotion API is `ordin.regression_promotion`. It complements the
existing `ordin.regression_replay` API and `scripts/run_regression_replay.py`;
their `ordin.regression_case.v1` fixtures remain supported unchanged.

Policy, semantics, observation, and extra expected-code overrides apply to
single-action safety replays. Trajectory replays reject those overrides, which
the trajectory runner cannot apply, and use the expectations in their steps.

The promotion loop is:

```text
integration failure
-> minimize and sanitize
-> reproduce with an existing Ordin fixture contract
-> add ordin.regression_replay.v1 metadata
-> prove the replay fails against the broken behavior
-> fix the implementation
-> replay passes
-> permanent CI coverage
```

## Replay contract

Promoted cases live in [`benchmarks/regressions.jsonl`](../benchmarks/regressions.jsonl).

`ordin.regression_replay.v1` wraps one of the existing contracts:

- `ordin.safety_fixture.v1` for a focused single action; or
- `ordin.agent_trajectory.v1` for a multi-step integration failure.

It does not introduce another action or trajectory representation.

Each promoted regression records:

- stable `id`;
- `failure_class`;
- severity;
- the invariant that was violated;
- why the failure matters;
- optional sanitized origin/reference;
- exact safety or trajectory fixture;
- optional `ordin.tool_semantics.v1` identity semantics;
- optional `ordin.policy_set.v1` declarative policy;
- optional prior `ordin.action_observation.v1` facts for focused single-action replay;
- optional expected temporal and provenance codes;
- optional per-case latency ceiling.

Supported failure classes cover false allows/critical misses, false blocks/unnecessary escalation, semantic effect/resource defects, parser/normalizer mismatches, identity failures, policy/temporal-policy defects, provenance and observation failures, integration translation defects, and material performance regressions.

## Replay one failure

Run exactly one promoted regression:

```bash
python scripts/replay_regression.py --id critical-root-delete
```

This is the preferred debugging command while fixing a newly captured failure. It does not require running the entire safety benchmark.

## Replay all promoted failures

```bash
python scripts/replay_regression.py
```

Write a machine-readable report:

```bash
python scripts/replay_regression.py \
  --json-out regression-replay-report.json
```

The permanent Safety benchmark CI job runs the complete promoted set after the main fixture benchmark and real-agent trajectory corpus.

## Promotion procedure

When an integration bug is found:

1. **Classify the failure.** Name the observed contract break, not only the symptom.
2. **Minimize it.** Keep only the action, context, policy, identity, observations, or trajectory steps necessary to reproduce the invariant.
3. **Sanitize it.** Never commit raw customer traces, credentials, private URLs, personal identifiers, proprietary source/output, or irrelevant local paths.
4. **Choose the existing fixture contract.** Use a safety fixture for one review; use an agent trajectory when ordering/history/observations matter.
5. **Add promotion metadata.** Explain the invariant and impact in the regression wrapper.
6. **Prove the negative control.** Before or alongside the repair, demonstrate that the replay gate fails when the known bad behavior is present. Tests may inject a deliberately wrong expected result or mutation rather than preserving vulnerable product code.
7. **Fix the root cause.** Do not weaken the expected invariant to make the replay pass.
8. **Run the individual replay.** Verify the promoted case passes.
9. **Run the full safety gate and permanent CI matrix.** The regression becomes part of the ordinary merge gate.

## Stable assertions

Assert the contract that must survive implementation changes:

- decision strength;
- required semantic effects;
- required resource identity/prefix;
- required temporal category;
- required provenance code;
- exact trusted runtime/server/tool identity behavior;
- policy result;
- observation-driven change in later review;
- bounded latency where performance is the defect.

Do not freeze incidental reason wording or complete serialized reviews unless exact serialization is itself the public contract.

## Sanitization guard

The regression loader performs a small fail-closed credential check in addition to review discipline. It rejects common live credential prefixes and non-redacted values under sensitive field names such as password, API key, access token, refresh token, secret key, and private key.

Explicit placeholders such as `<redacted>` are allowed. This guard is intentionally not a general secret scanner and does not make raw private traces safe to commit.

## Critical failures

A promoted critical case that is expected to be non-`allow` but evaluates to `allow` is counted explicitly as a critical false allow. Any expected-decision mismatch already fails the replay, so weaker regressions such as `block -> warn` also fail rather than disappearing behind the false-allow count.

The replay gate supplements, not replaces, the broader #67 safety benchmark thresholds and #87 trajectory coverage.

## Current seeded promotions

The initial set includes:

- critical root filesystem deletion;
- trusted runtime identity mutation;
- a declarative upload-block policy;
- post-action observation driven secret exfiltration.

These cases demonstrate the workflow and remain permanent regressions. New cases should be added only for independently useful failure invariants, not as a duplicate log of every bug report.
