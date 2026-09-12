# Ordin

Review commands and tool calls before running them.

Ordin is a Python library and CLI for developers and AI agents. It examines a
proposed action, identifies effects such as file deletion or network access,
and returns `allow`, `warn`, `ask`, or `block` with reasons. You can also describe
a task in plain language to find a shell command for it.

The core runs locally, uses deterministic rules, and has no required runtime
dependencies. It does not send commands, tool arguments, or history to a hosted
service.

## Install and try it

Use Python 3.10 or newer and Git. Ordin targets Linux; CI tests Python 3.10–3.13
and checks installed CLI behavior on Debian and Fedora.

This README describes the development version on `main` (`0.2.0.dev0`). Install
it in a virtual environment to use the APIs and integrations shown here:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install "git+https://github.com/LatentOps/ordin.git@main"
ordin doctor
```

Review a command:

```bash
ordin check "git status --short"
```

```text
decision: allow
risk: low
- reads source-control state (git status)
```

`check` reviews the quoted command without executing it. `doctor` validates
Ordin's local command data and schemas.

<details>
<summary>Install the older tagged release instead</summary>

The latest tagged release is [v0.1.0](https://github.com/LatentOps/ordin/releases/tag/v0.1.0):

```bash
python -m pip install "git+https://github.com/LatentOps/ordin.git@v0.1.0"
```

That release predates the Claude Code hook and MCP proxy. Use `main` for this
guide, or follow the documentation at the release tag. The release also provides
a wheel and source archive.

</details>

See [installation](docs/installation.md) for other install options and optional
semantic search dependencies.

## Find a command

Describe what you want to do:

```bash
ordin what is using port 3000 --limit 1
ordin find files larger than 1gb
ordin search "lookup dns for example.com" --limit 3
```

For the port query, Ordin suggests `lsof -i :3000`. Results include the command's
purpose, examples, risk, and local availability. Ordin prints suggestions; you
choose whether to run them.

Search uses BM25 lexical ranking over curated command cards. The optional
[semantic reranker](docs/semantic-reranking.md) works with an explicitly supplied
local model and does not download one automatically.

## Understand and enforce a decision

The CLI is advisory by default: a completed review exits successfully even when
its decision is `warn`, `ask`, or `block`. Use `--enforce` when a script or CI job
needs a failing exit status:

```bash
ordin check "git status --short" --json --enforce
```

| Decision | What it means | Default `AgentGate` result | Exit with `--enforce` |
| --- | --- | --- | --- |
| `allow` | Recognized behavior with no stronger finding | `execute` | `0` |
| `warn` | Known behavior with elevated risk | `escalate` | `10` |
| `ask` | Unknown semantics or an approval requirement | `escalate` | `20` |
| `block` | A critical condition or explicit blocking policy | `deny` | `30` |

Invalid CLI input returns `2`. An explicit `--fail-on` threshold changes which
decisions cause a nonzero exit. See [enforcement and exit codes](docs/enforcement.md)
for threshold examples and the full exit-code contract.

Provide intent and execution context when they matter:

```bash
ordin review \
  --command "git status --short" \
  --intent "inspect repository state" \
  --cwd "$PWD" \
  --repo-root "$PWD" \
  --json
```

Reviews can combine shell parsing, command semantics, resource paths, local
policy, and bounded action history. Unknown behavior remains uncertain.
Caller-supplied policies can strengthen a decision but cannot weaken a stronger
core finding.

## Use Ordin from Python

`AgentGate` translates a review into a runtime decision:

```python
from ordin import AgentGate

decision = AgentGate().evaluate(
    "git status --short",
    intent="inspect repository state",
)

print(decision.disposition)  # execute
print(decision.may_execute)  # True
```

Your runtime should execute the proposed action only when `may_execute` is true.
It handles approval when `requires_approval` is true and rejects a denied action.
The gate itself never runs the command.

For generic actions, use `ActionEnvelope` and `AgentGate.evaluate_action()`.
`ToolCallAdapter` and `MCPAdapter` preserve tool identity and arguments; trusted
semantics match exact identities. See the [Python API](docs/python-api.md) and
[tool adapters](docs/tool-and-mcp-adapters.md).

## Connect an agent or shell

| Integration | Entry point | Guide |
| --- | --- | --- |
| Claude Code hooks | `ordin-claude-hook` | [Hook configuration and supported tools](docs/claude-code-integration.md) |
| An MCP client and stdio server | `ordin-mcp-proxy` | [Proxy setup and tool semantics](docs/mcp-safety-proxy.md) |
| Bash or Zsh | `ordin shell-init` and `orun` | [Reviewed shell execution](docs/shell-integration.md) |
| Your own runtime | Python API or JSON CLI | [Runnable integration examples](examples/integrations/README.md) |

The MCP proxy reviews `tools/call` requests before forwarding them to its
configured server. Unknown tools require approval by default. Claude Code hooks
map decisions to the runtime's permission flow and can record post-tool
observations.

For a Bash session, enable the shell wrapper explicitly:

```bash
source <(ordin shell-init bash)
orun 'git status --short'
```

`orun` executes a command after its review flow permits it. The MCP proxy launches
the configured server; that server executes its tools.
The integrating runtime remains responsible for credentials, sandboxing, and
approval UI. Ordin does not edit shell startup files automatically.

## JSON, policy, and audit evidence

From a repository checkout, try the versioned example requests:

```bash
ordin review --stdin --json < examples/review.json
ordin action --stdin --json < examples/action.json
ordin policy validate examples/policy.json
ordin action --stdin --policy examples/policy.json --json < examples/action.json
```

Public schemas live in [schemas/](schemas/). The [policy guide](docs/policies.md)
explains action and context selectors; [temporal policies](docs/temporal-policies.md)
cover patterns across multiple actions.

Reviews expose structured provenance and advisory execution-capability profiles.
Local audit persistence is optional and disabled by default. Read
[audit and provenance](docs/audit-and-provenance.md) and
[execution observations](docs/execution-evidence.md) for the evidence contracts
and redaction behavior.

## What the tests establish

CI runs the test suite on Python 3.10–3.13, validates a built wheel, checks Linux
installation, and runs safety, trajectory, regression, and integration gates.
The [evaluation reports](docs/integration-evaluation.md) separate core review,
adapter handling, and local subprocess overhead. Each records its workloads,
revision, environment, and measurement limits.

These checks cover finite fixtures and recognized semantics. An `allow` decision
is not proof that an arbitrary action is safe. Keep execution permissions and
sandbox controls in the runtime, and supply the context and history needed for
the review. Local adapter timing does not measure a complete live-agent session.

## Contribute

```bash
git clone https://github.com/LatentOps/ordin.git
cd ordin
python -m pip install -e ".[dev]"
pre-commit install
pre-commit run --all-files
pytest -q
```

Start with [CONTRIBUTING.md](CONTRIBUTING.md). To extend command knowledge, read
[command packs](docs/command-packs.md) and [semantic analyzers](docs/semantic-analyzers.md).
For debugging and regression work, see [integration diagnostics](docs/integration-troubleshooting.md)
and [regression promotion](docs/regression-promotion.md).

The [documentation index](docs/README.md) links to the architecture, examples,
and detailed API contracts.

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
