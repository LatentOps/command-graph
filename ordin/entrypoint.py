from __future__ import annotations

import sys
from typing import Sequence

from .cli import main as command_main


EXPLICIT_COMMANDS = {
    "setup",
    "trace",
    "mcp",
    "semantics",
    "contracts",
    "search",
    "explain",
    "graph",
    "packs",
    "shell-init",
    "check",
    "review",
    "action",
    "policy",
    "temporal",
    "doctor",
    "index",
}
SEARCH_FLAGS_WITH_VALUE = {"--limit"}
SEARCH_BOOLEAN_FLAGS = {"--json", "-h", "--help"}


def normalize_argv(argv: Sequence[str]) -> list[str]:
    """Map a bare intent query onto the explicit ``search`` subcommand.

    Explicit subcommands and top-level options are left untouched. Bare search
    flags may appear after the intent words. ``--`` forces every following token
    to be treated as literal query text.
    """

    args = list(argv)
    if not args:
        return args
    if args[0] in EXPLICIT_COMMANDS or args[0].startswith("-"):
        return args

    query_tokens: list[str] = []
    search_options: list[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            query_tokens.extend(args[index + 1 :])
            break
        if token in SEARCH_BOOLEAN_FLAGS:
            search_options.append(token)
            index += 1
            continue
        if token in SEARCH_FLAGS_WITH_VALUE:
            search_options.append(token)
            if index + 1 < len(args):
                search_options.append(args[index + 1])
                index += 2
            else:
                index += 1
            continue
        if token.startswith("--limit="):
            search_options.append(token)
            index += 1
            continue
        query_tokens.append(token)
        index += 1

    if not query_tokens:
        return args
    return ["search", " ".join(query_tokens), *search_options]


def main(argv: Sequence[str] | None = None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    if raw_args in (["-h"], ["--help"]):
        print("Default mode: type an intent directly, for example `ordin how to ssh`.\n")
    return command_main(normalize_argv(raw_args))
