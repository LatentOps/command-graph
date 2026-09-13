from dataclasses import replace
import json
import os
from pathlib import Path
import sys

import pytest

from ordin.entrypoint import main as ordin_main
from ordin.mcp_contracts import MCPContractLock, semantics_binding_digest, tool_contract_digest
from ordin.setup import TARGETS, SetupError, apply, config_content, load_owned, plan, remove, status
from ordin.setup_cli import main, smoke
from ordin.tool_calls import ToolSemanticRule, ToolSemanticsRegistry


def settings(integration, **overrides):
    return {
        "integration": integration,
        "config": TARGETS[integration],
        "python": sys.executable,
        "state": False,
        "audit": False,
        "observations": False,
        "shell": "bash",
        "server_id": "fixture" if integration.startswith("mcp") else None,
        "command": [sys.executable, "-m", "fixture_server"] if integration == "mcp" else [],
        "upstream": "http://127.0.0.1:9999/mcp" if integration == "mcp-http" else None,
        "port": 8766,
        "semantics": None,
        "inventory": None,
        "contract_lock": None,
        **overrides,
    }


@pytest.mark.parametrize("integration", TARGETS)
def test_preview_apply_idempotence_smoke_and_removal_preserve_unrelated_files(
    tmp_path, integration
):
    root = tmp_path.resolve() / "workspace"
    root.mkdir()
    unrelated = root / "unrelated.json"
    unrelated.write_bytes(b'{ "keep": true }\n')
    options = settings(integration)
    preview = plan(options, root)
    assert preview["ok"] and all(change["operation"] == "create" for change in preview["changes"])
    assert not (root / ".ordin").exists()
    result = apply(options, root)
    assert not result["idempotent"]
    assert apply(options, root)["idempotent"]
    assert load_owned(root, integration) == options
    assert status(root, integration)["configured"]
    assert smoke(root, integration)["ok"]
    assert remove(root, integration, dry_run=True)["would_remove"]
    assert (root / TARGETS[integration]).exists()
    assert remove(root, integration)["ok"]
    assert not (root / TARGETS[integration]).exists()
    assert unrelated.read_bytes() == b'{ "keep": true }\n'


@pytest.mark.parametrize("integration", TARGETS)
def test_existing_configuration_and_modified_owned_files_are_never_overwritten(
    tmp_path, integration
):
    root = tmp_path.resolve()
    options = settings(integration)
    target = root / TARGETS[integration]
    target.parent.mkdir(parents=True, exist_ok=True)
    original = b'{"private-user-value": "leave untouched"}\n'
    target.write_bytes(original)
    assert not plan(options, root)["ok"]
    with pytest.raises(SetupError):
        apply(options, root)
    assert target.read_bytes() == original
    target.unlink()
    apply(options, root)
    target.write_bytes(original)
    with pytest.raises(SetupError):
        remove(root, integration)
    assert target.read_bytes() == original
    assert "leave untouched" not in json.dumps(status(root, integration))


def test_partial_write_rolls_back_only_created_files(tmp_path, monkeypatch):
    import ordin.setup as setup

    root = tmp_path.resolve()
    original = setup._write_new

    def failing(path, content, active_root):
        if path.name == "hooks.json":
            raise OSError("fixture disk full")
        return original(path, content, active_root)

    monkeypatch.setattr(setup, "_write_new", failing)
    with pytest.raises(OSError):
        apply(settings("cursor"), root)
    assert not list(root.rglob("*.json"))
    assert not list(root.rglob(".ordin-new-*"))


def test_interrupted_setup_can_complete_without_overwriting_an_owned_file(tmp_path):
    root = tmp_path.resolve()
    options = settings("cursor")
    apply(options, root)
    (root / TARGETS["cursor"]).unlink()
    assert not status(root, "cursor")["configured"]
    assert apply(options, root)["applied"] == [str(root / TARGETS["cursor"])]
    assert status(root, "cursor")["configured"]


