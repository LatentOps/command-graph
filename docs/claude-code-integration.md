# Claude Code integration

For continuous temporal review across hook processes, see
[live integration sessions](integration-sessions.md). Session persistence is explicit
through `ORDIN_CLAUDE_STATE`; the default hook mode remains stateless.

Ordin ships a dependency-free reference integration for Claude Code command hooks. It uses Claude Code's native `PreToolUse`, `PostToolUse`, and `PostToolUseFailure` events rather than an SDK or hosted service.

This integration was selected because Claude Code exposes a stable pre-execution decision point and structured post-execution events. That lets Ordin sit at the intended boundary:

```text
Claude proposes tool call
        |
        v
  PreToolUse hook
        |
        v
      Ordin
        |
        +--> allow
        +--> ask
        +--> deny
        |
        v
Claude-owned permission / execution layer
        |
        v
 PostToolUse / PostToolUseFailure
        |
        v
optional local ActionObservation evidence
```

Ordin never executes the Claude Code tool. Claude Code continues to own tool execution, credentials, sandboxing, permission UI, retries, cancellation, and runtime recovery.

Claude Code hook behavior is documented upstream at <https://code.claude.com/docs/en/hooks>.

## Install

Install Ordin from the repository or a built wheel, then verify the hook entry point exists:

```bash
ordin-claude-hook
```

The command exits with usage information until a hook mode is supplied.

## Configure hooks

A minimal project configuration is available at [`examples/claude-code-settings.json`](../examples/claude-code-settings.json). Copy the `hooks` object into the applicable Claude Code settings file:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {"type": "command", "command": "ordin-claude-hook pre"}
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {"type": "command", "command": "ordin-claude-hook post"}
        ]
      }
    ],
    "PostToolUseFailure": [
      {
        "matcher": "*",
        "hooks": [
          {"type": "command", "command": "ordin-claude-hook post-failure"}
        ]
      }
    ]
  }
}
```

Using `matcher: "*"` is intentional. A newly introduced or unrecognized tool must not bypass Ordin merely because its name was absent when the integration was written. Unknown tools normalize to a generic `tool.call` and require approval.

## Decision mapping

The integration uses the normal conservative `AgentGate` policy:

| Ordin runtime disposition | Claude Code hook decision |
| --- | --- |
| `execute` | `allow` |
| `escalate` | `ask` |
| `deny` | `deny` |

An explicit Ordin `block` always becomes `deny`. Unknown or untrusted tool semantics become `ask`. Malformed `PreToolUse` input fails closed to `deny` rather than allowing the action without review.

Claude Code's own permission system remains an independent caller-owned boundary. Ordin does not modify Claude Code permission rules.

## Supported built-in semantics

The reference integration currently has exact local semantics for these stable built-in identities:

| Claude Code tool | Ordin handling |
| --- | --- |
| `Bash` | existing shell parser, semantic analyzers, context and risk rules |
| `Read` | `filesystem.read` |
| `Glob` | `filesystem.metadata_read` |
| `Grep` | `filesystem.read` |
| `Write` | `filesystem.write` |
| `Edit` | `filesystem.write` |
| `NotebookEdit` | `filesystem.write` |
| `WebFetch` | `network.download` |
| `WebSearch` | `network.connect` |

The exact runtime identity is `claude-code`. Trusted tool semantics are keyed by exact `(runtime, tool)` identity; changing either identity removes the trusted match and fails closed to `ask`.

`Bash` is deliberately not represented by static tool effects. Its command text is passed through the existing shell review engine so nested shells, pipelines, substitutions, redirections, Git operations, infrastructure commands, and critical command rules retain their existing semantics.

## Context and identity binding

For each `PreToolUse` event, Ordin preserves:

- exact tool name;
- exact structured `tool_input`;
- working directory;
- Claude Code permission mode;
- optional agent type;
- a deterministic action ID bound to session ID, tool-use ID, and tool name;
- SHA-256 identity bindings for session/tool-use identifiers rather than storing the raw identifiers in action metadata.

The native hook payload does not expose the current user intent or a bounded recent-action history. The reference hook therefore does not fabricate either value or parse the transcript to guess them. Embedders that possess trustworthy intent/history can still use Ordin's public `AgentGate` APIs directly.

## Post-action observations

`PostToolUse` and `PostToolUseFailure` can be normalized into `ordin.action_observation.v1` evidence using the same deterministic action ID as the pre-tool review.

Observation persistence is disabled by default. To append redacted observations explicitly to a local JSONL file:

```bash
mkdir -p .ordin
export ORDIN_CLAUDE_OBSERVATIONS="$PWD/.ordin/claude-observations.jsonl"
```

The integration records bounded metadata such as runtime, hook event, tool name, permission mode, status, duration, and interruption state. It deliberately does **not** copy Claude Code `tool_response` or error text because either can contain source code, credentials, command output, or other sensitive data.

For failed Bash tool executions, the integration recognizes Claude Code's documented first-line `Exit code N` form when present. Otherwise the observation exit code remains unknown rather than being guessed.

## Optional local audit evidence

Decision audit persistence is also disabled by default. To opt in:

```bash
mkdir -p .ordin
export ORDIN_CLAUDE_AUDIT="$PWD/.ordin/claude-audit.jsonl"
```

This uses Ordin's existing `JsonlAuditSink`. Resource values, summaries, and action IDs remain redacted by default. Ordin never uploads this evidence.

The directory must already exist. The integration does not create hidden storage directories or discover policy/audit locations automatically.

## Python integration

The hook adapter is also available without invoking the console script:

```python
from ordin.claude_code import ClaudeCodeIntegration

integration = ClaudeCodeIntegration()
result = integration.review_pre_tool(pre_tool_payload)

if result.may_execute:
    ...
elif result.requires_approval:
    ...
else:
    ...

observation = integration.observation_from_hook(post_tool_payload)
```

This API remains review/evidence-only. It does not dispatch the tool represented by the payload.

## Safety and compatibility boundaries

- No Claude Code SDK is a runtime dependency.
- No network access is performed by the integration itself.
- No action execution is added to Ordin review APIs.
- Tool input remains subject to Ordin's bounded JSON depth, item count, and string limits. Oversized inputs fail closed.
- Unknown tool identities require approval instead of receiving guessed semantics.
- Existing shell, generic-action, policy, temporal, provenance, capability, and audit contracts are reused rather than forked.
- Post-tool hooks cannot retroactively make a blocked action safe; they only report caller-supplied evidence after execution.

The integration intentionally starts with a small exact semantic surface. Add new trusted tool semantics only when the tool identity and behavior are stable enough to review deterministically.
