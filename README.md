# Ordin

**Know what an action will do before it runs.**

Ordin is a local command-intelligence and pre-execution safety engine for developers and AI agents. It can find shell commands from natural-language intent, review proposed commands and generic actions before execution, and act as a deterministic gate between an agent and its runtime.

Core search and safety review are local-first. Ordin does not require a cloud service, upload command or action history, or execute agent actions on its own.

## Install

Ordin is distributed directly through GitHub for now.

Install the current stable release from its immutable `v0.1.0` tag:

```bash
python -m pip install "git+https://github.com/LatentOps/ordin.git@v0.1.0"
ordin doctor
```

You can also install the validated wheel attached to the GitHub release:

```bash
python -m pip install https://github.com/LatentOps/ordin/releases/download/v0.1.0/ordin-0.1.0-py3-none-any.whl
```

Install the current development tree directly from the repository:

```bash
python -m pip install "git+https://github.com/LatentOps/ordin.git"
```

Or clone it:

```bash
git clone https://github.com/LatentOps/ordin.git
cd ordin
python -m pip install .
```

See [Installation](docs/installation.md) for development setup and optional semantic reranking.

## Four ways to use Ordin

### 1. Find the command you need

Describe the task instead of remembering the exact Linux command:

```bash
ordin what is using port 3000
ordin find files larger than 1gb
ordin make file runnable
```

Or use the explicit search command:

```bash
ordin search "lookup dns for example.com" --limit 3
```

Ordin uses deterministic lexical retrieval by default and can incorporate local command availability and Linux distribution signals.

### 2. Review a command or generic action

```bash
ordin check "git reset --hard HEAD~1"
```

Example:

```text
decision: warn
risk: high
- changes local source-control state
- can rewrite or discard source-control history
```

For richer shell review, provide intent and execution context:

```bash
ordin review \
  --intent "remove generated dependencies" \
  --command "rm -rf node_modules" \
  --cwd "$PWD" \
  --repo-root "$PWD" \
  --json
```

Or review a versioned generic action:

```bash
cat examples/action.json | ordin action --stdin --json
```

Unknown commands and actions are not silently treated as safe.

### 3. Add an explicit local policy

Validate a data-only policy:

```bash
ordin policy validate examples/policy.json
```

Apply it to a generic action:

```bash
cat examples/action.json | ordin action --stdin --policy examples/policy.json --json
```

Policies match structured action kinds, operations, effects, resources, context, agent identity, intent state, and trajectory findings. They are explicit local JSON, cannot execute code, and can only preserve or strengthen the core safety requirement.

See [Declarative action policies](docs/policies.md).

### 4. Gate an AI agent before execution

```python
from ordin import AgentGate

result = AgentGate().evaluate(
    "git status --short",
    intent="inspect repository state",
)

if result.may_execute:
    # Execute through your own sandbox or tool runtime.
    ...
elif result.requires_approval:
    # Ask a human or caller-owned approval service.
    ...
else:
    # Reject the proposed action.
    ...
```

The runtime flow is deliberately small:

```text
agent proposes action
        |
        v
      Ordin
        |
        +--> execute
        +--> escalate
        +--> deny
        |
        v
caller-owned tool / shell / sandbox
```

`AgentGate` never runs the command. The integrating runtime owns execution, sandboxing, approval UI, retries, and trace persistence.

Want a runnable path instead of API-only snippets? Start with the [integration starter kit](examples/integrations/README.md). It covers Python embedding, JSON/subprocess runtimes, the MCP proxy, shell/coding-agent gating, and non-interactive CI checks with offline smoke-tested examples.

See [Agent runtime integration](docs/agent-integration.md), [Generic action review](docs/action-review.md), and [Python API](docs/python-api.md).

## Optional shell gate

Shell integration is explicit and reversible. Ordin does not edit shell startup files automatically.

For Bash:

```bash
source <(ordin shell-init bash)
orun 'git status --short'
orun 'rm -rf ./build'
```

For Zsh:

```zsh
source <(ordin shell-init zsh)
```

`orun` reviews the exact command first and only executes according to the shell integration's review flow. See [Shell integration](docs/shell-integration.md).

## What Ordin evaluates

A review can combine:

1. shell-aware parsing of compound commands, pipelines, substitutions, groups, and nested shell payloads;
2. semantic analyzers for high-value command families;
3. typed effects such as filesystem deletion, network upload, package installation, source-control mutation, privilege escalation, and container changes;
4. structured resources such as paths, URLs, packages, and later tool-specific targets;
5. exact rules for dangerous combinations;
6. optional execution context such as working directory, repository boundary, effective UID, shell, and agent identity;
7. bounded recent-action traces for multi-step patterns;
8. explicit caller-owned declarative policy.

The result uses four review decisions:

| Decision | Meaning |
| --- | --- |
| `allow` | Known low-risk behavior with no stronger finding. |
| `warn` | Known elevated behavior that should be reviewed. |
| `ask` | Ordin cannot establish enough confidence or policy requires approval. |
| `block` | A known critical condition or explicit blocking policy was detected. |

