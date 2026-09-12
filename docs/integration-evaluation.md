# Reproducible integration evaluation

This evaluation measures Ordin's local Claude Code hook adapter, MCP review
boundary, reviewed trajectory corpus, and safety fixtures. It does not launch a
live Claude Code session or measure a complete client/server execution loop.
It supplies reproducible local evidence for issue #91; the live-runtime study
and end-to-end timing requirements remain separate work.

## Methodology

Workloads and labels are defined before execution in
`ordin/integration_evaluation.py`, `benchmarks/safety.jsonl`, and
`benchmarks/agent_trajectories.jsonl`. The seven integration cases cover a benign
read, a write escalation, critical shell deletion, an unknown MCP tool, and an
MCP server identity mismatch. Cases use synthetic paths and local fixtures.
No customer transcripts, model calls, credentials, or network execution are
required.

The runner also executes adapter conformance, the existing failure-regression
corpus, the declarative-policy accuracy gate, and integration diagnostics. It
fails on decision mismatches, missing evidence links, or any component gate
failure. A provenance link is counted only when its action ID matches the
reviewed action. Observations are synthetic successful tool responses for the
allowed read cases, rather than proof of real upstream execution.

The action count represents distinct labeled cases and trajectory steps, not
the repeated calls used for timing. For each integration workload, one warm-up
is excluded, then the configured number of core and adapter measurements is
recorded. Percentiles use the nearest-rank method. MCP adapter timing includes
local observation handling for allowed calls. It excludes stdio transport,
upstream execution, model inference, and human approval. The safety-fixture
latency distribution is reported separately.

False allows, critical misses, false blocks, unnecessary escalations, decision
distributions, domain coverage, and evidence-linkage counts accompany the
rates. Known critical deletion cases and the benign read cases provide positive
and negative controls. Any unexpected behavior must be reproduced and promoted
through the [failure-regression workflow](failure-regressions.md).

## Reproduce

Install the checkout with its development dependencies, then run from its root:

```bash
python -m pip install -e ".[dev]"
python -m scripts.run_integration_evaluation \
  --revision "$(git rev-parse HEAD)" \
  --repetitions 20 \
  --json-out integration-evaluation-report.json \
  --markdown-out integration-evaluation-report.md
```

CI produces both report formats in the `ordin-safety-benchmark` artifact. Each
report records its evaluated revision, Python/platform/CPU environment, Ordin
version, policy, and timing sample counts. Re-running on another machine may
change latency.

## Interpretation and remaining scope

Zero failures on these finite fixtures does not estimate production safety or
the prevalence of harmful agent actions. Setup diagnostics and conformance
failures are recorded as friction categories; an empty category list is not a
usability study. The actual Claude Code runtime, an external MCP SDK/server,
network latency, and model versions are outside this local evaluation.

A complete real-world study for #91 still needs representative live-runtime
workloads, measured end-to-end overhead, reviewed setup findings, and documented
runtime/model versions. This report must not be presented as that completed
study or as universal agent safety accuracy.
