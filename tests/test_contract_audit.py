import json
import sys
from pathlib import Path

import pytest

import ordin
from ordin import (
    ActionEnvelope,
    ActionHistory,
    ActionObservation,
    ActionTrace,
    AgentGate,
    ExecutionContext,
    ObservationHistory,
    Ordin,
    ReviewRequest,
    TraceAction,
)
from ordin._json_contracts import load_configuration
from ordin.action_policy import ActionPolicyCondition, ActionPolicySet, load_action_policy
from ordin.audit import build_audit_event
from ordin.claude_code import main as claude_main
from ordin.data import DATA_DIR, load_commands, load_json
from ordin.graph import build_effect_graph
from ordin.mcp_contracts import MCPContractLock
from ordin.packs import pack_list_payload
from ordin.schema import SCHEMA_FILES, validate_instance, validate_named_schema
from ordin.session import IntegrationSession, SessionIdentity
from ordin.temporal import default_temporal_policy, load_temporal_policy
from ordin.tool_calls import ToolSemanticsRegistry, load_tool_semantics
from ordin.trace_capture import TraceRecorder, read_capture


ROOT = Path(__file__).resolve().parents[1]


def test_every_registered_schema_has_a_canonical_runtime_or_data_example(tmp_path):
    gate = Ordin()
    action = ActionEnvelope.shell("git status --short", action_id="contract-action")
    review = gate.review_action(action)
    recorder = TraceRecorder(tmp_path / "trace.db", integration="python", session_id="contract")
    recorder.record_review(review)
    event = read_capture(recorder.path)["events"][0]
    capture_case = json.loads(
        (ROOT / "benchmarks/captured_conformance.jsonl").read_text().splitlines()[0]
    )
    candidate = {
        **capture_case["capture_provenance"],
        "case": {
            key: value
            for key, value in capture_case.items()
            if key not in {"capture_provenance", "capture_integration", "capture_policy"}
        },
    }
    samples = {
        "action_trace": ActionTrace((TraceAction("git status"),)).as_dict(),
        "action_envelope": action.as_dict(),
        "action_history": ActionHistory((action,)).as_dict(),
        "action_review": review.as_dict(),
        "action_observation": ActionObservation("contract-action", exit_code=0).as_dict(),
        "observation_history": ObservationHistory().as_dict(),
        "execution_capabilities": review.capabilities.as_dict(),
        "provenance": review.provenance.as_dict(),
        "audit_event": build_audit_event(review).as_dict(),
        "review_request": ReviewRequest("git status").as_dict(),
        "review_result": gate.review("git status").as_dict(),
        "risk_review": gate.check("git status").as_dict(),
        "search_result": gate.search("make file runnable", limit=1)[0].as_dict(),
        "policy_set": ActionPolicySet("contract", "1", ()).as_dict(),
        "temporal_policy_set": default_temporal_policy().policy.as_dict(),
        "tool_semantics": ToolSemanticsRegistry("contract", "1", ()).as_dict(),
        "integration_session": IntegrationSession(
            SessionIdentity("contract", "s"), AgentGate()
        ).snapshot(),
        "mcp_contract_lock": MCPContractLock("a" * 64, {("server", "tool"): "b" * 64}).as_dict(),
        "mcp_inventory": {
            "schema_version": "ordin.mcp_inventory.v1",
            "server_id": "fixture",
            "protocol_revision": "2025-11-25",
            "tools": [],
        },
        "codex_mcp_map": json.loads((ROOT / "examples/codex-mcp-map.json").read_text()),
        "trace_event": {
            key: value
            for key, value in event.items()
            if key not in {"sequence", "session_key", "action_key"}
        },
        "trace_candidate": candidate,
        "command_card": load_commands()[0],
        "risk_rules": load_json(DATA_DIR / "risk_rules.json"),
        "effect_catalog": load_json(DATA_DIR / "effects.json"),
        "effect_graph": build_effect_graph().as_dict(),
        "command_pack": load_json(DATA_DIR / "packs/git/pack.json"),
        "pack_list": pack_list_payload(),
    }
    assert set(samples) == set(SCHEMA_FILES)
    for name, payload in samples.items():
        assert not validate_named_schema(name, payload), name
        json.dumps(payload, allow_nan=False)
    readers = {
        "action_trace": ActionTrace,
        "action_envelope": ActionEnvelope,
        "action_history": ActionHistory,
        "action_observation": ActionObservation,
        "observation_history": ObservationHistory,
        "review_request": ReviewRequest,
        "policy_set": ActionPolicySet,
        "tool_semantics": ToolSemanticsRegistry,
        "mcp_contract_lock": MCPContractLock,
        "temporal_policy_set": ordin.TemporalPolicySet,
        "execution_capabilities": ordin.ExecutionCapabilityProfile,
    }
    for name, reader in readers.items():
        assert reader.from_dict(samples[name]).as_dict() == samples[name]
    restored = IntegrationSession.restore(
        SessionIdentity("contract", "s"), AgentGate(), samples["integration_session"]
    )
    assert restored.snapshot() == samples["integration_session"]


