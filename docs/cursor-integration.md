# Cursor hooks

Available on the **0.4 development branch**; the published v0.3.0 package does
not include this adapter. Install the development checkout in an isolated
environment before using these commands. Linux and macOS are maintained;
native Windows installation is unsupported.

Ordin reviews proposed actions through the shared `AgentGate`. Cursor owns
execution, sandboxing, credentials, approvals, and retries. The adapter does
not run commands, contact servers, or invoke a model.

## Install and verify

From an installed development environment and this checkout:

```sh
ordin-cursor-hook doctor
ordin-cursor-hook pre < examples/cursor-pre.json
ordin-cursor-hook install .cursor/hooks.json
```

The fixture returns `permission: allow` without executing `git status`.
Installation creates a new file with private permissions and absolute Python
commands. Existing files cause an error; review and merge hook entries manually
if necessary. Verify Cursor has enabled/trusted the hooks. `doctor` checks Ordin
configuration, not the IDE's actual enablement. Moving the Python environment
requires reviewing and updating its recorded executable path.

The installer adds generic pre/post/failure hooks and lifecycle hooks. It wraps
launcher failures with `exit 2`. Never substitute an unguarded launcher:
Cursor otherwise permits actions on some hook failures. Timeout or forced
termination remains a host boundary and requires host verification.

## Contract and decisions

This profile follows the [Cursor hook contract](https://cursor.com/docs/hooks)
reviewed on 2026-09-12. Fixtures use its example version `1.7.2`; CI does not
certify a running Cursor binary. Native `preToolUse` does not enforce `ask`, so
Ordin returns `deny` whenever its configured review policy requires intervention.
Malformed inputs return denial and exit 2. Unknown tools remain untrusted.

| Tool/event | Ordin behavior |
| --- | --- |
| `Shell` | Shared command analysis; supplied working directory becomes context |
| `Read` | Read effect and exact file path |
| `Write`, `StrReplace`, `Delete` | File mutation effects; patch/content bytes become a digest |
| Other patch or future tools | Untrusted until explicitly reviewed semantics exist |
| MCP alias | Exact reviewed server/tool pair; no name splitting or inference |
| `postToolUse` | Links typed exit status from JSON `tool_output` to the reviewed action |
| `postToolUseFailure` | Records error/timeout; permission denial is not execution evidence |
| `subagentStart` / `Task` | Untrusted by default; delegation requires explicit policy review |

An action ID binds conversation, generation, tool-use ID, and tool name.
Observations cannot attach to a denied action or cross a recorded lifecycle
boundary. Command/MCP arguments are needed in memory for review; ordinary audit
and capture stay redacted. Email, transcript path, agent messages, file contents,
and full tool output are not copied into action evidence.

## MCP identity

Generic hooks require an operator-reviewed alias mapping. Review the exact
names your host emits, then adapt [the mapping example](../examples/cursor-mcp-map.json)
and [semantics example](../examples/integrations/mcp-semantics.json):

```sh
export ORDIN_CURSOR_MCP_MAP=/absolute/path/cursor-mcp-map.json
export ORDIN_CURSOR_SEMANTICS=/absolute/path/reviewed-semantics.json
```

These are separate contracts: an alias establishes identity, while semantics
describe effects/resources. Mapping a tool does not make it safe. Built-in names
cannot be replaced by MCP aliases. An explicitly supplied server must match.
For catalog digest enforcement, keep the existing locked Ordin MCP proxy between
Cursor and the server; hooks cannot certify a server's current catalog.

The optional `mcp-pre` mode accepts `beforeMCPExecution` with exact
`mcp_server_name`, `tool_name`, and JSON-string arguments. It is stateless and
rejects session/capture configuration because that event lacks a documented
unique pre/post identifier. It is not installed alongside generic hooks.

## Optional history and capture

```sh
export ORDIN_CURSOR_STATE=/absolute/private/directory/cursor-state.db
export ORDIN_CURSOR_TRACE=/absolute/private/directory/cursor-trace.db
```

Use a private directory. Both paths are opt-in. `ORDIN_CURSOR_AUDIT` optionally
writes standard redacted audit records; `ORDIN_CURSOR_POLICY` selects an action
policy. `ORDIN_CURSOR_FAIL_ON` defaults to `warn`. Raw capture remains disabled
unless explicitly requested through `ORDIN_CURSOR_TRACE_RAW=1`.

`session-start` creates history, `session-end` ends it, and operator-invoked
`session-reset` explicitly clears it using a `sessionStart` payload. A pre-event
arriving before startup initialization denies; retry only after initialization.
Post hooks cannot create missing state. Capture uses the existing
[sanitize/replay/promotion workflow](trace-capture.md).

Conversation histories are isolated. When a host supplies an explicit child ID,
Ordin partitions that child separately. Parent metadata without a child ID is
rejected. Child creation is reviewed and defaults to denial; trusting `Task`
also requires understanding visibility of subsequent child events. Generic
tool and subagent-start proposals are distinct reviews. Ordin never guesses a
child from its type, prompt, or completion summary. The documented stop payload
omits a stable child ID, so cleanup without one is refused and parent state is
preserved. This is not a claim of automatic coverage of every subagent action.

Cloud generic hooks can use the stateless profile once hooks run. Automatic
persistent startup/end is unavailable on ordinary cloud agents; early read-only
turns may bypass hooks. An operator-controlled explicit lifecycle is required
before enabling state there. Local absolute paths also need deployment-specific
replacement. Tab completion hooks are outside this profile.

## Claude compatibility

Cursor can load selected Claude hooks when its
[third-party hook feature and skills setting](https://cursor.com/docs/reference/third-party-hooks)
are enabled. Event/tool translation and approval coverage differ. In particular,
the native pre-tool escalation limitation still matters. Use this native adapter
for exact Cursor identity and conservative denial; do not claim that unchanged
Claude settings provide equivalent session or permission behavior. Ordin never
changes the host's feature, trust, or approval settings automatically.

## Validation

`tests/test_cursor.py` exercises parser denial, file/patch handling, MCP identity,
temporal history, lifecycle, child isolation, and sanitized replay. Shared
conformance and integration evaluation include Cursor, with core and boundary
latency separated. Runtime evaluation launches real isolated hook subprocesses;
the installed-wheel quickstart checks a complete local session lifecycle.
All fixtures are synthetic and offline; host activation still needs a local
benign smoke check after setup.
