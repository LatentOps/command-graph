import json

import pytest

from ordin import ActionEnvelope, ActionResource, ActionReview, ExecutionContext, Ordin
from ordin.action_policy import (
    ActionPolicyCondition,
    ActionPolicyRule,
    ActionPolicySet,
    PolicyResourceMatcher,
    load_action_policy,
)


def _review(
    *,
    kind="tool",
    operation="call",
    decision="allow",
    risk="low",
    effects=(),
    resources=(),
    context=None,
    intent=None,
    intent_alignment="not_applicable",
    trajectory=(),
):
    return ActionReview(
        action=ActionEnvelope(
            kind=kind,
            operation=operation,
            parameters={},
            intent=intent,
            context=context,
        ),
        decision=decision,
        risk=risk,
        reasons=["base review"],
        safer_next_step=None,
        effects=list(effects),
        resources=list(resources),
        adapter=None,
        intent_alignment=intent_alignment,
        trajectory_categories=list(trajectory),
    )


def _policy(*rules):
    return ActionPolicySet(policy_id="test-policy", version="1", rules=tuple(rules)).compile()


def test_policy_blocks_matching_effect_and_records_provenance():
    policy = _policy(
        ActionPolicyRule(
            id="block-delete",
            decision="block",
            when=ActionPolicyCondition(effects_any=("filesystem.delete",)),
            reason="deletion is disabled in this runtime",
            safer_next_step="Inspect the target without deleting it.",
        )
    )
    review = _review(decision="warn", risk="high", effects=("filesystem.delete",))

    result = policy.apply(review)

    assert result.decision == "block"
    assert result.policy["policy_id"] == "test-policy"
    assert result.policy_matches[0]["rule_id"] == "block-delete"
    assert "policy block-delete" in result.reasons[-1]
    assert result.safer_next_step == "Inspect the target without deleting it."


def test_policy_ask_can_elevate_warn_using_enforcement_order():
    policy = _policy(
        ActionPolicyRule(
            id="approve-warnings",
            decision="ask",
            when=ActionPolicyCondition(decisions=("warn",)),
        )
    )

    result = policy.apply(_review(decision="warn", risk="high"))

    assert result.decision == "ask"


def test_policy_warn_cannot_downgrade_existing_ask():
    policy = _policy(
        ActionPolicyRule(
            id="warn-tool",
            decision="warn",
            when=ActionPolicyCondition(kinds=("tool",)),
        )
    )

    result = policy.apply(_review(decision="ask", risk="unknown"))

    assert result.decision == "ask"


def test_policy_allow_never_downgrades_explicit_core_block():
    policy = _policy(
        ActionPolicyRule(id="allow-all", decision="allow", when=ActionPolicyCondition())
    )

    result = policy.apply(_review(decision="block", risk="critical"))

    assert result.decision == "block"


def test_conflicting_rules_are_deterministic_and_choose_strongest_enforcement():
    policy = _policy(
        ActionPolicyRule(id="warn", decision="warn"),
        ActionPolicyRule(id="ask", decision="ask"),
        ActionPolicyRule(id="block", decision="block"),
    )

    evaluation = policy.evaluate(_review())

    assert [match.rule_id for match in evaluation.matches] == ["warn", "ask", "block"]
    assert evaluation.decision == "block"


def test_resource_matching_supports_exact_and_prefix_only():
    resource = PolicyResourceMatcher(type="path", value="/workspace/repo", match="prefix")
    policy = _policy(
        ActionPolicyRule(
            id="repo-write",
            decision="ask",
            when=ActionPolicyCondition(resources_any=(resource,)),
        )
    )

    result = policy.apply(_review(resources=(ActionResource("path", "/workspace/repo/build"),)))
    assert result.decision == "ask"

    with pytest.raises(ValueError, match="unsupported resource match mode"):
        PolicyResourceMatcher(type="path", value=".*", match="regex")  # type: ignore[arg-type]


def test_context_agent_repo_privilege_intent_and_trajectory_conditions():
    condition = ActionPolicyCondition(
        agents=("coding-agent",),
        cwd_prefixes=("/workspace/repo",),
        repo_scope="inside",
        privileged=False,
        intent="mismatch",
        trajectory_any=("trajectory_download_execute",),
    )
    policy = _policy(ActionPolicyRule(id="context-rule", decision="block", when=condition))
    context = ExecutionContext(
        cwd="/workspace/repo/subdir",
        repo_root="/workspace/repo",
        euid=1000,
        agent="coding-agent",
    )
    review = _review(
        context=context,
        intent="inspect files",
        intent_alignment="mismatch",
        trajectory=("trajectory_download_execute",),
    )

    assert policy.apply(review).decision == "block"


