import json

import pytest

from ordin.entrypoint import main, normalize_argv


def test_normalize_bare_intent_to_search():
    assert normalize_argv(["how", "to", "ssh"]) == [
        "search",
        "how to ssh",
    ]


def test_normalize_bare_search_flags():
    assert normalize_argv(["make", "file", "runnable", "--json", "--limit", "2"]) == [
        "search",
        "make file runnable",
        "--json",
        "--limit",
        "2",
    ]


def test_explicit_subcommands_are_not_rewritten():
    assert normalize_argv(["trace", "inspect", "local.db"]) == ["trace", "inspect", "local.db"]
    assert normalize_argv(["check", "rm -rf /"]) == [
        "check",
        "rm -rf /",
    ]
    assert normalize_argv(["search", "make file runnable"]) == [
        "search",
        "make file runnable",
    ]


def test_double_dash_keeps_following_tokens_literal():
    assert normalize_argv(["find", "command", "--", "--json"]) == [
        "search",
        "find command --json",
    ]


def test_bare_query_executes_search(capsys):
    exit_code = main(["make", "file", "runnable", "--limit", "1", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload
    assert payload[0]["command"] == "chmod"


def test_bare_query_supports_multiword_unquoted_intent(capsys):
    exit_code = main(["lookup", "dns", "for", "domain", "example.com", "--limit=1", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 0
    assert payload[0]["command"] == "dig"


def test_explicit_check_keeps_safety_behavior(capsys):
    exit_code = main(["check", "rm -rf /", "--json", "--enforce"])
    payload = json.loads(capsys.readouterr().out)

    assert exit_code == 30
    assert payload["decision"] == "block"


def test_top_level_help_mentions_default_mode(capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])

    output = capsys.readouterr().out
    assert exc_info.value.code == 0
    assert "Default mode: type an intent directly" in output
    assert "ordin how to ssh" in output
