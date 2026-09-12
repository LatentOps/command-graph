import copy
import json
import asyncio
import sys
from pathlib import Path

import pytest

from ordin import AgentGate, Ordin, ReviewPolicy
from ordin.contracts_cli import main
from ordin.mcp_contracts import (
    MCPContractCheck,
    MCPContractLock,
    MCPContractObserver,
    load_contract_json,
    semantics_binding_digest,
    tool_contract_digest,
)
from ordin.mcp_proxy import (
    APPROVAL_REQUIRED_CODE,
    BLOCKED_CODE,
    MCPStdioSafetyProxy,
    _parse_jsonrpc_line,
)
from ordin.tool_calls import ToolResourceBinding, ToolSemanticRule, ToolSemanticsRegistry


def _tool():
    return {
        "name": "read_file",
        "description": "Read a file",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": True},
    }


def _semantics():
    return ToolSemanticsRegistry(
        "contract-test",
        "1",
        (
            ToolSemanticRule(
                id="read",
                kind="mcp",
                server="workspace",
                tool="read_file",
                effects=("filesystem.read",),
                resources=(ToolResourceBinding(argument="path", type="path"),),
            ),
        ),
    )


def _proxy(*, fail_on="warn", shell=False, server="workspace", audit=None):
    semantics = _semantics()
    shells = frozenset({"shell"}) if shell else frozenset()
    lock = MCPContractLock(
        semantics_binding_digest(semantics, shells),
        {("workspace", "read_file"): tool_contract_digest(_tool())},
    )
    return MCPStdioSafetyProxy(
        server_id=server,
        gate=AgentGate(
            Ordin(tool_semantics=semantics, policy=ReviewPolicy(fail_on=fail_on), audit=audit)
        ),
        shell_tools=shells,
        contract_lock=lock,
    )


def _discover(proxy, tools=None, request_id=100, cursor=None, next_cursor=None):
    params = {"cursor": cursor} if cursor else {}
    request = {"jsonrpc": "2.0", "id": request_id, "method": "tools/list", "params": params}
    assert proxy.process_client_message(request).forward
    result = {"tools": [_tool()] if tools is None else tools}
    if next_cursor:
        result["nextCursor"] = next_cursor
    proxy.observe_server_message({"jsonrpc": "2.0", "id": request_id, "result": result})


def _call(request_id=1, name="read_file", arguments=None):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments or {"path": "/workspace/README.md"}},
    }


def test_unchanged_contract_allows_and_changed_contract_requires_approval():
    proxy = _proxy()
    assert not proxy.process_client_message(_call()).forward
    _discover(proxy)
    allowed = proxy.process_client_message(_call(2))
    assert allowed.forward
    proxy.observe_server_message({"jsonrpc": "2.0", "id": 2, "result": {}})
    changed = _tool()
    changed["inputSchema"]["properties"]["path"]["type"] = "integer"
    _discover(proxy, [changed], request_id=101)
    rejected = proxy.process_client_message(_call(3))
    assert not rejected.forward
    assert rejected.response["error"]["code"] == APPROVAL_REQUIRED_CODE
    diagnostic = rejected.response["error"]["data"]["ordin"]["contract"]
    assert diagnostic["status"] == "changed"
    assert diagnostic["expected_digest"] != diagnostic["observed_digest"]
    assert "/workspace/README.md" not in json.dumps(diagnostic)


@pytest.mark.parametrize(
    "mutation", ["optional", "required", "type", "output", "annotation", "execution"]
)
def test_execution_contract_changes_invalidate_pins(mutation):
    original = _tool()
    changed = copy.deepcopy(original)
    if mutation in {"optional", "required"}:
        changed["inputSchema"]["properties"]["extra"] = {"type": "string"}
        if mutation == "required":
            changed["inputSchema"]["required"].append("extra")
    elif mutation == "type":
        changed["inputSchema"]["properties"]["path"]["type"] = "integer"
    elif mutation == "output":
        changed["outputSchema"] = {"type": "object"}
    elif mutation == "annotation":
        changed["annotations"]["readOnlyHint"] = False
    else:
        changed["execution"] = {"taskSupport": "required"}
    assert tool_contract_digest(original) != tool_contract_digest(changed)


