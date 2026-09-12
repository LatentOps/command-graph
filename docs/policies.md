# Declarative action policies

Ordin policies add caller-owned constraints on top of the core semantic review engine. Policies are local data. They do not execute code, call remote services, or replace Ordin's semantic analyzers.

A policy can only preserve or strengthen the execution requirement produced by the core review. It cannot turn an existing `block`, `ask`, or `warn` into a less restrictive result.

## Policy file

Policy files use `ordin.policy_set.v1` and JSON in core Ordin:

```json
{
  "schema_version": "ordin.policy_set.v1",
  "policy_id": "example-policy",
  "version": "1",
  "rules": [
    {
      "id": "approve-shell-writes",
      "decision": "ask",
      "when": {
        "kinds": ["shell"],
        "effects_any": [
          "filesystem.delete",
          "filesystem.write",
          "git.remote_write"
        ]
      },
      "reason": "mutating shell actions require approval"
    },
    {
      "id": "block-secret-upload",
      "decision": "block",
      "when": {
        "effects_any": ["network.upload"],
        "trajectory_any": ["trajectory_secret_exfiltration"]
      },
      "reason": "do not upload data after reading secret material"
    }
  ]
}
```

Core Ordin intentionally does not require a YAML parser. Teams that prefer YAML can convert it to JSON in their own configuration pipeline without adding a parser to the trusted runtime dependency surface.

## Validate a policy

```bash
ordin policy validate policy.json
ordin policy validate policy.json --json
```

Validation checks the public JSON schema and the stricter runtime model, including duplicate rule IDs, supported decisions and selectors, resource match modes, bounded collection sizes, and identifier limits.

Policy files are bounded to 1 MiB and at most 256 rules.

## Apply a policy to an action

```bash
cat examples/action.json | ordin action --stdin --policy policy.json --json
```

Policies are only loaded when explicitly supplied. Ordin does not automatically read `.ordin/policy.json`, a home-directory policy, environment policy URL, or network policy source.

Python callers can compile once and reuse the result:

```python
from ordin import ActionEnvelope, ActionPolicySet, Ordin

policy = ActionPolicySet.from_dict(
    {
        "schema_version": "ordin.policy_set.v1",
        "policy_id": "agent-policy",
        "version": "1",
        "rules": [
            {
                "id": "approve-shell",
                "decision": "ask",
                "when": {"kinds": ["shell"]},
            }
        ],
    }
)

ordin = Ordin(action_policy=policy)
review = ordin.review_action(ActionEnvelope.shell("git status --short"))
```

`ActionPolicySet` is immutable. Compilation is cached by structural policy value, so an application can reuse one compiled policy across many reviews without reparsing rule data.

## Selectors

All selectors present in a rule must match. Within a list selector, any listed value is accepted unless the selector is explicitly named `effects_all`.

Supported selectors are:

| Selector | Meaning |
| --- | --- |
| `kinds` | action kinds such as `shell` or `mcp` |
| `operations` | action operations such as `execute` or `call` |
| `effects_any` | at least one typed effect must match |
| `effects_all` | every listed typed effect must be present |
| `resources_any` | at least one structured resource must match |
| `risks` | base review risk |
| `decisions` | base review decision before policy |
| `agents` | caller-supplied runtime/agent identity |
| `cwd_prefixes` | working directory falls under one local path prefix |
| `repo_scope` | `inside`, `outside`, or `unknown` repository context |
| `privileged` | explicit effective UID indicates root or non-root |
| `intent` | intent presence/alignment state |
| `trajectory_any` | at least one trajectory category is present |

An empty `when` object is an explicit catch-all rule.

### Resource matching

A resource matcher has a type and optionally a value:

```json
{
  "type": "path",
  "value": "/workspace/repo",
  "match": "prefix"
}
```

Only `exact` and `prefix` are supported. Ordin does not execute regular expressions or embedded policy expressions.

### Context matching

Missing context does not pretend to satisfy a context constraint. For example, `"privileged": false` does not match when effective UID is unknown.

`repo_scope` uses the caller-supplied working directory and repository root. Both must be absolute POSIX paths; a missing, empty, or relative value gives scope `unknown`. `cwd_prefixes` also requires absolute POSIX paths. Ordin never resolves these policy inputs against the reviewer's own working directory. Matching is lexical and case-sensitive and does not resolve filesystem aliases.

To require established repository membership, reject both `outside` and `unknown` scope with separate rules. An `outside` rule alone does not match missing or unresolved context.

## Decision semantics

Policy conflict resolution uses Ordin's execution enforcement order:

```text
allow < warn < ask < block
```

This is intentionally different from the internal review aggregation precedence used to preserve the most informative semantic finding. A policy rule that says `ask` must be able to require approval for an action whose semantic review says `warn`.

All matching rules are retained in the output. The strongest policy requirement is merged with the base action decision, again using enforcement order. Therefore:

- `allow` cannot downgrade a base `warn`, `ask`, or `block`;
- `warn` cannot downgrade a base `ask` or `block`;
- `ask` can strengthen `warn` to require approval;
- nothing can downgrade `block`.

## Provenance

When a policy is configured, an action review may include:

```json
{
  "policy": {
    "policy_id": "agent-policy",
    "version": "1",
    "digest": "..."
  },
  "policy_matches": [
    {
      "rule_id": "approve-shell",
      "decision": "ask",
      "reason": "rule requires decision ask",
      "safer_next_step": null
    }
  ]
}
```

The digest is SHA-256 over canonical policy JSON and can be used by later audit/provenance layers to identify exactly which immutable policy content was evaluated.

## Security boundaries

Policy files cannot:

- execute Python;
- execute shell commands;
- call functions or callbacks;
- use regular-expression programs;
- fetch remote data;
- load plugins;
- weaken a stronger core decision.

If a custom action requires confident `allow` semantics, add a deterministic semantic adapter. A policy is not a substitute for understanding an otherwise unknown action.
