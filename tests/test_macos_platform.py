"""Explicit Darwin metadata and filesystem controls, also runnable on Linux."""

import os
from pathlib import Path

import pytest

from ordin import AgentGate
from ordin.action import ActionResource
from ordin.action_policy import PolicyResourceMatcher
from ordin.availability import command_availability, detect_environment
from ordin.context import ExecutionContext
from ordin.data import load_commands
from ordin.session import SessionIdentity, SqliteSessionStore


def test_darwin_does_not_inherit_linux_distribution_metadata(tmp_path):
    release = tmp_path / "os-release"
    release.write_text("ID=ubuntu\nID_LIKE=debian\n")
    environment = detect_environment(os_name="Darwin", os_release_path=release)
    assert environment.os == "darwin"
    assert environment.distro_id is None and environment.distro_like == ()


@pytest.mark.parametrize("command", ["apt", "apt-get", "ss", "fuser", "systemctl", "journalctl"])
def test_linux_command_cards_remain_incompatible_even_if_installed_on_macos(command):
    entry = next(entry for entry in load_commands() if entry["command"] == command)
    availability = command_availability(
        entry,
        environment=detect_environment(os_name="darwin"),
        which=lambda name: f"/opt/homebrew/bin/{name}",
    )
    assert availability.installed
    assert availability.platform_compatible is False
    assert availability.score_adjustment <= 0


def test_optional_tool_availability_is_independent_of_runtime_support():
    entries = {entry["command"]: entry for entry in load_commands()}
    for command in ["git", "aws", "kubectl", "terraform", "docker"]:
        present = command_availability(
            entries[command],
            environment=detect_environment(os_name="darwin"),
            which=lambda name: f"/opt/homebrew/bin/{name}",
        )
        absent = command_availability(
            entries[command],
            environment=detect_environment(os_name="darwin"),
            which=lambda name: None,
        )
        assert present.installed and not absent.installed
        assert present.platform_compatible is not False
        assert present.score_adjustment > absent.score_adjustment


def test_macos_context_keeps_alias_case_and_sibling_boundaries_distinct():
    context = ExecutionContext(
        cwd="/private/tmp/Repo/src", repo_root="/private/tmp/Repo", shell="zsh", euid=501
    )
    assert not context.is_elevated
    assert context.resolve_path("../out") == "/private/tmp/Repo/out"
    assert context.path_within_repo("/private/tmp/Repo/out")
    assert not context.path_within_repo("/tmp/Repo/out")
    assert not context.path_within_repo("/private/tmp/repo/out")
    assert not context.path_within_repo("/private/tmp/Repo-other/out")
    matcher = PolicyResourceMatcher(type="path", value="/private/tmp/Repo/out")
    assert matcher.matches(ActionResource("path", "/private/tmp/Repo/out"))
    assert not matcher.matches(ActionResource("path", "/tmp/Repo/out"))
    assert not matcher.matches(ActionResource("path", "/private/tmp/repo/out"))


@pytest.mark.skipif(os.name != "posix", reason="native POSIX mode and symlink contract")
def test_native_private_store_and_uid_in_temporary_directory(tmp_path):
    directory = tmp_path / "Private temporary directory"
    directory.mkdir(mode=0o700)
    target = directory / "sessions.db"
    identity = SessionIdentity("platform-control", "s")
    with SqliteSessionStore(target).transaction(identity, AgentGate(), create=True) as session:
        assert session.snapshot()["sequence"] == 0
    assert target.stat().st_mode & 0o777 == 0o600
    assert target.stat().st_uid == os.geteuid()
    alias = directory / "alias.db"
    alias.symlink_to(target)
    with pytest.raises((OSError, ValueError)):
        with SqliteSessionStore(alias).transaction(identity, AgentGate()):
            pass
    # Context matching remains lexical even for a real filesystem alias.
    root_alias = tmp_path / "alias-root"
    root_alias.symlink_to(directory, target_is_directory=True)
    context = ExecutionContext(cwd=str(directory), repo_root=str(directory))
    assert not context.path_within_repo(str(root_alias / "sessions.db"))
    assert Path(alias).resolve() == target.resolve()