def test_order_and_prose_do_not_create_false_drift_but_property_names_do():
    first = _tool()
    second = dict(reversed(list(first.items())))
    second["description"] = "This text grants no permissions"
    second["title"] = "A display title"
    second["inputSchema"] = copy.deepcopy(first["inputSchema"])
    second["inputSchema"]["properties"]["path"]["description"] = "A different description"
    assert tool_contract_digest(first) == tool_contract_digest(second)
    second["inputSchema"]["properties"]["description"] = {"type": "string"}
    assert tool_contract_digest(first) != tool_contract_digest(second)


def test_integral_numbers_have_one_canonical_representation():
    first = _tool()
    first["inputSchema"]["properties"]["path"]["maxLength"] = 10
    second = copy.deepcopy(first)
    second["inputSchema"]["properties"]["path"]["maxLength"] = 10.0
    assert tool_contract_digest(first) == tool_contract_digest(second)


@pytest.mark.parametrize(
    "schema",
    [
        None,
        [],
        {"type": "object", "properties": []},
        {"type": "object", "required": "path"},
        {"type": "object", "minLength": "one"},
        {"type": "object", "$ref": "https://example.com/schema"},
        {"type": "object", "$ref": "#/$defs/missing"},
    ],
)
def test_malformed_or_unverifiable_schema_fails_closed(schema):
    proxy = _proxy()
    invalid = {**_tool(), "inputSchema": schema}
    _discover(proxy, [invalid])
    result = proxy.process_client_message(_call())
    assert not result.forward
    assert result.response["error"]["data"]["ordin"]["contract"]["status"] == "invalid"


def test_deep_and_duplicate_tool_contracts_fail_closed():
    proxy = _proxy()
    _discover(proxy, [_tool(), _tool()])
    assert not proxy.process_client_message(_call()).forward
    nested = {"type": "object"}
    for _ in range(40):
        nested = {"type": "object", "properties": {"value": nested}}
    with pytest.raises(ValueError, match="nesting"):
        tool_contract_digest({**_tool(), "inputSchema": nested})


def test_renamed_tool_or_server_cannot_inherit_a_pin():
    proxy = _proxy(server="other")
    _discover(proxy)
    assert (
        proxy.process_client_message(_call()).response["error"]["data"]["ordin"]["contract"][
            "status"
        ]
        == "unpinned"
    )
    proxy = _proxy()
    _discover(proxy, [{**_tool(), "name": "renamed"}])
    assert not proxy.process_client_message(_call(name="renamed")).forward
    assert (
        proxy.process_client_message(_call(2)).response["error"]["data"]["ordin"]["contract"][
            "status"
        ]
        == "missing"
    )


def test_permissive_policy_cannot_bypass_missing_pin_and_core_block_stays_blocked():
    proxy = _proxy(fail_on="block", shell=True)
    assert not proxy.process_client_message(_call()).forward
    blocked = proxy.process_client_message(_call(2, "shell", {"command": "rm -rf /"}))
    assert blocked.response["error"]["code"] == BLOCKED_CODE
    action = proxy.adapter.adapt("read_file", {"path": "README.md"}, action_id="direct")
    review = proxy.gate.ordin.review_action(
        action, contract_check=proxy.contracts.check("read_file")
    )
    assert not proxy.gate.ordin.allows(review)