Agent integrations can then apply a separate `ReviewPolicy` to decide what proceeds automatically and what requires escalation.

Generic action reviews also expose deterministic execution-capability recommendations and structured decision provenance. Callers may optionally attach a local redacted JSONL audit sink; audit persistence is disabled by default and Ordin never uploads the evidence. See [Execution capability profiles and observations](docs/execution-evidence.md) and [Decision provenance and local audit evidence](docs/audit-and-provenance.md).

## Machine-readable review

Non-Python runtimes can send versioned requests through stdin:

```bash
cat examples/review.json | ordin review --stdin --json
cat examples/action.json | ordin action --stdin --json
```

Public payloads use versioned Ordin schemas, including `ordin.review_request.v1`, `ordin.action_envelope.v1`, `ordin.action_review.v1`, and `ordin.policy_set.v1`.

Schemas live in [`schemas/`](schemas/) and are validated by `ordin doctor` and CI.

## Command intelligence

Search is deterministic by default. Ordin combines BM25-style lexical scoring with intent, aliases, examples, templates, command availability, and Linux distribution compatibility.

```bash
ordin search "find large files" --json
ordin explain find
ordin packs
```

An optional local semantic reranker can reorder only a bounded deterministic candidate set. It is not part of the default dependency set and Ordin does not automatically download a model.

Install semantic support from a repository checkout:

```bash
git clone https://github.com/LatentOps/ordin.git
cd ordin
python -m pip install ".[semantic]"
```

See [Deterministic ranking](docs/deterministic-ranking.md), [Availability and platforms](docs/availability-and-platforms.md), and [Semantic reranking](docs/semantic-reranking.md).

## Typed effect graph

Curated command metadata can express:

```text
intent -> command
command -> subcommand
command/subcommand -> flag
command/subcommand/flag -> effect
effect -> resource
command -> safer alternative
command -> required privilege
```

The graph is built locally in memory and does not require a graph database.

```bash
ordin graph
ordin graph --json
```

See [Effect graph](docs/effect-graph.md) and [Schema contracts](docs/schema-contracts.md).

## Documentation by goal

| I want to... | Start here |
| --- | --- |
| install or use the CLI | [Installation](docs/installation.md), [Bare intent CLI](docs/bare-intent-cli.md) |
| integrate Ordin into an agent/runtime | [Integration starter kit](examples/integrations/README.md), [Agent runtime integration](docs/agent-integration.md) |
| review generic actions | [Generic action review](docs/action-review.md) |
| define local policy | [Declarative action policies](docs/policies.md) |
| embed Ordin in Python | [Python API](docs/python-api.md) |
| gate an AI agent | [Agent runtime integration](docs/agent-integration.md) |
| enable reviewed shell execution | [Shell integration](docs/shell-integration.md) |
| understand safety behavior | [Architecture](docs/architecture.md), [Context-aware review](docs/context-aware-review.md), [Trace-aware review](docs/trace-aware-review.md) |
| inspect provenance or keep a local audit trail | [Decision provenance and local audit evidence](docs/audit-and-provenance.md) |
| extend command knowledge | [Command packs](docs/command-packs.md), [Semantic analyzers](docs/semantic-analyzers.md), [Effect graph](docs/effect-graph.md) |
| contribute | [Contributing](CONTRIBUTING.md), [Development workflow](docs/development-workflow.md) |

See the full [documentation index](docs/README.md).

## Development

```bash
python -m pip install -e ".[dev]"
pre-commit install
pre-commit run --all-files
pytest -q
```

The shared pre-commit gate covers Ruff lint and formatting, staged mypy checks, compilation, `ordin doctor`, and repository namespace integrity. Pull requests additionally run Python 3.10 through 3.13, built-wheel validation, and isolated Debian/Fedora installation smoke tests.

## Repository layout

```text
ordin/              library, CLI, analyzers, action/policy engine, packaged resources
data/               curated command cards, effects, packs, and risk rules
schemas/            public JSON Schema contracts
benchmarks/         deterministic search-quality fixtures
examples/           human and agent integration examples
docs/               usage, architecture, safety, and development documentation
scripts/            repository integrity tooling
tests/              unit, integration, packaging, and safety tests
.github/workflows/  CI and release pipelines
```

## Design boundaries

Ordin intentionally does not:

- execute commands or generic actions received through review APIs;
- become a general agent framework;
- require a hosted service for core behavior;
- upload shell history, command text, or action payloads;
- automatically fetch remote policy;
- generate arbitrary shell programs from free-form prompts;
- treat unclassified behavior as safe.

## Release

Current public release: [Ordin v0.1.0](https://github.com/LatentOps/ordin/releases/tag/v0.1.0).

Future version tags are validated and published as GitHub Releases with wheel and source-distribution assets.

## License

Ordin is licensed under the Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