def test_unknown_privilege_does_not_match_root_or_non_root_selector():
    for privileged in (False, True):
        policy = _policy(
            ActionPolicyRule(
                id=f"privileged-{privileged}",
                decision="block",
                when=ActionPolicyCondition(privileged=privileged),
            )
        )

        result = policy.apply(_review(context=ExecutionContext(euid=None)))

        assert result.decision == "allow"
        assert result.policy_matches == []


@pytest.mark.parametrize(
    "cwd,root",
    [
        (".", "."),
        ("src", "/workspace"),
        ("/workspace/src", "."),
        ("", "/workspace"),
        ("/workspace", ""),
    ],
)
def test_relative_or_empty_repository_context_matches_only_unknown_scope(cwd, root):
    context = ExecutionContext(cwd=cwd, repo_root=root)
    for scope in ("inside", "outside", "unknown"):
        policy = _policy(
            ActionPolicyRule(
                id="scope", decision="block", when=ActionPolicyCondition(repo_scope=scope)
            )
        )
        result = policy.apply(_review(context=context))
        assert result.decision == ("block" if scope == "unknown" else "allow")


def test_relative_context_never_uses_the_reviewer_working_directory(tmp_path, monkeypatch):
    policy = _policy(
        ActionPolicyRule(
            id="unknown", decision="block", when=ActionPolicyCondition(repo_scope="unknown")
        )
    )
    for directory in (tmp_path, tmp_path / "nested"):
        directory.mkdir(exist_ok=True)
        monkeypatch.chdir(directory)
        result = policy.apply(_review(context=ExecutionContext(cwd="src", repo_root=".")))
        assert result.decision == "block"


@pytest.mark.parametrize("prefix", [".", "relative/repo", "~/repo"])
def test_cwd_prefix_configuration_requires_absolute_posix_paths(prefix):
    with pytest.raises(ValueError, match="absolute"):
        ActionPolicyCondition(cwd_prefixes=(prefix,))
    with pytest.raises(ValueError, match="absolute"):
        ActionPolicyCondition.from_dict({"cwd_prefixes": [prefix]})


def test_intent_present_matches_even_when_alignment_is_available():
    policy = _policy(
        ActionPolicyRule(
            id="intent-present",
            decision="ask",
            when=ActionPolicyCondition(intent="present"),
        )
    )

    result = policy.apply(_review(intent="inspect files", intent_alignment="aligned"))

    assert result.decision == "ask"


def test_policy_parser_rejects_unknown_fields_and_duplicate_rule_ids():
    payload = {
        "schema_version": "ordin.policy_set.v1",
        "policy_id": "test",
        "version": "1",
        "rules": [{"id": "one", "decision": "warn", "when": {"not_a_selector": True}}],
    }
    with pytest.raises(ValueError, match="unsupported fields"):
        ActionPolicySet.from_dict(payload)

    with pytest.raises(ValueError, match="duplicate policy rule ids"):
        ActionPolicySet(
            policy_id="test",
            version="1",
            rules=(
                ActionPolicyRule(id="same", decision="warn"),
                ActionPolicyRule(id="same", decision="block"),
            ),
        )


def test_policy_compile_is_cached_and_digest_is_stable():
    policy = ActionPolicySet(
        policy_id="test",
        version="1",
        rules=(ActionPolicyRule(id="one", decision="ask"),),
    )

    assert policy.compile() is policy.compile()
    assert policy.digest == ActionPolicySet.from_dict(policy.as_dict()).digest
    assert len(policy.digest) == 64


def test_ordin_compiles_and_reuses_action_policy():
    policy = ActionPolicySet(
        policy_id="test",
        version="1",
        rules=(
            ActionPolicyRule(
                id="approve-shell",
                decision="ask",
                when=ActionPolicyCondition(kinds=("shell",)),
            ),
        ),
    )
    ordin = Ordin(action_policy=policy)

    first = ordin.review_action(ActionEnvelope.shell("git status --short"))
    second = ordin.review_action(ActionEnvelope.shell("git status --short"))

    assert first.decision == "ask"
    assert second.decision == "ask"
    assert first.policy == second.policy
    assert first.policy_matches[0]["rule_id"] == "approve-shell"


def test_load_policy_validates_schema_and_compiles(tmp_path):
    path = tmp_path / "policy.json"
    policy = ActionPolicySet(
        policy_id="test",
        version="1",
        rules=(ActionPolicyRule(id="one", decision="ask"),),
    )
    path.write_text(json.dumps(policy.as_dict()), encoding="utf-8")

    loaded = load_action_policy(path)

    assert loaded.policy == policy
    assert loaded.digest == policy.digest


def test_load_policy_rejects_schema_invalid_file(tmp_path):
    path = tmp_path / "policy.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": "ordin.policy_set.v1",
                "policy_id": "test",
                "version": "1",
                "rules": [{"id": "x", "decision": "warn", "when": {}, "extra": True}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="schema validation failed"):
        load_action_policy(path)