def test_partial_inventory_and_list_changed_require_new_complete_discovery():
    proxy = _proxy()
    _discover(proxy, next_cursor="page-2")
    assert not proxy.process_client_message(_call()).forward
    _discover(proxy, [], request_id=101, cursor="page-2")
    assert proxy.process_client_message(_call(2)).forward
    proxy.observe_server_message({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
    assert not proxy.process_client_message(_call(3)).forward


def test_unrequested_discovery_cannot_establish_trust_and_ids_cannot_cross_correlate():
    proxy = _proxy()
    proxy.observe_server_message({"jsonrpc": "2.0", "id": 50, "result": {"tools": [_tool()]}})
    assert not proxy.process_client_message(_call()).forward
    request = {"jsonrpc": "2.0", "id": 100, "method": "tools/list"}
    assert proxy.process_client_message(request).forward
    assert not proxy.process_client_message(_call(100)).forward
    proxy.observe_server_message(
        {"jsonrpc": "2.0", "id": 100, "method": "server/request", "params": {}}
    )
    assert not proxy.process_client_message(_call(2)).forward
    proxy.observe_server_message({"jsonrpc": "2.0", "id": 100, "result": {"tools": [_tool()]}})
    assert proxy.process_client_message(_call(3)).forward


def test_stale_semantics_binding_cannot_allow_call():
    lock = MCPContractLock("a" * 64, {("workspace", "read_file"): tool_contract_digest(_tool())})
    observer = MCPContractObserver("workspace", lock, "b" * 64)
    observer.request(1, {})
    observer.response(1, {"result": {"tools": [_tool()]}})
    assert observer.check("read_file").status == "semantics_changed"


def test_strict_parser_rejects_duplicate_and_rounded_contract_numbers(tmp_path):
    path = tmp_path / "lock.json"
    path.write_text('{"pins":[],"pins":[]}')
    with pytest.raises(ValueError, match="duplicate"):
        load_contract_json(path)
    for value in (b"1e-999", b"0.1234567890123456789012345"):
        with pytest.raises(ValueError, match="precision"):
            _parse_jsonrpc_line(b'{"value":' + value + b"}")


def test_final_audit_records_ask_for_missing_contract():
    class Audit:
        def __init__(self):
            self.reviews = []

        def record(self, review):
            self.reviews.append(review)

    audit = Audit()
    proxy = _proxy(audit=audit)
    assert not proxy.process_client_message(_call()).forward
    assert len(audit.reviews) == 1
    assert audit.reviews[0].decision == "ask"
    assert audit.reviews[0].provenance.final_decision == "ask"


def test_contract_cli_validates_and_diffs_without_network(tmp_path, capsys):
    semantics = _semantics()
    lock = MCPContractLock(
        semantics_binding_digest(semantics),
        {("workspace", "read_file"): tool_contract_digest(_tool())},
    )
    lock_path, inventory, rules = (
        tmp_path / name for name in ("lock.json", "tools.json", "semantics.json")
    )
    for path, payload in (
        (lock_path, lock.as_dict()),
        (inventory, {"tools": [_tool()]}),
        (rules, semantics.as_dict()),
    ):
        path.write_text(json.dumps(payload))
    assert main(["validate", str(lock_path), "--json"]) == 0
    assert (
        main(
            [
                "diff",
                str(lock_path),
                str(inventory),
                "--server-id",
                "workspace",
                "--semantics",
                str(rules),
                "--json",
            ]
        )
        == 0
    )
    inventory.write_text('{"tools":[]}')
    assert (
        main(
            [
                "diff",
                str(lock_path),
                str(inventory),
                "--server-id",
                "workspace",
                "--semantics",
                str(rules),
                "--json",
            ]
        )
        == 1
    )
    capsys.readouterr()


def test_verified_check_cannot_claim_mismatched_digests():
    with pytest.raises(ValueError, match="matching"):
        MCPContractCheck("workspace", "read_file", "matched", "a" * 64, "b" * 64)


def test_installed_module_proxy_enforces_pins_across_stdio_boundary():
    root = Path(__file__).resolve().parents[1]

    async def exercise():
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "ordin.mcp_proxy",
            "--server-id",
            "starter-kit",
            "--semantics",
            str(root / "examples/integrations/mcp-semantics.json"),
            "--contract-lock",
            str(root / "examples/integrations/mcp-contract-lock.json"),
            "--",
            sys.executable,
            str(root / "examples/integrations/fixture_mcp_server.py"),
            cwd=root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        async def request(payload):
            process.stdin.write((json.dumps(payload) + "\n").encode())
            await process.stdin.drain()
            return json.loads(await asyncio.wait_for(process.stdout.readline(), 10))

        try:
            missing = await request(_call(1, "read_note", {"path": "README.md"}))
            assert missing["error"]["code"] == APPROVAL_REQUIRED_CODE
            listed = await request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            assert listed["result"]["tools"][0]["name"] == "read_note"
            allowed = await request(_call(3, "read_note", {"path": "README.md"}))
            assert allowed["result"]["content"][0]["text"] == "fixture note"
            unknown = await request(_call(4, "other_tool"))
            assert unknown["error"]["code"] == APPROVAL_REQUIRED_CODE
            process.stdin.close()
            assert await asyncio.wait_for(process.wait(), 10) == 0
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(exercise())
