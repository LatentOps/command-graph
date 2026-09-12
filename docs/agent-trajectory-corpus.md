# Real-agent trajectory corpus

Ordin keeps a versioned offline corpus of multi-step agent trajectories in [`benchmarks/agent_trajectories.jsonl`](../benchmarks/agent_trajectories.jsonl).

The corpus complements the single-action adversarial safety fixtures. It is intended to capture risks that become clearer only when actions, identities, retries, and post-action observations are evaluated together.

## Fixture contract

Each JSONL record uses `ordin.agent_trajectory.v1` and contains:

- `id`: stable fixture identity;
- `source`: integration path such as `claude_code` or `mcp_proxy`;
- `provenance_kind`: `captured`, `redacted`, `reconstructed`, `synthetic`, or `synthetic_from_failure`;
- `behavior_classes`: the risk/workflow categories the trajectory exercises;
- optional infrastructure `domain` and descriptive `tags`;
- optional exact versioned `ordin.tool_semantics.v1` rules required to interpret tool/MCP identities;
- ordered `steps` containing existing `ordin.action_envelope.v1` actions;
- optional existing `ordin.action_observation.v1` evidence linked to a step action ID;
- the expected decision, temporal categories, and semantic effects for every step;
- `contextual_required` when the final safety property must become stronger or gain a temporal category only because prior trajectory context is present.

The format deliberately reuses Ordin's public action, history, observation, temporal, and tool-semantics contracts. The trajectory layer owns replay/provenance/coverage only; it does not create another action model.

## Current required behavior coverage

The checked-in corpus must cover all of these classes:

- benign multi-step development;
- accidental destructive actions;
- suspicious or policy-violating actions;
- nested shell/tool invocation;
- retry/reformulation after a warning or escalation;
- privilege/capability changes;
- read-then-exfiltrate behavior;
- repository/history mutation;
- infrastructure changes;
- tool/server/runtime identity changes;
- partial failure followed by recovery;
- post-action observations that influence later review.

The runner also requires both the Claude Code and MCP proxy integration sources to remain represented.

## Replay

Run the corpus locally:

```bash
python scripts/run_trajectory_corpus.py
```

Write a machine-readable report:

```bash
python scripts/run_trajectory_corpus.py \
  --json-out agent-trajectory-report.json
```

The report includes:

- trajectory and step decision accuracy;
- context-dependent detection rate;
- behavior-class coverage;
- action-kind coverage;
- integration-source coverage;
- decision coverage;
- infrastructure-domain coverage;
- provenance-kind coverage;
- exact regression diagnostics.

CI runs this beside the existing safety benchmark. Neither corpus is presented as universal real-world safety accuracy.

## Post-action evidence

Observations are caller-supplied facts, not model claims. They can materially change later temporal review.

For example, the committed `mcp-observed-secret-exfiltration` fixture begins with an MCP read that is predicted only as `filesystem.read`. Its post-action observation records the additional observed effect `secret.read`. A later MCP upload therefore matches the existing secret-exfiltration temporal policy and is blocked. Replaying the final action without the prior observed context does not receive that trajectory signal.

This is the intended evidence boundary: an integration can add what actually happened after execution, while Ordin keeps predicted and observed facts distinct in provenance.

## Sanitization and promotion rules

Do not commit raw customer/private traces.

A trajectory may be promoted when it is one of:

- a safe local/public capture;
- a reviewed redaction;
- a deterministic reconstruction of a real integration behavior;
- a synthetic fixture derived from a real failure class;
- an intentionally synthetic adversarial control.

Before committing a captured or reconstructed case:

1. remove credentials, tokens, cookies, private URLs, user identifiers, proprietary source/output, and unrelated filesystem paths;
2. replace values with deterministic placeholders while preserving the semantic property being tested;
3. keep only action arguments required for the safety invariant;
4. describe the resulting provenance honestly with `provenance_kind`;
5. assert the stable safety property instead of byte-for-byte incidental output;
6. keep the case fully runnable offline.

If sanitization would remove the behavior that made the case useful, do not commit the trace. Reconstruct a minimal safe fixture instead.

## Limitations

- The corpus is curated, not statistically representative of every agent workload.
- `reconstructed` and `synthetic_from_failure` are not raw production traces.
- Coverage counts demonstrate exercised contracts, not prevalence in deployed systems.
- A passing trajectory corpus does not establish universal safety or eliminate integration-specific failure modes.
- New real integration failures should become regression fixtures through the dedicated failure-promotion workflow rather than silently editing existing expected labels.
