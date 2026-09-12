# Failure replay and regression promotion

Real integration failures should become permanent Ordin regressions instead of one-off fixes.

The promotion loop is:

```text
integration failure
-> sanitize the minimum deterministic input
-> reproduce it as ordin.regression_case.v1
-> verify the fixture fails before the repair
-> fix the owning behavior
-> keep the fixture permanently green
```

The committed corpus lives in [`benchmarks/failure_regressions.jsonl`](../benchmarks/failure_regressions.jsonl). Each record wraps one existing `ordin.agent_trajectory.v1` fixture, so a single action is simply a one-step trajectory and a multi-step failure can retain history and post-action observations without creating another action model.

## Required metadata

Every promoted case records:

- a stable `id`;
- `failure_class`;
- the stable safety or integration `invariant` that was violated;
- a short `source` describing where the failure was discovered;
- the deterministic action or trajectory needed to reproduce it.

Supported failure classes are:

- `false_allow`;
- `false_block`;
- `semantic_effect`;
- `parser_normalizer`;
- `identity_handling`;
- `policy_temporal`;
- `provenance_audit`;
- `post_action_observation`;
- `integration_translation`;
- `performance_regression`.

Prefer assertions at the stable contract level: decision, required effects/resources, trajectory categories, identity binding, or provenance/observation linkage. Do not freeze incidental prose or complete serialized reviews unless those bytes are themselves the contract.

## Replay one case

```bash
python scripts/run_regression_replay.py \
  --case mutated-tool-runtime-fails-closed
```

This is the fastest path when repairing one reported failure.

## Replay the full promoted set

```bash
python scripts/run_regression_replay.py
```

Write a machine-readable report with:

```bash
python scripts/run_regression_replay.py \
  --json-out failure-regression-report.json
```

The report includes case coverage, pass/fail state, and an explicit `critical_misses` count. Any promoted mismatch fails the runner; an expected `block` that becomes anything weaker is additionally counted as a critical miss.

The ordinary pytest matrix also replays the committed corpus, so promoted regressions are part of permanent CI rather than an optional local benchmark.

## Sanitization

Never copy raw customer or private traces into the repository.

Before promotion:

1. keep only inputs required to reproduce the invariant;
2. replace private paths, hosts, URLs, repository identifiers, and user data with deterministic placeholders;
3. remove credentials, cookies, authorization values, private keys, access/refresh tokens, and API keys;
4. remove unrelated command output and model text;
5. preserve only the semantics that caused the failure;
6. classify reconstructed or synthetic-from-failure cases honestly in the nested trajectory provenance.

The regression loader rejects common credential-shaped field names and several common live-secret patterns as a fail-closed backstop. That check is intentionally not a complete secret scanner; maintainers still own fixture review.

## Promotion checklist

For a newly reported integration failure:

1. reproduce the behavior on the exact current Ordin revision;
2. identify the stable violated invariant and failure class;
3. add the smallest sanitized regression fixture;
4. run the single-case command and confirm it reproduces the failure before the repair;
5. implement the repair without weakening unrelated safety behavior;
6. replay the single case, the full promoted regression corpus, the real-agent trajectory corpus, and the safety benchmark;
7. run the complete repository CI matrix;
8. merge only after every required check is green.

For multi-step failures, preserve the minimum required prior actions and observations in the nested trajectory. For integration identity bugs, keep exact runtime/server/tool identities necessary to prove the mismatch and replace unrelated identifiers with placeholders.

This workflow complements the broader safety benchmark and real-agent trajectory corpus. It does not turn a finite set of past bugs into a claim of universal real-world safety accuracy.
