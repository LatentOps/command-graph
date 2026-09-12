# Platform and integration compatibility

Ordin's Python runtime and integration boundaries work on Linux and macOS.
Its detailed command knowledge remains Linux-first. Runtime support does not
mean that every suggested command or option exists on the local system.

## Maintained validation

| Platform | Python validation | Core/package | Agent and MCP boundaries | Shell execution |
| --- | --- | --- | --- | --- |
| Ubuntu GitHub runner | 3.10, 3.11, 3.12, 3.13 | Full tests, wheel install, quality and safety gates | Claude Code, Codex, MCP stdio and HTTP fixtures | Bash; Zsh tests run when available |
| Debian 12 | Distribution Python 3.11 | Installed package/CLI and doctor smoke tests | Supported; full boundary tests run on Ubuntu | POSIX Bash supported; no separate distro shell gate |
| Fedora 42 | Distribution Python 3.13 | Installed package/CLI and doctor smoke tests | Supported; full boundary tests run on Ubuntu | POSIX Bash supported; no separate distro shell gate |
| macOS 15, Apple Silicon | 3.13 | Full tests, wheel build/install and installed doctor | Claude Code, Codex, MCP stdio and HTTP, capture/persistence and conformance fixtures | System `/bin/bash` and `/bin/zsh` execution tests; ZLE decision boundary test |
| Native Windows | No maintained release gate | Not supported | Not supported | Not supported |

The Python package supports 3.10–3.13. All four versions have Linux CI;
macOS 3.10–3.12 and Intel Macs are not independently certified by this matrix.
Agent tests exercise the maintained hooks without requiring proprietary agent
binaries. They do not certify every host-agent version, approval UI, hosted
tool, credential configuration, or external MCP server.

The macOS job pins the `macos-15` runner label instead of following the moving
`macos-latest` label. The runner's architecture and supported labels are listed
in [GitHub's runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners).

## Command search and optional tooling

`EnvironmentInfo` reports macOS as `darwin` and does not read Linux distribution
metadata there. Search reports executable discovery separately from platform
compatibility. Finding an executable on `PATH` cannot override incompatible
command-card metadata; a missing executable does not mean Ordin cannot review
that command for a remote Linux environment.

| Knowledge or integration | macOS status |
| --- | --- |
| Core review, `AgentGate`, action and temporal policies | Tested |
| Claude Code and Codex hooks/plugin assets | Tested with local hook fixtures; host approval remains required |
| MCP stdio and Streamable HTTP | Tested with local protocol/transport fixtures |
| Audit, observations, session storage, trace capture | Tested with POSIX ownership/mode and linkage checks |
| Bash/Zsh `shell-init` and `orun` | Tested; explicit wrappers, no automatic interception or sandbox |
| Portable command forms such as `git status`, `cat`, `ls` | Supported with limitations: GNU/BSD option differences still matter |
| `apt`, `apt-get`, `ss`, Linux `fuser` socket syntax | Linux command cards; incompatible on macOS |
| `systemctl`, `journalctl` | Linux systemd command cards; incompatible on macOS |
| AWS, Azure, Google Cloud, Kubernetes, Terraform/OpenTofu, Docker, database packs | Conditional on installed CLIs, versions, remote service access and credentials |
| Optional semantic search/reranking dependencies | Not part of the macOS core gate; check those packages separately |

Cards without explicit OS metadata retain unknown compatibility. A recognized
command or effect is not a certificate of complete platform semantics. Ordin
does not install Homebrew packages, translate Linux options into BSD options,
or grant execution because a package manager is present.

## Filesystem and shell boundaries

Context and exact resource matching use supplied spellings. They do not fold
case, expand a home directory, resolve symlinks, or treat `/tmp` and
`/private/tmp` as interchangeable trusted roots. This deliberately avoids
broadening rules on case-insensitive volumes. Supply consistent absolute paths
from the execution host. A lexical match is not a filesystem authorization
check: the caller's sandbox must handle symlink traversal and changes between
review and execution. Resource `prefix` rules are literal string prefixes;
include a separator when a directory boundary is intended.

The integration passes the shell's actual effective UID, including macOS user
IDs such as 501. It does not assume Linux's common 1000 convention. A missing
UID remains unknown. Private local databases require the same owner-only
directory/file contract on Linux and macOS, including the native temporary
directory chosen by the OS.

`orun` executes an exact reviewed string in a child shell; changes to `cd`,
environment variables, or shell functions do not update the parent. Zsh's
Ctrl-X Ctrl-G widget reviews the current editor buffer and invokes ZLE's normal
accept action only when review succeeds. Tests cover allow/reject behavior at
that boundary, not an entire terminal emulator. Normal Enter remains the
shell's ordinary execution path. See [shell integration](shell-integration.md).

Only Bash and Zsh are accepted by `shell-init`; unsupported shell names fail
explicitly. Native Windows shell execution and Windows path-policy semantics
are outside this support contract.
