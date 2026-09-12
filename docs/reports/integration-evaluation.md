# Ordin real-agent integration evaluation

Revision: `e121bbce7545e5716263d3b6146506528dd87103`
Ordin: `0.2.0.dev0`; policy: `fail_on=warn`
Environment: 3.13.15 / Linux-6.17.0-1022-azure-x86_64-with-glibc2.39 / 4 logical CPUs

This report is generated from versioned local safety fixtures, maintained first-party integration paths, and the reviewed real-agent trajectory corpus. It is an engineering evaluation, not a claim of universal agent safety.

## Scope

- Integration workloads: 7
- Agent trajectories: 11
- Trajectory steps: 21
- Safety cases: 34
- Total reviewed actions represented: 62

## Safety and decision quality

- Integration false allows: 0
- Integration false blocks: 0
- Integration unnecessary escalations: 0
- Integration critical misses: 0
- Safety-fixture false allows: 0
- Safety-fixture critical misses: 0
- Safety-fixture false blocks: 0
- Trajectory contextual detection rate: 1.0000
- Policy accuracy gate: PASS

## Integration integrity

- Conformance checks: 17
- Conformance failures: 0
- Identity controls detected: 2 / 2
- Provenance linkage: 1.0000
- Post-action observation linkage: 1.0000 (2 applicable cases)

## Latency on this environment

- Core review p50 / p95 / p99: 0.0415 / 13.0422 / 13.1663 ms
- Integration boundary p50 / p95 / p99: 0.0791 / 13.1611 / 13.2909 ms

Integration-boundary timing covers local adapter/proxy review handling only. It excludes model inference, network transport, upstream MCP execution, and human approval latency.

## Friction and failures

- Friction categories: none observed
- Evaluation errors: 0

## Limitations

- This is a finite, reviewed, local-first engineering evaluation; it is not universal real-world safety accuracy.
- Integration-boundary timing excludes model inference, network transport, upstream MCP execution, and human approval latency.
- Fixture labels and reconstructed trajectories do not estimate production prevalence of unsafe actions.
- Latency values are environment-specific and are not cross-machine performance claims.
