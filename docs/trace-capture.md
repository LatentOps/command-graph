# Local trace capture and regression promotion

The [offline quickstart](quickstart.md) verifies the synthetic demo through
capture, replay and explicit promotion from an installed wheel.

Capture is disabled by default. Enable it to retain action-level evidence from
Claude Code, Codex, MCP stdio, or MCP HTTP, then turn a reviewed segment into an
offline regression. Capture and promotion never upload data or run a tool.

## Enable capture

Create a private directory outside your checkout, owned by your user and with
mode `0700` on Linux or macOS. Use separate files for session state, audit logs,
and trace capture. On Windows, restrict the directory's ACL yourself; Ordin's
owner/mode checks apply to POSIX systems.

| Integration | Metadata capture | Additional raw local opt-in |
| --- | --- | --- |
| Claude Code | `ORDIN_CLAUDE_TRACE=/private/claude.ordin-trace.db` | `ORDIN_CLAUDE_TRACE_RAW=1` |
| Codex | `ORDIN_CODEX_TRACE=/private/codex.ordin-trace.db` | `ORDIN_CODEX_TRACE_RAW=1` |
| MCP stdio | `ordin-mcp-proxy --trace /private/mcp.ordin-trace.db … -- server-command` | `--trace-raw-local` |
| MCP HTTP | `ordin-mcp-http --trace /private/http.ordin-trace.db …` | `--trace-raw-local` |

Environment raw flags accept only `0` or `1`. Raw mode requires an explicit
capture path. The Python integration builders accept `trace_path` and
`raw_local`; a custom `Ordin` instance can use `attach_trace` and pass the
returned recorder to its integration. Capture does not enable persistent hook
history: configure `ORDIN_CLAUDE_STATE` or `ORDIN_CODEX_STATE` and lifecycle hooks
separately when testing temporal behavior across hook processes.

Metadata records include the integration and adapter contract version, Ordin
version, an installed VCS revision when available, evaluator configuration
digest, action kind and operation, decision, risk, proposed disposition,
catalog effects, resource digests, hashed identities/categories/provenance
codes, and redacted observations. SQLite sequence numbers give capture-relative
ordering; session hashes keep concurrently captured sessions separate. Unknown
resource labels and effects are filtered and mark the event as incomplete.
Contract pins and configured shell aliases are included in MCP configuration
digests. A missing VCS revision is `null`; the recorder does not guess from the
user's working repository.

The proposed disposition describes the review result, not proof of execution
or host approval. Only a correlated post-action observation supplies execution
evidence. Capture writes and existing audit/session writes are separate
transactions: a later audit or state failure can leave a reviewed proposal
without an execution observation. Capture failures prevent the associated
integration review from granting execution. Incomplete post capture reports an
error; it cannot undo an already executed tool.

Metadata mode retains no original arguments, paths, hostnames, request IDs,
session IDs, output, or transcripts. Hashes preserve equality and can be guessed
for predictable values, so metadata captures should also remain private.
Raw mode adds **unredacted normalized actions and is unsafe to share**. It does
not collect full transcripts or extra tool output; information already removed
by an adapter, such as Codex patch bodies, stays removed. Raw mode cannot be
changed in place. Start a new file when changing modes.

Capture files use SQLite transactions, a 64 MiB bound, a 4,096-event limit, and a
1 MiB limit per event. They reject duplicate action/observation identities,
unmatched observations, and observations of denied actions. On POSIX, files must
be regular, singly linked, owner-only files in a private directory; symbolic
links and publicly writable directories are refused. A full file reports an
error instead of silently losing evidence. Rotate it explicitly. These controls
do not defend against an attacker already running as the owning user.

## Inspect, reduce, and review

```bash
ordin trace inspect /private/mcp.ordin-trace.db --json
ordin trace sanitize /private/mcp.ordin-trace.db \
  --start 2 --end 5 --expected block \
  --category trajectory_secret_exfiltration \
  --output candidate.json --json
ordin trace replay candidate.json --json
ordin trace replay candidate.json --integration --json
```