def test_lock_and_escaping_paths_refuse_mutation(tmp_path):
    root = tmp_path.resolve()
    with pytest.raises(SetupError):
        plan(settings("cursor", config="../outside.json"), root)
    with pytest.raises(SetupError):
        plan(settings("cursor", config=".ordin/setup/cursor.json"), root)
    with pytest.raises(SetupError):
        plan(settings("cursor", config=".ordin/private/cursor/state.db"), root)
    apply(settings("cursor"), root)
    lock = root / ".ordin/setup/.lock"
    lock.write_text("pending operator review")
    with pytest.raises(SetupError, match="locked"):
        apply(settings("cursor"), root)
    assert not status(root, "cursor")["configured"]
    assert lock.read_text() == "pending operator review"


@pytest.mark.skipif(os.name != "posix", reason="POSIX ownership and hooks")
def test_symlink_and_insecure_private_state_refusal(tmp_path):
    root = tmp_path.resolve()
    (root / ".cursor").symlink_to(root, target_is_directory=True)
    with pytest.raises(SetupError, match="symlink"):
        plan(settings("cursor"), root)
    (root / ".cursor").unlink()
    options = settings("cursor", state=True, observations=True, audit=True)
    apply(options, root)
    assert smoke(root, "cursor")["ok"]
    directory = root / ".ordin/private/cursor"
    directory.chmod(0o755)
    assert status(root, "cursor")["code"] == "private_path_permissions_required"


@pytest.mark.parametrize("integration", ("claude", "codex", "cursor"))
def test_opt_in_evidence_and_launcher_configuration_are_explicit(tmp_path, integration):
    root = tmp_path.resolve()
    options = settings(integration, state=True, audit=True, observations=True)
    apply(options, root)
    config = config_content(options, root).decode()
    assert "|| exit 2" in config and "_TRACE_RAW=0" in config
    assert smoke(root, integration)["ok"]
    # The smoke process uses its own private directory, never the user's history.
    assert not list((root / ".ordin/private" / integration).iterdir())


def test_mcp_setup_requires_reviewed_pins_and_keeps_unknowns_untrusted(tmp_path):
    root = tmp_path.resolve()
    options = settings("mcp")
    assert plan(options, root)["mcp_review_required"]
    tool = {"name": "read", "inputSchema": {"type": "object"}}
    registry = ToolSemanticsRegistry(
        "setup",
        "1",
        (
            ToolSemanticRule(
                id="read", kind="mcp", server="fixture", tool="read", effects=("filesystem.read",)
            ),
        ),
    )
    inventory = {
        "schema_version": "ordin.mcp_inventory.v1",
        "server_id": "fixture",
        "protocol_revision": "2025-11-25",
        "tools": [tool],
    }
    lock = MCPContractLock(
        semantics_binding_digest(registry, frozenset()),
        {("fixture", "read"): tool_contract_digest(tool)},
    )
    for name, data in (
        ("inventory", inventory),
        ("semantics", registry.as_dict()),
        ("contract_lock", lock.as_dict()),
    ):
        path = root / (name + ".json")
        path.write_text(json.dumps(data))
        options[name] = str(path)
    assert not plan(options, root)["mcp_review_required"]
    apply(options, root)
    changed = replace(lock, semantics_digest="0" * 64)
    Path(options["contract_lock"]).write_text(json.dumps(changed.as_dict()))
    assert not status(root, "mcp")["configured"]


@pytest.mark.parametrize(
    "change",
    [
        {"command": ["server", "--token", "private"]},
        {"command": ["TOKEN=private", "server"]},
        {"command": ["server", "https://private.invalid"]},
    ],
)
def test_credentials_are_not_copied_into_mcp_configuration(tmp_path, change):
    with pytest.raises(SetupError):
        plan(settings("mcp", **change), tmp_path.resolve())


def test_main_cli_discovers_setup_and_machine_readable_preview(tmp_path, capsys):
    assert (
        ordin_main(["setup", "cursor", "--root", str(tmp_path.resolve()), "--dry-run", "--json"])
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["ok"] and not (tmp_path / ".ordin").exists()
    assert main(["status", "--root", str(tmp_path.resolve()), "--json"]) == 0
    assert len(json.loads(capsys.readouterr().out)["integrations"]) == 6
