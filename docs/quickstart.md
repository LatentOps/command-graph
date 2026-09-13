# Offline v0.3 quickstart

For the 0.4 development branch, start with [setup and local verification](setup.md).
This page remains the manual reference for the published v0.3.0 release.

This guide targets the stable v0.3.0 tag or published wheel from the
[installation guide](installation.md). Use a matching v0.3.0 checkout for
fixtures; v0.2.0 does not contain the new Codex/HTTP/session/capture workflows.

Use an activated Python 3.10–3.13 environment on Linux or macOS and a matching
source checkout for the fixture files. The commands below review synthetic
actions, use local fixtures, and need no model, agent account or credentials.
Running a real agent still requires its own setup and permissions.

## Check the installed package

```bash
ordin doctor --json
ordin check "git status --short" --json --enforce
python -c 'from ordin import AgentGate; assert AgentGate().evaluate("git status --short").may_execute'
```

These commands review text and validate package resources. They do not execute
the reviewed Git command. An integrating host should run an action only when
the gate permits it; sandbox and approval checks remain with that host.

## Claude Code

```bash
ordin-claude-hook --help
ordin-claude-hook pre < examples/claude-code-pre.json
```

The synthetic read should produce `permissionDecision: allow`. Review
[`examples/claude-code-settings.json`](../examples/claude-code-settings.json)
and merge its `hooks` object into `.claude/settings.json`, preserving unrelated
settings. Launch Claude from the activated environment, or replace each hook
command with the absolute executable path shown by `command -v ordin-claude-hook`.
See [the Claude guide](claude-code-integration.md) for decision mapping and tools.

For continuous history, use the lifecycle groups in
[`claude-code-session-settings.json`](../examples/claude-code-session-settings.json)
and explicitly opt into a private state path:

```bash
mkdir -p -m 700 "$HOME/.local/state/ordin"
export ORDIN_CLAUDE_STATE="$HOME/.local/state/ordin/claude-sessions.db"
```

Keep that setting consistent for start/pre/post/end events. Missing or corrupt
state prevents a pre-tool grant; do not configure persistence without lifecycle
hooks. [Live sessions](integration-sessions.md) explains resets and isolation.

## Codex

```bash
ordin-codex-hook doctor
ordin-codex-hook pre < examples/codex-pre.json
ordin-codex-hook install ~/.codex/hooks.json
```

Installation refuses an existing hook file. In that case generate a temporary
file and merge reviewed event groups yourself; do not replace a working config.
Restart Codex and review/trust the definitions in `/hooks`. The test payload
reviews a benign shell action but never executes it. Unresolved pre-tool
decisions deny because Codex cannot safely express Ordin's `ask` there.
The [Codex guide](codex-integration.md) covers explicit MCP aliases, plugin
assets, policy and optional `ORDIN_CODEX_STATE` persistence.

## MCP: inspect, review, lock, proxy

Run from the matching checkout and choose unused output filenames:

```bash
ordin mcp inspect --server-id starter-kit --output inventory.json -- \
  python examples/integrations/fixture_mcp_server.py
ordin semantics scaffold inventory.json --output draft-semantics.json
ordin semantics validate draft-semantics.json --inventory inventory.json
```

The last command intentionally reports unresolved trust. A scaffold has **no
trusted rules**. For this non-executing fixture only, the author-reviewed
registry is provided in the checkout:

```bash
ordin semantics validate examples/integrations/mcp-semantics.json --inventory inventory.json
ordin semantics lock examples/integrations/mcp-semantics.json \
  --inventory inventory.json --output contracts.json
ordin-mcp-proxy --server-id starter-kit \
  --semantics examples/integrations/mcp-semantics.json --contract-lock contracts.json -- \
  python examples/integrations/fixture_mcp_server.py
```

Configure the last command as the MCP client's stdio server command. The client
must initialize, complete `tools/list`, then call `read_note`. The fixture
returns a fixed note without reading a real file. For another server, review
effects/resources yourself; names and descriptions never grant trust. The
[setup guide](mcp-semantics-setup.md) and [proxy guide](mcp-safety-proxy.md)
explain exact identities, drift, pending-call bounds and approval errors.

## MCP HTTP

In two terminals with the same environment and matching checkout:

```bash
python -m ordin.http_evaluation --port 8787
```

```bash
ordin-mcp-http --server-id fixture --upstream http://127.0.0.1:8787/mcp \
  --semantics examples/integrations/http-semantics.json --shell-tool shell --port 8766
```

Connect an MCP client to `http://127.0.0.1:8766/mcp`; initialize before tool calls.
Both services are local fixtures. The [HTTP guide](mcp-http-proxy.md) states
the supported protocol profile, authentication forwarding, session and timeout
limits. A remote upstream requires its own credentials and transport security.

## Capture a synthetic failure and review a regression

Choose a new private output directory outside your repository:

```bash
ORDIN_DEMO_DIR="$(mktemp -d)/capture"
python examples/trace_capture_demo.py "$ORDIN_DEMO_DIR"
ordin trace inspect "$ORDIN_DEMO_DIR/demo.ordin-trace.db" --json
ordin trace replay "$ORDIN_DEMO_DIR/candidate.json" --integration --json
```

The example simulates a read, trusted secret-access evidence, and a denied upload
proposal. It executes no command or remote request. Inspect the whole candidate,
then explicitly promote it to a new local file:

```bash
ordin trace promote "$ORDIN_DEMO_DIR/candidate.json" \
  --target failure --output reviewed-regression.jsonl --json
python scripts/run_regression_replay.py --corpus reviewed-regression.jsonl
```

For real capture, follow [capture/sanitization](trace-capture.md). Raw capture
remains unsafe to share; default metadata and explicit human review reduce
exposure but are not a substitute for checking the candidate.

## Verify every quickstart against a wheel

From the matching checkout, after installing build tools:

```bash
python -m build
ORDIN_QUICKSTART_ENV="$(mktemp -d)/venv"
python -m venv "$ORDIN_QUICKSTART_ENV"
"$ORDIN_QUICKSTART_ENV/bin/python" -m pip install --no-deps dist/*.whl
"$ORDIN_QUICKSTART_ENV/bin/python" -I scripts/check_quickstarts.py --json-out quickstarts-report.json
```

The script refuses an editable checkout, uses installed console scripts, and
exercises doctor/review, Python embedding, Claude persistence, Codex hooks/plugin
installation, MCP inspection/review/pinning/stdio, HTTP loopback, temporal reset,
and trace replay/promotion. Its work files are temporary. The package CI job
runs this same command. Host trust prompts and actual agent/model behavior
remain manual integration checks; see the [compatibility matrix](compatibility.md).
