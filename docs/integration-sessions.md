# Live integration sessions

Ordin's maintained integrations can carry the last 32 reviewed actions and their
linked observations into the existing temporal policy engine. Repeated destructive
actions, repeated privilege escalation, and secret-read/upload sequences therefore
affect the next live review. Ordin still returns a decision; the host owns approval,
execution, credentials, sandboxing, and trustworthy post-action evidence.

## State ownership

`IntegrationSession` owns one exact `SessionIdentity(runtime, session_id, server)`.
It has no global registry and never writes to disk by itself. Every proposal needs
a distinct action ID. Review and append are one serialized operation. All reviewed
proposals, including denied attempts, count toward the 32-action window. A stronger
single-action decision is never weakened by temporal state.

Observations must match a retained, non-denied action, and there can be only one
terminal observation per action. Unknown, duplicate, or expired correlations raise
an error. An evicted action's observation is evicted at the same time. A detached
`snapshot()` uses `ordin.integration_session.v1` and can be validated with the
bundled schema. `restore()` rejects identity, policy, semantics, or Ordin-version
mismatches. The configuration digest includes the default temporal policy.

`reset()` removes all temporal influence. `end()` clears and closes the session.
The host must finish or cancel outstanding execution before resetting and must
not reuse action correlation IDs across a reset. A session identifier is supplied
by the trusted runtime, not by model-authored tool arguments. Subagent metadata is
retained in the action; subagents sharing a runtime session share its safety window.

## MCP stdio

Each `MCPStdioSafetyProxy` owns a fresh in-memory session automatically. Its server
identity is exact and its session ID is unique to that connection. Embedders can
provide `session_id` explicitly. Reusing a server name in a different proxy does
not share state or action IDs. Stopping the proxy discards its state.

The proxy correlates terminal responses and attaches redacted observations before
forwarding responses to the client. It limits in-flight tool requests to 32.
It also refuses a proposal that would evict a still-pending action from history,
including when denied proposals filled that history. Retry after a pending
result is recorded; the bound is not enlarged and evidence is not dropped.
`reset_session()` requires zero in-flight calls and does not reset the action-ID
counter. Malformed state or expired observation linkage terminates forwarding
conservatively. Tool-result text is never parsed into trusted observed effects.

## Claude Code hooks

Separate hook invocations are separate processes. Enable private persistence
explicitly to carry state between them:

```bash
mkdir -m 700 "$HOME/.ordin-state"
export ORDIN_CLAUDE_STATE="$HOME/.ordin-state/claude.sqlite3"
```

Merge the hooks in [`examples/claude-code-session-settings.json`](../examples/claude-code-session-settings.json)
into your Claude settings and restart the agent. Keep the environment variable
consistent across every lifecycle, pre-tool, and post-tool hook. The ordinary
`claude-code-settings.json` example remains usable without session persistence.

The session example uses the documented [Claude lifecycle events](https://code.claude.com/docs/en/hooks):

| Event | Command | Behavior |
| --- | --- | --- |
| `SessionStart` | `ordin-claude-hook session-start` | Create missing state; preserve existing state on resume/compaction |
| `PreToolUse` | `ordin-claude-hook pre` | Review against retained history; commit state before returning permission |
| `PostToolUse` | `ordin-claude-hook post` | Attach the matching redacted completion |
| `PostToolUseFailure` | `ordin-claude-hook post-failure` | Attach the matching redacted failure |
| `SessionEnd` | `ordin-claude-hook session-end` | Delete this session's state |

If the start hook did not run, state is corrupt, storage is full/unavailable, or
configuration changed, pre-tool returns `deny`. It does not silently start over.
An operator can explicitly reset after stopping outstanding tools by supplying a
`SessionStart` payload to `ordin-claude-hook session-reset`. Reset also permits a
reviewed configuration change. Lifecycle commands are no-ops while persistence
and trace capture are disabled. Trace-enabled lifecycle hooks still record a
boundary even without persistent history. Ending a session intentionally removes its history; resuming an ended
session starts a new bounded window.

The store uses SQLite transactions to prevent lost updates between hook processes.
It allows at most 64 sessions and 1 MiB per snapshot, with a 64 MiB database page
limit and a 10-second lock timeout. A short rollback journal may exist during a
transaction. Snapshots are data-only and independently validateable. The file must
be regular, singly linked, and private; on POSIX it and its containing directory
must belong to the invoking user and disallow other writers. The initial file mode
is `0600`. Store files outside repositories and agent-writable directories.

**Persistent session state is private execution context, not a shareable audit.**
It can contain commands, tool arguments, paths, and caller context needed to replay
history. It does not store post-tool output or transcripts. Opt-in storage is not
encrypted or authenticated against its owner. Protect it with the host sandbox;
an attacker with the same user's access can replace the database. SQLite secure
deletion reduces residual deleted content but is not a storage-device erasure
guarantee. Native Windows private-file support is not claimed.

## Embedding and richer observations

Codex uses the same store through `ORDIN_CODEX_STATE` and its installed
start/pre/post/end hooks; see [Codex integration](codex-integration.md).
MCP HTTP creates an isolated in-memory session for each negotiated downstream
token; see [HTTP sessions](mcp-http-proxy.md). A transport/session identity change
does not inherit another client's history. The [offline quickstart](quickstart.md)
exercises persistence, temporal evidence and reset behavior from a wheel.

```python
from ordin import AgentGate
from ordin.claude_code import ClaudeCodeIntegration, build_claude_code_integration
from ordin.session import IntegrationSession, SessionIdentity

gate = build_claude_code_integration().gate
session = IntegrationSession(SessionIdentity("claude-code", "runtime-session-42"), gate)
integration = ClaudeCodeIntegration(gate=gate, session=session)
```

The reusable session also accepts generic `ActionEnvelope` reviews through
`session.evaluate()` and linked `ActionObservation` values through `session.observe()`.
Default hooks/proxy observations contain status and bounded identity metadata only.
A trusted embedding host may supply `observed_effects=("secret.read",)` to
`observation_from_hook()` or `observe_server_message()` when it has independent
evidence of that effect. This is an explicit Python argument, never a field trusted
from an upstream response or model output. Such effects feed the existing temporal
engine and cannot erase a predicted effect or downgrade a decision.

## Validation

The permanent integration evaluation includes synthetic live-session controls
through the maintained integration methods. It reports temporal detections, false
detections, observation linkage, isolation/reset failures, and core review versus
additional in-memory integration latency. Persistence/concurrency/privacy and real
hook-process behavior have separate regression tests. No fixture runs the reviewed
shell command or requires a hosted agent/model.
