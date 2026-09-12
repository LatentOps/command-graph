# Reproducible integration evaluation

Issue #91 is evaluated in two layers so Ordin core behavior is not mixed with
runtime/process overhead:

1. `ordin.integration_evaluation.v1` measures the core and in-process adapter
   boundaries across the reviewed safety fixtures, real-agent trajectory corpus,
   policy checks, regression replay, conformance, provenance, and observations.
2. `ordin.runtime_integration_evaluation.v1` executes the coding-agent hook and
   MCP proxy as real local subprocess boundaries. The MCP proxy launches and
   relays to a deterministic local upstream server over stdio.

Together these reports cover the coding-agent integration from #84, the MCP
proxy from #85, the trajectory corpus from #87, regression promotion from #88,
conformance from #89, and diagnostics from #90. They are engineering evidence,
not a claim of universal agent safety.

## Workload selection and labels

Labels are defined before execution in `ordin/integration_evaluation.py`,
`ordin/runtime_evaluation.py`, `benchmarks/safety.jsonl`,
`benchmarks/agent_trajectories.jsonl`, and
`benchmarks/runtime_mcp_semantics.json`.

The core/adapter study contains seven labeled integration workloads, eleven
multi-step trajectories, and thirty-four reviewed safety cases. It includes
benign reads, write escalation, destructive shell actions, unknown tool
identities, server identity mismatch, temporal behavior, policy checks,
provenance and observation linkage, and all nine maintained infrastructure
domains.

The executable-boundary study adds six protocol cases:

- coding-agent read -> allow
- coding-agent write -> ask
- coding-agent root deletion -> deny
- MCP read -> forwarded upstream result
- unknown MCP tool -> approval required
- MCP shell root deletion -> blocked before upstream

The MCP upstream is `scripts/runtime_fixture_mcp_server.py`, a deterministic
local JSON-RPC process. It is deliberately small and versioned so CI does not
rely on an external service, network availability, credentials, or customer
data.

## Metrics and interpretation

The core report emits counts and rates for reviewed actions and trajectories,
`allow` / `warn` / `ask` / `block` distribution, false allows, critical misses,
false blocks, unnecessary escalations, trajectory detection, action-kind and
infrastructure coverage, identity/conformance controls, policy accuracy,
provenance/observation linkage, and p50/p95/p99 core and adapter latency.

The runtime report separately emits process-level p50/p95/p99 latency for the
coding-agent hook and MCP proxy paths. For the hook, timing includes Python
process startup, stdin parsing, Ordin review, and stdout serialization. For the
MCP proxy, timing includes proxy startup, stdio relay, the deterministic local
upstream process, Ordin review, and response serialization. It does not include
model inference, network transport, or human approval latency.

Counts are always reported alongside rates. Representative critical catches,
false-block examples, ambiguous/escalated cases, setup findings, and friction
categories are included in the machine-readable and Markdown runtime reports.
Raw action parameters and sensitive values are not copied into published
examples.

Any unexpected correctness failure must be reproduced and promoted through the
[failure-regression workflow](failure-regressions.md) before #91 is considered
complete. The permanent CI job runs that replay before both evaluation layers.

## Reproduce

Install the checkout with development dependencies and run from the repository
root:

```bash
python -m pip install -e ".[dev]"

python scripts/run_integration_evaluation.py \
  --revision "$(git rev-parse HEAD)" \
  --repetitions 20 \
  --json-out integration-evaluation-report.json \
  --markdown-out integration-evaluation-report.md

python scripts/run_runtime_evaluation.py \
  --revision "$(git rev-parse HEAD)" \
  --repetitions 5 \
  --json-out runtime-integration-evaluation-report.json \
  --markdown-out runtime-integration-evaluation-report.md
```

CI uploads all four files with the safety benchmark, trajectory report, and
regression replay under the `ordin-safety-benchmark` artifact.

The checked-in [core sample report](reports/integration-evaluation.md) and
[JSON results](reports/integration-evaluation.json) remain a recorded CI
snapshot for the core/adapter layer. Runtime report snapshots are published only
from a successful CI execution of the executable-boundary study so measured
latency is tied to a concrete runner and revision rather than fabricated.

## Setup and developer-experience findings

The executable study treats setup itself as part of the evaluation:

- the coding-agent hook must accept one JSON object on stdin and return the
  expected permission mapping without a hidden fallback;
- the MCP proxy must load exact semantics, start the declared upstream command,
  relay an allowed call, and fail closed on unknown or destructive calls;
- allowed read cases must emit redacted post-action observations through the
  process boundary;
- process exits, protocol mismatches, and missing observation linkage become
  explicit friction categories and fail the evaluation.

No workaround is applied inside the study when one of these checks fails.

## Representative findings

The reviewed process cases include two explicit critical catches: root deletion
is denied at the coding-agent hook and blocked before the MCP upstream. The
unknown MCP identity is an intentionally ambiguous control and must require
caller-owned approval. The benign read controls must not false-block. If the
published runtime report records no false blocks, it says so explicitly instead
of omitting that category.

## Limitations

The coding-agent runtime study exercises the actual Ordin hook executable with
Claude Code protocol-shaped events, but it does not launch a hosted Claude model
session. The MCP study exercises the actual Ordin proxy executable and a real
local upstream subprocess, but not a third-party MCP implementation or network
transport. These exclusions keep the benchmark reproducible and isolate Ordin's
integration overhead from model and network latency.

Latency is environment-specific. Fixture labels and reconstructed trajectories
do not estimate production prevalence of unsafe actions. Zero failures on these
finite workloads do not establish universal real-world safety accuracy.
