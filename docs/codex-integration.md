# Codex integration

`ordin-codex-hook` translates Codex lifecycle events into the same `ActionEnvelope`,
`AgentGate`, policies, temporal history, and observations used by other Ordin
integrations. Codex owns execution, sandboxing, credentials, approvals, and retries.

This feature requires Ordin 0.3; while that release is in preparation, install
the development build. The 0.2.0 release does not contain it.
The adapter follows the [official Codex hook contract](https://developers.openai.com/codex/hooks/),
checked on 2026-09-12. Evaluation uses local fixtures without model requests.

## Install and verify

In an activated Linux or macOS environment with Ordin 0.3 installed:

```bash
ordin-codex-hook doctor
ordin-codex-hook install ~/.codex/hooks.json
```

The installer writes native hook configuration with absolute paths to the installed
interpreter and packaged launcher. It refuses to overwrite an existing file. If
that layer already exists, install to a temporary file and merge its event groups
after reviewing both configurations. Avoid installing duplicate handlers in multiple
layers: Codex runs all matching hooks.

The [offline quickstart](quickstart.md) tests this installer and protocol path
against the built wheel, without launching Codex or making a model request.

Restart Codex, open `/hooks`, and review/trust the definitions. Codex skips untrusted
or disabled hooks. `doctor` validates configuration loading, not the host's current
hook trust, feature settings, or complete tool coverage.

Reusable plugin source is in [`plugins/ordin`](../plugins/ordin), with a compatibility
manifest and default `hooks/hooks.json`. Administrators can include that folder in
an existing marketplace. The Python wheel also includes the launcher/config, so
installation does not require a checkout. Importing Ordin changes no marketplace
or personal Codex configuration.

Run a safe, model-free protocol check:

```bash
printf '%s\n' '{"hook_event_name":"PreToolUse","session_id":"smoke","turn_id":"turn","tool_use_id":"call","tool_name":"Bash","tool_input":{"command":"git status --short"},"cwd":"/workspace","permission_mode":"default"}' | ordin-codex-hook pre
```

This reviews text without executing it. Runtime evaluation also tests a destructive
proposal, unknown tool, patch, and redacted post-tool correlation in subprocesses.

## Decision mapping

| Ordin result | Python disposition | Codex pre-tool output |
| --- | --- | --- |
| Allowed by `ReviewPolicy` | `execute` | `permissionDecision: allow` |
| Warning/uncertainty requiring review | `escalate` | `permissionDecision: deny`, with review-required reason |
| Explicit block | `deny` | `permissionDecision: deny` |
| Invalid input, unavailable state, handler failure | Error/deny | Supported denial or blocking exit code 2 |

**Codex currently does not support `permissionDecision: ask` in `PreToolUse`.**
Its documented behavior reports a hook failure and continues the call. Ordin denies
unresolved proposals instead. After reviewing an action, a user can narrow/update
its explicit local policy or semantics and retry. Python embeddings still receive
`requires_approval` for their own approval UI.

`PermissionRequest` reviews without appending the proposal twice to history. Explicit
blocks deny it; other decisions return an empty object and preserve Codex's normal
sandbox approval prompt. Ordin does not grant broader sandbox permissions based on
command classification.

## Action mapping

| Hook tool | Ordin action | Semantics |
| --- | --- | --- |
| Exact `Bash` | `shell.execute`, exact command | Existing shell analyzers |
| Exact `apply_patch` | `tool.call`, runtime `codex`, extracted targets and digest | Conservative write/delete capabilities, explicit path resources |
| Explicitly mapped MCP hook alias | `mcp.call`, exact configured server/tool | Existing MCP adapter and reviewed semantics |
| Other local tool | Generic `tool.call`, runtime `codex` | `ask` until reviewed semantics exist |

Patch framing and target extraction are bounded to 1 MiB and 64 paths. Unsupported
framing fails closed. Add/update/delete/move targets are retained exactly; source
lines are replaced with a digest before history/audit processing. The fixed patch
rule describes the tool's possible write/delete capabilities, not observed effects
or a claim that every patch deletes a file. Resource policy and the sandbox govern
target access; Ordin does not resolve symlinks or normalize paths into trust.

MCP hook names are not split on `__`: original server/tool names can contain that
delimiter, and aliases need not preserve their original identities. Configure exact
mapping and semantics explicitly:

```bash
export ORDIN_CODEX_MCP_MAP=/absolute/path/codex-mcp-map.json
export ORDIN_CODEX_SEMANTICS=/absolute/path/reviewed-semantics.json
```

See [`examples/codex-mcp-map.json`](../examples/codex-mcp-map.json). Mapping grants no
permission. Enforce [contract pinning](mcp-contract-pinning.md) with the MCP proxy:
Codex's call hook does not expose a complete live discovery inventory itself.

## Configuration and state

| Variable | Purpose |
| --- | --- |
| `ORDIN_CODEX_POLICY` | Reviewed action-policy JSON |
| `ORDIN_CODEX_SEMANTICS` | Additional exact semantics; cannot shadow the built-in patch rule |
| `ORDIN_CODEX_MCP_MAP` | Exact hook-alias to server/tool mapping |
| `ORDIN_CODEX_FAIL_ON` | `warn` by default; existing `ask`/`block` thresholds also accepted |
| `ORDIN_CODEX_STATE` | Opt-in private SQLite session database |
| `ORDIN_CODEX_AUDIT` | Optional redacted decision audit |
| `ORDIN_CODEX_OBSERVATIONS` | Optional redacted post-tool JSONL |
| `ORDIN_CODEX_TRACE` | Optional private action-capture database |
| `ORDIN_CODEX_TRACE_RAW` | Explicit `1` adds raw local actions; unsafe to share |

For continuous temporal review, create an owner-only directory outside the repository
and set `ORDIN_CODEX_STATE` consistently for every hook. Start/pre/post/end hooks reuse
[shared session state](integration-sessions.md), its 32-action bound, configuration
binding, missing/corrupt-state denial, reset/end semantics, and privacy limits. Alias
mappings are part of the configuration identity. Nothing persists by default.

Action IDs bind session, turn, tool-use ID, and exact tool name. Subagents use the
parent session supplied by Codex and retain hashed agent metadata when present.
Unknown/duplicate correlations cannot attach to another action. `session-reset` is
an explicit operator action after outstanding tools stop.

Observations contain correlation and result status, with an exit code when provided
as typed runtime metadata. Arbitrary output text is not parsed as an attested status.
Prompts, transcripts, output, and patch source are absent from observations. A trusted
Python host may supply `observed_effects`; raw responses cannot grant themselves
trusted effect labels.

## Runtime limits

[Codex tool coverage](https://developers.openai.com/codex/hooks/#tool-coverage) includes
shell/unified exec, patches, MCP, and most local function tools. Hosted tools and some
specialized paths do not use these hooks. `write_stdin` does not trigger another
pre-tool review for an existing process. Hooks skipped, disabled, or killed by the
host cannot enforce Ordin's decision. Other trusted hooks can rewrite inputs, so
the complete hook stack is part of the execution trust boundary.

The launcher bounds its child to 12 seconds within a 30-second tool-hook timeout and
maps handler startup/errors to denial. Session-end cleanup uses a shorter timeout
and is advisory. Host termination/timeout behavior remains outside Ordin's control.
Keep sandbox restrictions and trusted hook configuration in place. Native Windows
hook installation is not currently claimed.