def test_validator_enforces_all_declared_numeric_and_uniqueness_bounds():
    assert validate_instance(33, {"type": "integer", "maximum": 32})
    assert not validate_instance(32, {"type": "integer", "maximum": 32})
    assert validate_instance(["a", "a"], {"type": "array", "uniqueItems": True})
    assert validate_instance([1, 1.0], {"type": "array", "uniqueItems": True})
    assert not validate_instance([1, True], {"type": "array", "uniqueItems": True})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_typed_json_contracts_reject_nonfinite_payloads(value):
    with pytest.raises(ValueError, match="finite"):
        ActionEnvelope("tool", "call", parameters={"nested": [value]})
    with pytest.raises(ValueError, match="finite"):
        ActionObservation("a", metadata={"nested": [value]})


def test_typed_context_trace_and_policy_constructors_match_wire_bounds():
    for euid in (-1, True, 1.5):
        with pytest.raises(ValueError):
            ExecutionContext(euid=euid)
    with pytest.raises(ValueError):
        ActionTrace(tuple(TraceAction("git status") for _ in range(33)))
    with pytest.raises(ValueError):
        TraceAction(" ")
    with pytest.raises(ValueError):
        ActionPolicyCondition(kinds=tuple("shell" for _ in range(129)))
    with pytest.raises(ValueError):
        ActionPolicyCondition(privileged="false")


@pytest.mark.parametrize("loader", [load_action_policy, load_temporal_policy, load_tool_semantics])
def test_configuration_parsers_reject_duplicate_members_before_schema_validation(tmp_path, loader):
    path = tmp_path / "configuration.json"
    path.write_text('{"rules":[],"rules":[]}')
    with pytest.raises(ValueError, match="duplicate"):
        loader(path)


def test_configuration_reader_bounds_and_finite_numbers(tmp_path):
    path = tmp_path / "configuration.json"
    path.write_text('{"value":NaN}')
    with pytest.raises(ValueError, match="finite"):
        load_configuration(path, label="fixture", maximum=1024)
    with pytest.raises(ValueError, match="maximum size"):
        load_configuration(path, label="fixture", maximum=2)


def test_input_builders_preserve_v02_defaults_but_reject_unknown_members():
    action = ActionEnvelope.from_dict(
        {"kind": "shell", "operation": "execute", "parameters": {"command": "git status"}}
    )
    assert Ordin().review_action(action).allowed
    request = ReviewRequest.from_dict({"command": "git status"})
    assert Ordin().review_request(request).allowed
    for reader, payload in [
        (ActionEnvelope, action.as_dict()),
        (ReviewRequest, request.as_dict()),
        (ActionHistory, ActionHistory().as_dict()),
        (ActionObservation, ActionObservation("a").as_dict()),
        (ObservationHistory, ObservationHistory().as_dict()),
    ]:
        with pytest.raises(ValueError, match="unknown"):
            reader.from_dict({**payload, "typo": True})


def test_claude_help_does_not_read_hook_input(monkeypatch, capsys):
    class Unreadable:
        def read(self, *args):
            raise AssertionError("help must not read a hook")

    monkeypatch.setattr(sys, "stdin", Unreadable())
    assert claude_main(["--help"]) == 0
    assert "session-start" in capsys.readouterr().out


def test_public_export_and_console_inventory_matches_frozen_manifest():
    from importlib import import_module

    manifest = load_json(DATA_DIR / "public-surface-0.3.json")
    assert sorted(ordin.__all__) == manifest["exports"]
    assert len(ordin.__all__) == len(set(ordin.__all__))
    assert all(hasattr(ordin, name) for name in ordin.__all__)
    assert set(SCHEMA_FILES) == set(manifest["schemas"])
    for module, names in manifest["module_contracts"].items():
        assert all(hasattr(import_module(module), name) for name in names), module
