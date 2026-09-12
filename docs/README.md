# Ordin documentation

Start with what you are trying to do. The implementation details are linked after the user-facing paths.

## I want to install Ordin or use it from a terminal

- [Installation](installation.md)
- [Offline v0.3 quickstart](quickstart.md)
- [Platform and integration compatibility](compatibility.md)
- [Bare intent CLI](bare-intent-cli.md)
- [Generic action review](action-review.md)
- [Declarative action policies](policies.md)
- [Temporal action policies](temporal-policies.md)
- [Interactive shell integration](shell-integration.md)
- [Enforcement and exit codes](enforcement.md)

Typical commands:

```bash
ordin what is using port 3000
ordin check "git reset --hard HEAD~1"
cat action.json | ordin action --stdin --json
ordin policy validate examples/policy.json
ordin temporal validate data/temporal_policies.json
source <(ordin shell-init bash)
orun 'git status --short'
```

## I want to embed Ordin in an application

- [Python API](python-api.md)
- [Generic action review](action-review.md)
- [Execution capability profiles and observations](execution-evidence.md)
- [Decision provenance and local audit evidence](audit-and-provenance.md)
- [Tool and MCP adapters](tool-and-mcp-adapters.md)
- [Declarative action policies](policies.md)
- [Temporal action policies](temporal-policies.md)
- [Schema contracts](schema-contracts.md)
- [v0.3 public compatibility surface](public-compatibility-0.3.md)
- [Context-aware review](context-aware-review.md)
- [Trace-aware review](trace-aware-review.md)

The Python API reviews actions but never executes them.

## I want to gate an AI agent

- [Integration starter kit](../examples/integrations/README.md)
- [Claude Code hooks](claude-code-integration.md)
- [Codex hooks and plugin](codex-integration.md)
- [Live integration sessions](integration-sessions.md)
- [MCP safety proxy](mcp-safety-proxy.md)
- [MCP Streamable HTTP proxy](mcp-http-proxy.md)
- [MCP contract pinning](mcp-contract-pinning.md)
- [MCP inspection and semantics setup](mcp-semantics-setup.md)
- [Integration diagnostics](integration-troubleshooting.md)
- [Agent runtime integration](agent-integration.md)
- [Tool and MCP adapters](tool-and-mcp-adapters.md)
- [Execution capability profiles and observations](execution-evidence.md)
- [Decision provenance and local audit evidence](audit-and-provenance.md)
- [Generic action review](action-review.md)
- [Declarative action policies](policies.md)
- [Temporal action policies](temporal-policies.md)
- [Python API](python-api.md)
- [Trace-aware review](trace-aware-review.md)
- [Enforcement and exit codes](enforcement.md)

The canonical integration is:

```text
agent proposes action -> Ordin -> execute / escalate / deny -> caller runtime
```

Review APIs return decisions without executing actions. The optional shell
wrapper executes reviewed commands, and the MCP proxy relays stdio traffic to
its configured server. The runtime owns sandboxing, credentials, and approvals.

## I want to understand how Ordin decides

- [Architecture](architecture.md)
- [Generic action review](action-review.md)
- [Execution capability profiles and observations](execution-evidence.md)
- [Decision provenance and local audit evidence](audit-and-provenance.md)
- [Declarative action policies](policies.md)
- [Temporal action policies](temporal-policies.md)
- [Typed effect graph](effect-graph.md)
- [Semantic analyzers](semantic-analyzers.md)
- [Context-aware review](context-aware-review.md)
- [Trace-aware review](trace-aware-review.md)

## I want to evaluate Ordin

- [Local integration evaluation and recorded reports](integration-evaluation.md)
- [Adapter conformance](integration-conformance.md)
- [Agent trajectory corpus](agent-trajectory-corpus.md)
- [Failure replay](failure-regressions.md)
- [Extended regression promotion](regression-promotion.md)
- [Local trace capture and reviewed promotion](trace-capture.md)
- [Safety benchmark and adversarial fuzzing](safety-benchmark.md)
- [Search quality benchmark](search-quality-benchmark.md)

The safety benchmark has separate hard regression gates for false allows, critical misses, false blocks, and weakened adversarial equivalents.

## I want to understand command discovery

- [Search quality benchmark](search-quality-benchmark.md)
- [Deterministic ranking](deterministic-ranking.md)
- [Availability and Linux platform signals](availability-and-platforms.md)
- [Optional semantic reranking](semantic-reranking.md)
- [Command packs](command-packs.md)

## I want to extend or contribute to Ordin

- [Development workflow](development-workflow.md)
- [Safety benchmark and adversarial fuzzing](safety-benchmark.md)
- [Generic action review and adapter contract](action-review.md)
- [Execution capability profiles and observations](execution-evidence.md)
- [Decision provenance and local audit evidence](audit-and-provenance.md)
- [Tool and MCP adapters](tool-and-mcp-adapters.md)
- [Declarative action policies](policies.md)
- [Temporal action policies](temporal-policies.md)
- [Command packs](command-packs.md)
- [Schema contracts](schema-contracts.md)
- [Releasing Ordin](releasing.md)
- [v0.3 release notes](releases/v0.3.0.md)
- [Security and threat model](threat-model.md)
- [Contributing guidelines](../CONTRIBUTING.md)

The local quality contract is:

```bash
python -m pip install -e ".[dev]"
pre-commit install
pre-commit run --all-files
pytest -q
```

The core design rule across safety layers is conservative composition: richer semantic, contextual, temporal, adapter, observed, provenance, or caller-owned policy evidence must not erase a known stronger safety requirement. Execution policy is applied separately by the caller or shell integration.