Inspect lists session keys, ordered review/observation events, lifecycle
boundaries, and non-allow decisions as possible segment endpoints. Select one
session with `--session-key` when a file contains several. Start/end are
inclusive sequence numbers. Sanitization requires an explicit expected final
decision; an optional category specifies the invariant more precisely. It
prints the complete candidate and only writes a candidate file when `--output`
is supplied. Existing candidate files are refused.

The sanitizer replaces identities and resources with deterministic aliases:
equal values remain equal and distinct tools remain distinct. It removes raw
action data even from raw-mode captures. It reconstructs abstract effects,
observations, and bounded temporal signals using the existing `ActionEnvelope`,
tool semantics, observation, and trajectory contracts. Review the entire
candidate before promotion. The machine-readable provenance is always
`synthetic_from_failure`, with `derivation: semantic_reconstruction`.

This workflow reproduces semantic and temporal properties. It cannot reproduce
every parser bug, HTTP framing problem, exact permission policy, or hidden host
state from metadata alone. Integration replay uses maintained hook adapters or
the shared MCP action gate; it does not simulate an HTTP wire exchange. Use the
existing transport regression fixtures for framing bugs. Select at most 32
reviews with one integration configuration and no lifecycle boundary. A delayed
observation interleaved with another review, truncated evidence, or changing
tool semantics requires a narrower segment or manual reconstruction. The
sanitizer never inserts private commands to force a match.

## Promote an offline fixture

```bash
ordin trace promote candidate.json --target conformance \
  --output benchmarks/captured_conformance.jsonl --json
```

Promotion is an explicit local append. It scans with the existing credential
and sensitive-field rules, validates provenance, and replays the requested
invariant before writing. Conformance promotion also requires integration
replay. A mismatch or unsafe candidate leaves the destination unchanged. No
command stages, commits, or pushes a fixture.

| Target | Existing contract | Runner |
| --- | --- | --- |
| `trajectory` | `ordin.agent_trajectory.v1` | `scripts/run_trajectory_corpus.py` |
| `failure` | `ordin.regression_case.v1` | `scripts/replay_regression.py` |
| `extended` | `ordin.regression_replay.v1` | Existing regression promotion loader/runner |
| `conformance` | Regression case plus capture provenance | `scripts/run_integration_conformance.py --captured-fixtures PATH` |

Promoted records retain capture/version/configuration digests and the original
captured decisions. They do not claim to be verbatim production transcripts.
The destination must already contain valid records for the selected target;
duplicate IDs are refused. A short-lived `.ordin-lock` serializes cooperating
promoters, and the writer checks for intervening edits before replacing the
file. Do not edit a corpus concurrently with promotion. A leftover lock is not
automatically stolen: inspect it and confirm no promoter is running before
removing it. Credential scanning is a useful guard, not proof that arbitrary
manually edited text is public.

## Reproducible example

```bash
python examples/trace_capture_demo.py /private/new-ordin-demo
ordin trace replay /private/new-ordin-demo/candidate.json --integration --json
# Review candidate.json, then choose an explicit promotion destination.
```

The example simulates a permitted read, a trusted observation of `secret.read`,
and a denied upload proposal. It runs no shell command and contacts no server.
Its sanitized result is checked into `benchmarks/captured_conformance.jsonl`
and replayed by permanent tests and the safety CI job.

For direct Python contracts, `ordin trace record events.json --capture
/private/direct.ordin-trace.db --session SESSION --semantics semantics.json`
reviews an input object containing an `events` array of 1–64 existing action and
observation contracts. It supplies in-memory temporal state and executes
nothing. Direct captures use integration `python` and can enter the three core
corpora; adapter conformance requires a maintained integration identity.

Keep databases outside version control. The repository ignores
`*.ordin-trace.db*` and `.ordin-traces/` as an additional guard. Only explicitly
reviewed, sanitized fixtures belong in a PR.
