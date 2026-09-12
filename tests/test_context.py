import pytest

from ordin.context import ExecutionContext, ReviewRequest
from ordin.analyzers import analyze_tokens
from ordin.shell import shell_tokens


def test_review_request_has_versioned_machine_contract():
    request = ReviewRequest(
        command="rm -rf ./build",
        intent="clean generated files",
        context=ExecutionContext(
            cwd="/repo",
            shell="bash",
            euid=1000,
            interactive=False,
            repo_root="/repo",
            agent="test-agent",
        ),
    )
    payload = request.as_dict()
    assert payload["schema_version"] == "ordin.review_request.v1"
    assert payload["context"]["cwd"] == "/repo"
    assert payload["context"]["interactive"] is False


def test_review_request_round_trips_and_keeps_missing_context_explicit():
    request = ReviewRequest.from_dict(
        {
            "schema_version": "ordin.review_request.v1",
            "command": "git status",
            "context": {"cwd": "/repo"},
        }
    )
    assert request.context is not None
    assert request.context.cwd == "/repo"
    assert request.context.euid is None
    assert request.as_dict()["context"]["euid"] is None


def test_review_request_rejects_unknown_schema():
    with pytest.raises(ValueError):
        ReviewRequest.from_dict(
            {
                "schema_version": "ordin.review_request.v999",
                "command": "git status",
            }
        )


def test_execution_context_resolves_paths_without_ambient_state():
    context = ExecutionContext(cwd="/repo/src", repo_root="/repo")
    assert context.resolve_path("../build") == "/repo/build"
    assert context.path_within_repo("/repo/build") is True
    assert context.path_within_repo("/tmp/build") is False
    assert ExecutionContext().resolve_path("./build") is None


def test_relative_path_has_unknown_repository_membership():
    context = ExecutionContext(cwd="/repo", repo_root="/repo")
    assert context.path_within_repo("relative/path") is None
    assert context.path_within_repo("") is None
    assert ExecutionContext(repo_root=".").path_within_repo("/repo/path") is None


def test_filesystem_analyzer_receives_context_and_resolves_target():
    analysis = analyze_tokens(
        shell_tokens("rm -rf ./build"),
        context=ExecutionContext(cwd="/repo"),
    )
    assert analysis is not None
    assert any(
        item.resource == "path:/repo/build"
        for item in analysis.evidence
        if item.effect == "filesystem.delete"
    )
