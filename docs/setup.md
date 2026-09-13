# Set up an integration

`ordin setup` is available in **0.4 development** on Linux and macOS. Install the
development package in an isolated Python 3.10–3.13 environment first. The stable
v0.3.0 package uses the [manual quickstart](quickstart.md).

```sh
ordin setup cursor --dry-run
ordin setup cursor
ordin setup smoke cursor
ordin setup status
```

Choose `claude`, `codex`, `cursor`, `mcp`, `mcp-http`, or `shell`. Setup creates
reviewable configuration using the installed Python's absolute path. The smoke
command verifies a benign local boundary without model inference or tool
execution. Enable/trust the configuration in the host before real use; setup
does not change its sandbox, approval, credential, or trust settings.

## Files and scope

| Integration | Default root and new file | Activation |
| --- | --- | --- |
| Claude Code | Current project, `.claude/settings.local.json` | Host reads project settings; review hooks |
| Codex | Home directory, `.codex/hooks.json` | Review/trust in Codex `/hooks` |
| Cursor | Current project, `.cursor/hooks.json` | Verify host hook enablement/trust |
| MCP stdio | Current project, `.mcp.json` | Claude project MCP configuration; other clients can use the reviewed server entry |
| MCP HTTP | Current project, `.ordin/mcp-http.json` | Launch proxy explicitly; copy its `client` entry into the host's supported MCP configuration |
| Shell | Current project, `.ordin/shell-init.sh` | Source explicitly from Bash/Zsh; startup files remain user-owned |

`--root /absolute/path` selects a bounded alternative root. `--config relative/path`
selects a new file inside it, for example `.cursor/mcp.json` for Cursor's MCP
configuration. Receipt/evidence directories are reserved. One owned profile per
integration/root is supported; arbitrary TOML/IDE settings merges are manual.

Each setup also writes `.ordin/setup/INTEGRATION.json`, its private ownership
receipt. Existing configuration is never overwritten or reformatted, even if it
contains useful unrelated settings. A conflict leaves that file byte-for-byte
unchanged. Use a reviewed manual merge or remove an unchanged Ordin-owned profile
before generating a replacement. Do not delete unrelated host configuration.

## Preview, rollback, and diagnostics

```sh
ordin setup claude --state --audit --dry-run --json
ordin setup claude --state --audit --json
ordin setup doctor claude --json
ordin setup remove claude --dry-run --json
ordin setup remove claude --json
```

Preview prints exact proposed file contents, hashes, paths, conflicts, and removal
instructions, without writing or contacting any server. Setup atomically publishes
complete new files and refuses a concurrent target. An exclusive setup lock
serializes cooperating writers. Repeating unchanged setup is idempotent.
Ordinary write failures roll back only newly created, unchanged files. If an
interruption leaves a receipt without its target, rerunning identical setup can
complete it. A stale lock is reported for operator review, never automatically
stolen.

Removal requires both the receipt and generated file to match. Edits or foreign
files cause refusal. It deletes only those owned files; private evidence and empty
directories remain. Configuration updates that intentionally differ therefore
use previewed removal followed by setup, or the manual guides below.

`status`/`doctor` reports Ordin version, configured versus foreign/missing state,
missing executables, ownership/permission failures, lock conflicts, and MCP
contract validation. Discovery checks only these documented roots or `--root`.
Diagnostics do not print existing configuration, arguments, credentials, or
evidence. Host activation/version compatibility is explicitly unverified; fixture
compatibility cannot certify a running proprietary host.

## Optional state and evidence

Agent hooks accept `--state` for persistent history and `--audit` for redacted
audit. `--observations` writes redacted post-action evidence; Cursor uses its
metadata trace pipeline for correlation. Files live in the private
`.ordin/private/INTEGRATION` directory. Raw capture is disabled. Setup supplies
matching lifecycle/pre/post commands, clears inherited raw-capture settings, and
retains host-owned policy/semantics configuration.

MCP proxies already maintain history in memory; `--state` is for agent hooks.
MCP accepts `--audit` and `--observations`. Shell evidence requires manual setup.
State and evidence permissions are checked; symlinks, reparse points, multiple
file links, and unsafe writable paths are refused. These controls assume the
owning user controls their files; they do not provide a sandbox.

Cursor `ask` maps conservatively to denial. Its cloud lifecycle/subagent visibility
limits still apply; read the [Cursor contract](cursor-integration.md) before enabling
persistent state outside the supported local lifecycle.

## MCP review workflow

```sh
ordin setup mcp --server-id workspace -- python -m my_server
ordin setup smoke mcp
```

This creates an untrusted proxy profile: unknown tools require intervention.
Setup does not start the supplied server or convert discovery into trusted rules.
It prints the existing inspection/review/lock handoff. With a synthetic or approved
server command, follow that workflow:

```sh
ordin mcp inspect --server-id workspace --output inventory.json -- python -m my_server
ordin semantics scaffold inventory.json --output semantics.json
# Review and edit effects/resources manually using the generated review worksheet.
ordin semantics validate semantics.json --inventory inventory.json
ordin semantics lock semantics.json --inventory inventory.json --output contract-lock.json
ordin setup remove mcp
ordin setup mcp --server-id workspace --inventory inventory.json \
  --semantics semantics.json --contract-lock contract-lock.json -- python -m my_server
```

Inventory, reviewed semantics, and contract lock must be supplied together and
validate against the exact server identity. They remain referenced files, so drift
is diagnosed. Explicit shell-tool trust and credential-bearing launch arguments
require manual configuration. Supply credentials through the host's existing
environment/secret mechanism, never setup command arguments.

For HTTP:

```sh
ordin setup mcp-http --server-id workspace --upstream https://example.invalid/mcp
ordin setup smoke mcp-http
ordin setup launch mcp-http
```

The URL is a placeholder; use your intended endpoint. Preview/setup/smoke do not
contact it. `launch` explicitly starts the configured local proxy in the foreground.
The listener is loopback, fixed port 8766 by default (`--port` changes it).
URL credentials/query tokens and automatic authorization forwarding are refused.
Use reviewed inventory/semantics/lock flags as above when available; HTTP catalog
collection otherwise follows the [manual HTTP guide](mcp-http-proxy.md).

## Local smoke and setup benchmark

Hook smoke runs the generated POSIX launchers against synthetic start/read/post/end
events using temporary evidence directories. Shell smoke checks script syntax and
benign review. MCP stdio smoke exercises local gate/observation correlation; HTTP
smoke runs a real loopback fixture. Both validate the generated configuration and
reviewed pins, but neither contacts or certifies your actual upstream server.

The installed-wheel `scripts/check_quickstarts.py` acceptance gate measures two
commands per integration: setup and fixture smoke. Its JSON reports elapsed seconds
and separately verifies preview, idempotence, status, and removal. Package installation,
optional preview, host trust, and MCP human semantics review are excluded explicitly.
It runs in temporary directories without touching real host settings or adding
telemetry. Use [feedback intake](feedback-intake.md) stages to report remaining
installation, configuration, host-enablement, semantics-review, or smoke friction.

Advanced/manual references: [Claude](claude-code-integration.md),
[Codex hooks/plugin](codex-integration.md), [Cursor](cursor-integration.md),
[MCP stdio](mcp-safety-proxy.md), [MCP HTTP](mcp-http-proxy.md), and
[Bash/Zsh](shell-integration.md).
