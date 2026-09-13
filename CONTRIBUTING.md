# Contributing to Ordin

Ordin is an open-source LatentOps project for local, intent-aware
command discovery and pre-execution safety review. Contributions should keep the
project local-first, explainable, and useful without cloud services.

## Development setup

For bugs and setup friction, start with the [safe feedback intake guide](docs/feedback-intake.md).
It covers structured reports, sanitized captures, private vulnerability disclosure,
and promotion of reproducible failures into permanent regressions.

Install the project and repository tooling into a development environment:

```bash
python -m pip install -e ".[dev]"
pre-commit install
```

Run the same local quality and integrity gate used by CI:

```bash
pre-commit run --all-files
```

The hooks run Ruff lint and format checks, staged mypy checks for typed API
boundaries, Python compilation, `ordin doctor`, and the Ordin namespace guard.
The hooks use the development dependencies declared in `pyproject.toml`, so
local tooling and CI do not maintain separate tool-version configurations.

The full test suite remains a separate required gate:

```bash
pytest -q
```

## Requirements

Before opening a pull request:

- Run `pre-commit run --all-files`.
- Run `pytest -q`.
- Keep core behavior offline by default.
- Do not add telemetry, command upload, shell history upload, action upload, or required remote services.
- Keep dependencies small and justify any new dependency in the pull request.
- Preserve stable schema versions for public search, command review, generic action review, policy, indexing, and graph contracts.
- Never add an execution side effect to review APIs. Ordin reviews caller-owned actions; the caller owns execution.

## Generic action adapters

Generic action adapters are part of Ordin's trusted safety boundary.

New adapters must:

- derive semantics from deterministic parsing or curated Ordin metadata rather than trusting caller-provided risk/effect labels;
- return `ask` when they cannot establish enough semantics to classify an action safely;
- reuse the shared effect vocabulary and expose structured resources separately;
- keep all inputs bounded and schema validated;
- avoid network access, hosted inference, plugin loading, or action execution;
- preserve an equivalent or stronger safety outcome for actions that can already be expressed through an existing adapter;
- include offline tests for safe, risky, malformed, unknown, and installed-package behavior;
- document the public action kind, operations, effects, resources, and conservative fallback behavior.

See `docs/action-review.md` for the extension contract.

## Declarative action policies

Policy changes need the same level of review as safety-engine changes.

The core policy language is intentionally data-only. Policy contributions must not add:

- arbitrary Python or callback execution;
- shell evaluation;
- embedded expression languages;
- unbounded regular-expression policy programs;
- automatic remote policy fetching;
- hidden global or home-directory policy discovery.

Policy rules may only preserve or strengthen the execution requirement from the
core review. They must never downgrade an existing `warn`, `ask`, or `block`.
Conflict resolution uses the execution enforcement order `allow < warn < ask < block`.

Changes to policy selectors or semantics must include:

- public JSON Schema updates with source/package parity;
- parser and invalid-input tests;
- conflict and monotonicity tests;
- context-missing tests where applicable;
- CLI/Python tests and installed-wheel coverage for public behavior;
- documentation describing the selector and its fail-closed behavior.

Keep policy files explicit. Ordin should not silently load a policy unless the caller
configured it or supplied a policy path.

See `docs/policies.md`.

## Command Cards

New or changed command cards must include:

- `schema_version`
- `command`
- `summary`
- `aliases`
- `intents`
- `default_risk`
- `risk_tags`
- at least one safe example when possible
- templates only when slot extraction can fill them predictably

Typed graph metadata is optional for backwards compatibility, but new cards
that cover mutation, network transfer, package changes, source control,
containers, process control, or other safety-relevant behavior should declare
semantic effects when the shared effect catalog has an appropriate term.

Supported graph metadata includes:

- `effects` on a command;
- `flags` with aliases and effects;
- `subcommands` with their own effects and flags;
- `requires_privileges`;
- `safer_alternatives`.

Effect references must exist in `data/effects.json`. Prefer a reusable semantic
effect such as `filesystem.delete` over a command-specific label such as
`rm.delete`.

Template targets must not be invented implicitly. If a template has a benign
default that is safe to apply without additional user intent, declare it
explicitly with `safe_defaults`. Do not use `safe_defaults` to choose mutation
targets such as files, directories, branches, processes, or remote resources.

Command-card pull requests should also include:

- one search test for a natural-language query;
- one graph/effect test when adding typed semantics;
- one risk test when the command can mutate files, permissions, processes, packages, network state, containers, or system configuration;
- conservative wording for risky examples.

Avoid adding destructive examples unless the example is clearly labeled,
narrowly scoped, and paired with a safer inspection step.

## Effect Catalog

Effect catalog changes affect multiple commands and need explicit review.

New effects must include:

- a stable dotted effect name;
- `risk`;
- `category`;
- `description`;
- `reason`;
- `safer_next_step` when the effect is mutating or otherwise elevated.

Use effects to describe observable action semantics, not policy decisions.
Command-specific parsing belongs in command metadata or semantic analyzers;
the catalog should remain reusable across command families.

Run `validate_effect_graph_data()` indirectly through the normal test suite and
`ordin doctor`. Unknown effect references or broken graph relationships
must fail validation.

## Risk Rules

Risk rules affect safety behavior and need stricter review than ordinary metadata.

Changes to risk rules must include:

- tests for the risky command pattern;
- tests that normal read-only commands are not over-blocked when relevant;
- a conservative risk category;
- a clear `safer_next_step`.

Typed effects and risk rules are complementary. Effects provide reusable
command semantics; risk rules remain useful for exact dangerous combinations,
critical path patterns, shell composition, and other cases that require more
specific matching.

False-safe behavior is the highest-priority bug class. If an action can cause
data loss, expose secrets, broaden permissions, terminate processes, install
untrusted code, or mutate infrastructure, prefer warning over silence. If the
action is not classified well enough to establish safety, prefer `ask` over
silently treating it as low risk.

## Man-Page Indexing

The local man-page indexer must remain optional. Ordin should still work
from bundled curated data when `apropos` or `man -k` is unavailable.

Man-page entries intentionally remain valid without typed effects; curated
cards can add richer semantics incrementally.

Tests for index behavior should use saved input text rather than requiring
local man-db tools on the test machine.

## Pull Request Checklist

- Scope is focused and unrelated cleanup is avoided unless it removes dead code exposed by the change.
- `pre-commit run --all-files` passes.
- Tests pass.
- `python -m ordin doctor` passes.
- New JSON output keeps a schema version.
- Effect references and typed graph relationships validate.
- Generic adapters and policy changes fail closed rather than assuming unknown behavior is safe.
- Policy rules cannot weaken stronger core findings.
- Documentation is updated when CLI behavior, schemas, safety semantics, or contributor requirements change.
