# Reporting integration failures safely

Use a [structured issue form](https://github.com/LatentOps/ordin/issues/new/choose)
for public safety/decision bugs, integration/setup failures, or sanitized captured
failures. Reports may be false allows, false blocks, unnecessary escalation,
identity or contract drift mismatches, history/reset or observation errors,
parser/transport failures, policy confusion, setup/documentation friction,
latency regressions, or platform differences.

**Vulnerabilities and sensitive bypass details belong in
[private vulnerability reporting](https://github.com/LatentOps/ordin/security/advisories/new),
not public issues.** See [SECURITY.md](../SECURITY.md).

Never paste credentials, API keys, tokens, cookies, private repository content,
full prompts or conversations, proprietary output, private URLs/hostnames, or
raw trace databases. Even metadata databases contain correlatable hashes and
should remain private. Sanitization is a guard, not permission to publish
information you do not own.

## Minimum useful report

Supply Ordin's version/commit, integration and host version, OS/Python versions,
expected versus actual decision or behavior, whether history matters, and a
minimal synthetic/offline reproduction. Give version numbers rather than
environment dumps. Unknown information is acceptable. Report setup time only
if known; leave it blank for an unfinished setup. No report is uploaded by Ordin.

## Attachment contract

The preferred attachment is one manually reviewed `ordin.trace_candidate.v1`
JSON file produced by the existing [trace workflow](trace-capture.md):

```sh
ordin trace inspect /private/capture.ordin-trace.db --json
ordin trace sanitize /private/capture.ordin-trace.db \
  --start 1 --end 3 --expected block \
  --category trajectory_secret_exfiltration --output candidate.json --json
ordin trace replay candidate.json --json
ordin trace replay candidate.json --integration --json
```

Choose the segment, expected decision, and category from your actual invariant;
the numbers above are an example. Inspect locally without pasting inspection
output. Keep one session and configuration, no lifecycle crossing, and at most
32 proposals. The candidate replaces resource/tool identities with aliases,
removes raw arguments, and retains abstract effects, observation relationships,
captured decisions, and configuration/version digests. Review **every field**
before attaching it. Do not attach the database or its WAL/SHM sidecars.

State whether the source was a real observed failure, a public maintainer
reconstruction, or an intentionally synthetic demonstration. Tool-generated
provenance stays `synthetic_from_failure` with
`derivation: semantic_reconstruction`; that describes reconstruction mechanics,
not proof of production origin. Never relabel it as a raw production trace.
Explain the expected semantic invariant and include success/failure summaries
from both replays. A replay mismatch is useful evidence: preserve the expected
label and explain the mismatch instead of changing it to make the test green.

Metadata reconstruction cannot prove an HTTP framing, shell parser, host
approval, or environment bug. Add a minimized offline protocol fixture for
those boundaries. No tokens, private URLs, or actual destructive execution are
needed. If privacy cannot be established, use the private reporting route.

## Maintainer triage

1. Reproduce against the current stable release and main when relevant. Record
   which versions reproduced it and any required fixture/host differences.
2. Classify the failure and setup stage using the vocabulary below. Decide
   whether it is core behavior, an integration defect, docs/setup friction, or
   expected conservative behavior such as an unknown tool requiring approval.
3. Minimize and sanitize the fixture; preserve honest source provenance and
   the original expectation. Explain limitations if it cannot be reproduced.
4. Fix confirmed defects in a focused PR. Test benign controls as well as the
   reported unsafe path. Do not weaken a safety threshold to match a report.
5. Once the invariant passes, explicitly promote a reviewed candidate to
   `failure` or `conformance` using `ordin trace promote`. Parser/transport
   bugs also need their actual boundary regression. Review permanent tests
   and all CI before merging.
6. Record the outcome: fixed, expected behavior, documentation only, or not
   reproducible with a concrete reason. A useful expected-behavior control can
   become a permanent regression too. Link the fixing PR/test in the issue.

## Synthetic report to regression example

This exercise simulates a report claiming an upload was unnecessarily denied.
It is **intentionally synthetic**, not customer feedback. Triage finds a
preceding trusted secret-read observation, so the denial is expected behavior.
The regression candidate protects that context-sensitive decision.

```sh
python examples/trace_capture_demo.py /private/new-feedback-demo
ordin trace replay /private/new-feedback-demo/candidate.json --json
ordin trace replay /private/new-feedback-demo/candidate.json --integration --json
# Read the candidate and confirm its privacy/provenance before this local append:
ordin trace promote /private/new-feedback-demo/candidate.json \
  --target conformance --output /private/new-feedback-regressions.jsonl --json
```

Use a new directory and destination. The example performs no tool execution,
model inference, or upload. Its permanent test verifies capture, redaction,
both replays, promotion, and conformance loading. A maintainer may later move
the reviewed fixture into the versioned corpus through a normal PR.

## Local aggregate summary

Manually classify each report once, without names, issue URLs, timestamps,
free text, repository names, or persistent user identifiers. The fixture
[feedback-summary-input.json](../tests/fixtures/feedback-summary-input.json)
is entirely synthetic; it demonstrates the input shape, not adoption counts.

```sh
python scripts/summarize_feedback.py tests/fixtures/feedback-summary-input.json
python scripts/summarize_feedback.py /private/classified-reports.json \
  --output /private/new-aggregate-summary.json
```

The script accepts only bounded classification fields and optional finite
minutes to first working setup. It reports integration/failure/disposition and
setup-stage counts, permanent-regression conversions, and mean setup minutes
with the number of supplied samples. Unknown durations are excluded; an empty
sample has a null mean. Output files must be new. There is no runtime telemetry,
network fetch, upload, or individual tracking. Counts describe submitted
reports, not users, prevalence, or universal product accuracy.

| Field | Values |
| --- | --- |
| `integration` | `claude-code`, `codex`, `cursor`, `mcp-proxy`, `mcp-http`, `shell`, `python`, `unknown` |
| `failure` | `false_allow`, `false_block`, `unnecessary_escalation`, `identity_mismatch`, `contract_drift`, `session_history`, `observation_linkage`, `parser_transport`, `policy_semantics`, `setup_failure`, `documentation`, `performance`, `platform` |
| `disposition` | `open`, `fixed`, `expected_behavior`, `documentation_only`, `not_reproducible` |
| `setup_stage` | `installation`, `discovery`, `configuration`, `host_enablement`, `semantics_review`, `smoke`, `none` |
| `regression` | Boolean; a permanent regression was added |
| `minutes_to_first_working` | Optional number or null; omit until setup works |
