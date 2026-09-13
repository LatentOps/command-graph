# Agent Safety Corpus v1

Corpus **1.0.0** requires Ordin **0.4 development**. Its nine offline cases include
five reconstructions of public maintainer defects and four intentionally synthetic
controls. There are no customer sessions or raw production traces. The
[snapshot](../benchmarks/agent-safety-corpus/v1/cases.json) and labels are reviewed source files.

## Reproduce

From an installed development checkout, record the exact revision and choose a
new report directory:

```sh
python scripts/run_agent_safety_corpus.py \
  --revision "$(git rev-parse HEAD)" --output /tmp/new-ordin-corpus-report
```

Output includes JSON/Markdown results, the supplied Git revision, dataset SHA-256,
Ordin/Python/platform versions, and timings. Use a clean checkout of that revision;
the argument is not an attestation. CI supplies its checkout SHA. No credentials,
model, remote service, or proprietary agent binary is required. HTTP controls use
ephemeral loopback ports and a fixture that never executes tool requests.

The [recorded report](reports/agent-safety-corpus-v1/report.md) identifies its source
revision and environment. Required Safety benchmark CI runs the entire snapshot
and uploads fresh reports. The Python/macOS suites also enforce it. No critical
subset or existing safety threshold is reduced.

## Construction and evidence

Every item embeds an existing sanitized `ordin.trace_candidate.v1`, containing
the existing failure, trajectory, action, observation, and semantics contracts.
All candidates pass sensitive-field/credential checks, core/integration replay,
and explicit conformance promotion.

Semantic reconstruction cannot encode malformed responses, buffered deadline
races, a 32-action pending queue, or caller-owned context policy. Such cases pair
a semantic anchor with a mandatory named control exercising the actual boundary.
The manifest cannot select arbitrary Python or shell code.

| Case | Evidence and invariant | Boundary |
| --- | --- | --- |
| `rejected_response` | [PR #142](https://github.com/LatentOps/ordin/pull/142): rejected results retain their reservation and accept valid retries | MCP correlation |
| `history_pressure` | [PR #143](https://github.com/LatentOps/ordin/pull/143): pending actions survive history pressure and settle correctly | Bounded MCP history |
| `relative_context` | [PR #144](https://github.com/LatentOps/ordin/pull/144): relative repository context stays unknown to policy | Shared policy through Cursor, with absolute-path benign control |
| `buffered_deadline` | [PR #125](https://github.com/LatentOps/ordin/pull/125): buffered bytes cannot escape after the deadline | HTTP stream with deterministic clock |
| `failed_http_session` | [PR #125](https://github.com/LatentOps/ordin/pull/125): timed-out POST cannot leave a reusable session | Actual loopback timeout and refused recovery |
| `secret_child` | Synthetic: observed secret read changes upload review; explicit children stay isolated | Cursor reconstruction and identity control |
| `destructive_retries` | Synthetic: repeated deletion adds its temporal category | Cursor history; core `warn` requires intervention under default policy |
| `contract_identity` | Synthetic: catalog drift and server mutation lose permission | Actual MCP lock/digest and identity controls |
| `benign_parity` | Synthetic: reads remain allowed; unknown/destructive calls agree across transports | MCP stdio/HTTP parity and upstream call-count checks |

Source fields pin the original maintainer PR merge revisions. Applying the shared
context defect to Cursor is a reconstruction, not a claim the original report came
from Cursor. The HTTP cases share a PR but test different failure mechanisms.
Repeated read anchors support boundary fixtures; they are not independent
production observations or additional failure classes.

`scripts/build_agent_safety_corpus.py` records the recipe, using private temporary
metadata capture and semantic aliases in a new output directory. Capture digests
may differ across environments; regeneration does not authorize replacing the
frozen snapshot. Each candidate retains `synthetic_from_failure` and
`semantic_reconstruction` provenance. Manifest provenance separately identifies
intentionally synthetic source controls.

## Results and limits

The snapshot has **15 semantic actions** across MCP stdio, HTTP, and Cursor:
10 allow, 3 warn, 1 ask, and 1 block. Counts include anchors; internal pressure-loop
and protocol messages belong to the separate boundary controls. Initially all
nine cases pass, both context-dependent cases retain their signal, and tested
decision/control failure counts are zero.

Reports separate core critical misses/false allows, false blocks, and unnecessary
escalations from boundary failures. Applicable counts and detection rates cover
contracts, identity, observation linkage, context policy, session refusal,
deadlines, and parity. Control exceptions fail the gate. Label mismatches remain
failures; the evaluator never rewrites expectations.

Latency distributions measure complete core trajectories, complete integration
reconstructions including validation/setup, and complete boundary controls. They
do not estimate per-tool, model, approval, or production network latency. Small
samples make tail quantiles coarse; deliberate timeout waits must not be compared
directly with core review.

Known gaps include proprietary host enablement, cloud/subagent visibility,
credential-dependent services, real-user false-positive distributions, and
arbitrary parser or filesystem/symlink behavior. The source defects were already
fixed when this snapshot was constructed. This is regression evidence, not an
estimate of production prevalence or universal safety.

## Maintain and promote

Use [feedback intake](feedback-intake.md) to minimize reports and preserve honest
provenance. Add a boundary control where semantic reconstruction cannot reproduce
the defect; include a benign counterpart and review privacy before promotion.
Avoid inflating the dataset with reformulations of one bug.

Preserve `v1/cases.json` after publication. Changes to labels, semantics, or
membership require a new corpus snapshot/version and documented comparison;
retain earlier data/reports. Corpus and package versions are independent.
Construction never stages, commits, uploads, or publishes evidence automatically.
