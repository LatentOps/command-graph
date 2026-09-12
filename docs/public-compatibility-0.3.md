# v0.3 public compatibility surface

The [versioned inventory](../data/public-surface-0.3.json), also included in the
wheel, defines the supported exports, entry points and schemas. Importable
names outside that inventory and the documented module contracts are internal.

| Surface | Status |
| --- | --- |
| `Ordin`, `AgentGate`, adapters and root `__all__` names | Supported methods, typed input constructors and returned fields. Compiled classes are factory results, not an extension API. |
| Action/history/observation/context/capability/review/provenance records | Supported v1 wire forms, serialized with `.as_dict()`. |
| Policy, temporal policy, semantics, MCP locks and inventory | Supported data-only v1 configuration/interchange with additional semantic checks. |
| Claude Code, Codex, MCP stdio and HTTP | Supported for their documented host/protocol boundaries. |
| Trace capture and promotion metadata | Experimental opt-in; the SQLite layout is internal. |
| Diagnostics/evaluation reports, semantic reranking and man index | Experimental; version markers identify layouts without freezing every field indefinitely. |
| Private names, transport handlers, caches and engine state | Internal. |

Legacy constants such as `MAN_INDEX_SCHEMA_VERSION` stay importable without
making the associated experimental format stable. Package attributes containing
imported modules are not intended exports. Frozen dataclasses do not promise
security-grade immutability of arbitrary nested Python containers.

## Validation and defaults

All 28 registered schemas have canonical runtime/data specimens in permanent
tests, including legacy `action_trace`. Source and wheel resources must match.
The lightweight validator implements bundled schema keywords, not arbitrary
third-party JSON Schema dialects.

`Ordin` mapping entry points validate versioned wire forms before constructing
typed inputs. Legacy convenience builders may fill omitted defaults or version
markers; serialize their results before sending them to a strict wire endpoint.
Unknown named fields are rejected. Extensible action `parameters` and
observation `metadata` remain supported within their recursive budgets.

| Contract | Main bounds/defaults |
| --- | --- |
| Action envelope | Kind 64 chars; operation/ID 128; parameter depth 8, 128 members/items per container, strings 32,768; nullable intent/context/ID |
| Histories | 32 retained entries; observation IDs unique and linked to retained proposals |
| Context | Nullable supplied fields; non-negative integer UID; absolute POSIX paths to establish repository scope |
| Policy/semantics | 1 MiB files, at most 256 rules, exact identities |
| Temporal policy | Windows of 1–32 actions and bounded pattern/signal lists |
| Session/audit/capture | Explicit session ownership and bounded local storage; see their individual guides |

Schemas list structural types, enums, nullability and collection bounds.
Runtime checks additionally enforce finite JSON numbers, duplicate-free
configuration objects, catalog membership, valid argument paths, recursive
budgets and observation/identity relationships. Structural validity never
grants tool trust. Matching preserves identity spelling and does not fold case
or resolve symlinks to broaden path rules.

## CLI and host contracts

| Entry point | Machine behavior |
| --- | --- |
| `ordin` | Bare words search; explicit commands keep their meaning. Help uses stdout/exit 0. Versioned review output is stable; other reports retain their declared status. |
| `ordin-claude-hook` | Pre-tool execute/escalate/deny maps to allow/ask/deny. Malformed pre-input emits deny with exit 0 for the host protocol. Post/lifecycle errors exit nonzero. |
| `ordin-codex-hook` | Unresolved pre-tool decisions deny because host `ask` is not safely supported. Permission hooks preserve the host prompt. |
| `ordin-mcp-proxy` | Stdout is protocol-only; startup errors use stderr/nonzero exit. Tool escalation/block uses JSON-RPC `-32040`/`-32041`. |
| `ordin-mcp-http` | Explicit HTTP session, auth, origin, framing and timeout boundaries. |
| `shell-init` / `orun` | Explicit Bash/Zsh wrappers; ordinary Enter is unchanged. |

Advisory review does not execute and normally exits 0. Malformed CLI/configuration
input generally exits 2; hook post/lifecycle errors exit 1. Enforced failures
map warn/ask/block to 10/20/30; permitted decisions exit 0 under `--fail-on`.
See [enforcement](enforcement.md). Parse version/status fields, not human reasons
or JSON key order. Hosts retain execution, sandbox, credential, retry and
approval ownership; shared Ordin decisions cannot remove host limitations.

## Compatibility and deprecation

Valid v0.2 action/review/trace forms remain readable. This audit tightens invalid
inputs: non-finite numbers, unknown members, invalid typed context/trace bounds
and duplicate configuration members are rejected. No v2 is needed to represent
the valid v1 forms.

Keep documented APIs and existing v1 meanings within the supported 0.3 line.
Removing fields, changing types or reinterpreting meanings requires a new wire
version and migration notes. Review additive capabilities for consumer impact
and announce them in release notes. Retain old readers through at least the
next minor line where safe and practical. Announce planned removals one minor
line ahead when practical; security fixes may reject malformed inputs
immediately with tests and compatibility notes. Experimental/internal surfaces
may change faster. `0.x` does not promise indefinite compatibility.
