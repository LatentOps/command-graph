# Interactive shell integration

Ordin's shell integration is explicit, local, and reversible. It does
not upload command history or automatically install itself into shell startup
files.

Bash and Zsh execution are tested on macOS as well as the Linux coverage listed
in the [compatibility matrix](compatibility.md). Zsh helpers use ordinary local
names for review exit codes because [`status` is a read-only Zsh parameter](https://zsh.sourceforge.io/Doc/Release/Parameters.html).

## Bash

Load the integration for the current shell:

```bash
source <(ordin shell-init bash)
```

Then run exact shell text through the reviewed wrapper:

```bash
orun 'git status --short'
orun 'rm -rf ./build'
orun $'printf "one\\n"\nprintf "two\\n"'
```

`orun` sends the exact string to `ordin check` with explicit context from
the shell (`PWD`, shell name, `EUID`, interactive mode, and optionally
`ORDIN_REPO_ROOT`). It executes the string in a child Bash process only
when the enforcement threshold allows it. The wrapper never uses `eval`.

Because execution happens in a child shell, stateful commands such as `cd`,
`export`, shell functions, and aliases do not modify the parent Bash session.
Review those with `ordin check` and run them normally if appropriate.

Disable the sourced functions with:

```bash
ordin_shell_disable
```

## Zsh

Load the integration:

```zsh
source <(ordin shell-init zsh)
```

Zsh gets the same `orun 'command text'` wrapper plus a ZLE widget bound to
**Ctrl-X Ctrl-G**. Type a command into the normal Zsh prompt and press that key
sequence to review the current `BUFFER`. If the review passes, the widget calls
the original ZLE `accept-line`, so `cd`, exports, aliases, functions, pipelines,
redirections, quoting, and multiline buffers retain normal current-shell
semantics. A blocked/warned/uncertain command remains in the buffer for editing.

The integration deliberately does not replace Enter, so sourcing it does not
silently change the user's normal execution key. Remove the widget and helper
functions with:

```zsh
ordin_shell_disable
```

## Decision behavior

The default shell threshold is `warn`, which means only `allow` is executed
automatically through the integration. `warn`, `ask`, and `block` are printed
and not executed.

Advanced users can relax the threshold explicitly:

```bash
export ORDIN_SHELL_FAIL_ON=ask
# warnings may execute; ask/block still stop

export ORDIN_SHELL_FAIL_ON=block
# warn/ask may execute; only block stops
```

Only `warn`, `ask`, and `block` are accepted. An invalid value prevents the
wrapper/widget from executing the command.

If a repository boundary is known, provide it explicitly:

```bash
export ORDIN_REPO_ROOT=/path/to/repo
```

This enables the context-aware outside-repository mutation checks from the
review engine.

## Safety and privacy properties

- no shell startup file is modified automatically;
- no command or shell history is sent to a remote service;
- the Bash/Zsh wrapper does not use `eval`;
- multiline and quoted command text is reviewed as one exact string;
- Zsh's review key preserves the existing line editor and current-shell
  execution semantics;
- the core `ordin check` and `review` commands remain non-executing.
