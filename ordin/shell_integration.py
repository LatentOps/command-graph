from __future__ import annotations


SUPPORTED_SHELLS = ("bash", "zsh")

_BASH_INIT = r"""# Ordin interactive integration for Bash.
# Source with: source <(ordin shell-init bash)

__ordin_shell_threshold() {
  case "${ORDIN_SHELL_FAIL_ON:-warn}" in
    warn|ask|block)
      printf '%s' "${ORDIN_SHELL_FAIL_ON:-warn}"
      ;;
    *)
      printf '%s\n' 'Ordin: ORDIN_SHELL_FAIL_ON must be warn, ask, or block.' >&2
      return 2
      ;;
  esac
}

orun() {
  if (( $# != 1 )); then
    printf '%s\n' "Usage: orun 'command text'" >&2
    return 2
  fi

  local command_text="$1"
  if [[ -z "${command_text//[[:space:]]/}" ]]; then
    printf '%s\n' 'Ordin: refusing an empty command.' >&2
    return 2
  fi

  local threshold output review_status
  threshold="$(__ordin_shell_threshold)" || return $?

  local -a review_args
  review_args=(
    check "$command_text"
    --cwd "$PWD"
    --shell bash
    --interactive
    --fail-on "$threshold"
  )
  if [[ -n "${EUID+x}" ]]; then
    review_args+=(--euid "$EUID")
  fi
  if [[ -n "${ORDIN_REPO_ROOT:-}" ]]; then
    review_args+=(--repo-root "$ORDIN_REPO_ROOT")
  fi

  output="$(command ordin "${review_args[@]}" 2>&1)"
  review_status=$?

  if [[ "$output" != decision:\ allow* ]]; then
    printf '%s\n' "$output" >&2
  fi
  if (( review_status != 0 )); then
    return "$review_status"
  fi

  # Deliberately use a child shell rather than eval. Stateful commands such as
  # cd/export affect only the child shell; use normal shell execution for those.
  command bash -c "$command_text" ordin-shell
}

ordin_shell_disable() {
  unset -f orun 2>/dev/null || true
  unset -f __ordin_shell_threshold 2>/dev/null || true
  unset -f ordin_shell_disable 2>/dev/null || true
}
"""

_ZSH_INIT = r"""# Ordin interactive integration for Zsh.
# Source with: source <(ordin shell-init zsh)
# Ctrl-X Ctrl-G reviews the current ZLE buffer and accepts it only when allowed.

__ordin_shell_threshold() {
  case "${ORDIN_SHELL_FAIL_ON:-warn}" in
    warn|ask|block)
      print -rn -- "${ORDIN_SHELL_FAIL_ON:-warn}"
      ;;
    *)
      print -ru2 -- 'Ordin: ORDIN_SHELL_FAIL_ON must be warn, ask, or block.'
      return 2
      ;;
  esac
}

__ordin_review_args() {
  local command_text="$1"
  local threshold="$2"
  local -a args
  args=(
    check "$command_text"
    --cwd "$PWD"
    --shell zsh
    --interactive
    --fail-on "$threshold"
  )
  if [[ -n "${EUID+x}" ]]; then
    args+=(--euid "$EUID")
  fi
  if [[ -n "${ORDIN_REPO_ROOT:-}" ]]; then
    args+=(--repo-root "$ORDIN_REPO_ROOT")
  fi
  print -rN -- "${args[@]}"
}

orun() {
  if (( $# != 1 )); then
    print -ru2 -- "Usage: orun 'command text'"
    return 2
  fi

  local command_text="$1"
  if [[ -z "${command_text//[[:space:]]/}" ]]; then
    print -ru2 -- 'Ordin: refusing an empty command.'
    return 2
  fi

  local threshold output review_status
  threshold="$(__ordin_shell_threshold)" || return $?

  local -a review_args
  review_args=(
    check "$command_text"
    --cwd "$PWD"
    --shell zsh
    --interactive
    --fail-on "$threshold"
  )
  if [[ -n "${EUID+x}" ]]; then
    review_args+=(--euid "$EUID")
  fi
  if [[ -n "${ORDIN_REPO_ROOT:-}" ]]; then
    review_args+=(--repo-root "$ORDIN_REPO_ROOT")
  fi

  output="$(command ordin "${review_args[@]}" 2>&1)"
  review_status=$?

  if [[ "$output" != decision:\ allow* ]]; then
    print -ru2 -- "$output"
  fi
  if (( review_status != 0 )); then
    return "$review_status"
  fi

  command zsh -c "$command_text" ordin-shell
}

__ordin_zle_review_accept() {
  local command_text="$BUFFER"
  if [[ -z "${command_text//[[:space:]]/}" ]]; then
    zle .accept-line
    return
  fi

  local threshold output review_status
  threshold="$(__ordin_shell_threshold)" || {
    zle -M 'Ordin: invalid enforcement threshold'
    return
  }

  local -a review_args
  review_args=(
    check "$command_text"
    --cwd "$PWD"
    --shell zsh
    --interactive
    --fail-on "$threshold"
  )
  if [[ -n "${EUID+x}" ]]; then
    review_args+=(--euid "$EUID")
  fi
  if [[ -n "${ORDIN_REPO_ROOT:-}" ]]; then
    review_args+=(--repo-root "$ORDIN_REPO_ROOT")
  fi

  output="$(command ordin "${review_args[@]}" 2>&1)"
  review_status=$?
  if (( review_status == 0 )); then
    if [[ "$output" != decision:\ allow* ]]; then
      zle -I
      print -r -- "$output"
    fi
    zle .accept-line
    return
  fi

  zle -I
  print -r -- "$output"
  zle reset-prompt
}

ordin_shell_enable_zle() {
  if [[ -o interactive ]]; then
    zle -N ordin-review-accept __ordin_zle_review_accept
    bindkey '^X^G' ordin-review-accept
  fi
}

ordin_shell_disable() {
  if [[ -o interactive ]]; then
    bindkey -r '^X^G' 2>/dev/null || true
    zle -D ordin-review-accept 2>/dev/null || true
  fi
  unfunction orun 2>/dev/null || true
  unfunction __ordin_shell_threshold 2>/dev/null || true
  unfunction __ordin_zle_review_accept 2>/dev/null || true
  unfunction ordin_shell_enable_zle 2>/dev/null || true
  unfunction ordin_shell_disable 2>/dev/null || true
}

ordin_shell_enable_zle
"""


def render_shell_init(shell: str) -> str:
    normalized = shell.strip().lower()
    if normalized == "bash":
        return _BASH_INIT
    if normalized == "zsh":
        return _ZSH_INIT
    raise ValueError(f"unsupported shell {shell!r}; choose one of: {', '.join(SUPPORTED_SHELLS)}")
