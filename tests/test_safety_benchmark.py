from pathlib import Path

import pytest

from ordin.safety_benchmark import (
    DEFAULT_FUZZ_SEED,
    SAFETY_FIXTURE_SCHEMA_VERSION,
    SafetyFixture,
    SafetyThresholds,
    adversarial_equivalence_errors,
    evaluate_safety,
    generate_adversarial_equivalence_cases,
    generic_action_fuzz_errors,
    load_safety_fixtures,
    policy_accuracy_errors,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "benchmarks" / "safety.jsonl"


def test_repository_safety_fixtures_have_zero_false_allows_and_critical_misses():
    report = evaluate_safety(load_safety_fixtures(FIXTURES))

    assert report.case_count >= 30
    assert report.false_allows == 0
    assert report.critical_misses == 0
    assert report.false_blocks == 0
    assert report.semantic_misses == 0
    assert report.regression_errors(SafetyThresholds()) == []
    assert report.percentile_ms(0.50) >= 0
    assert report.percentile_ms(0.95) >= report.percentile_ms(0.50)
    assert report.percentile_ms(0.99) >= report.percentile_ms(0.95)


def test_trajectory_cases_are_explicitly_measured():
    report = evaluate_safety(load_safety_fixtures(FIXTURES))

    assert len(report.trajectory_cases) >= 3
    assert report.trajectory_detection_rate == 1.0


def test_priority_domain_semantics_are_measured_in_benchmark():
    report = evaluate_safety(load_safety_fixtures(FIXTURES))

    expected_domains = {
        "aws",
        "azure",
        "database",
        "gcloud",
        "github",
        "kubernetes",
        "remote",
        "systemd",
        "terraform",
    }
    assert set(report.domain_coverage) == expected_domains
    for coverage in report.domain_coverage.values():
        assert coverage["cases"] >= 2
        assert coverage["semantic_matches"] == coverage["cases"]


def test_fuzz_generation_is_deterministic_for_recorded_seed():
    first = generate_adversarial_equivalence_cases(seed=DEFAULT_FUZZ_SEED)
    second = generate_adversarial_equivalence_cases(seed=DEFAULT_FUZZ_SEED)

    assert first == second
    assert first
    assert all(case.variants for case in first)


def test_adversarial_dangerous_forms_do_not_weaken_decisions():
    assert adversarial_equivalence_errors(seed=DEFAULT_FUZZ_SEED) == []


def test_generic_tool_and_mcp_fuzz_is_deterministic_and_fail_closed():
    first = generic_action_fuzz_errors(seed=DEFAULT_FUZZ_SEED, iterations=16)
    second = generic_action_fuzz_errors(seed=DEFAULT_FUZZ_SEED, iterations=16)

    assert first == second == []


def test_declarative_policy_accuracy_has_no_misses():
    assert policy_accuracy_errors() == []


def test_fixture_loader_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "fixtures.jsonl"
    path.write_text(
        '{"schema_version":"ordin.safety_fixture.v1","id":"same","type":"shell",'
        '"command":"git status","expected":"allow"}\n'
        '{"schema_version":"ordin.safety_fixture.v1","id":"same","type":"shell",'
        '"command":"git status","expected":"allow"}\n',
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate safety fixture id"):
        load_safety_fixtures(path)


def test_fixture_validation_is_fail_closed():
    with pytest.raises(ValueError, match="requires runtime"):
        SafetyFixture.from_dict(
            {
                "schema_version": SAFETY_FIXTURE_SCHEMA_VERSION,
                "id": "bad-tool",
                "type": "tool",
                "tool": "read",
                "expected": "ask",
            }
        )

    with pytest.raises(ValueError, match="expected decision"):
        SafetyFixture.from_dict(
            {
                "schema_version": SAFETY_FIXTURE_SCHEMA_VERSION,
                "id": "bad-decision",
                "type": "shell",
                "command": "git status",
                "expected": 1,
            }
        )

    with pytest.raises(ValueError, match="unsupported safety fixture schema"):
        SafetyFixture.from_dict(
            {
                "schema_version": "ordin.safety_fixture.v999",
                "id": "bad-schema",
                "type": "shell",
                "command": "git status",
                "expected": "allow",
            }
        )


def test_performance_budgets_are_hard_regression_gates():
    report = evaluate_safety(
        [
            SafetyFixture(
                id="safe",
                type="shell",
                command="git status --short",
                expected="allow",
            )
        ]
    )

    strict = SafetyThresholds(max_p95_ms=-1.0, max_p99_ms=-1.0)
    errors = report.regression_errors(strict)
    assert any("p95 latency" in error for error in errors)
    assert any("p99 latency" in error for error in errors)


def test_report_exposes_machine_readable_metrics():
    report = evaluate_safety(
        [
            SafetyFixture(
                id="safe",
                type="shell",
                command="git status --short",
                expected="allow",
            ),
            SafetyFixture(
                id="unknown",
                type="tool",
                runtime="agent",
                tool="unknown",
                expected="ask",
            ),
        ]
    )

    payload = report.as_dict()
    assert payload["cases"] == 2
    assert payload["false_allows"] == 0
    assert payload["critical_misses"] == 0
    assert payload["false_blocks"] == 0
    assert payload["semantic_misses"] == 0
    assert payload["asks"] == 1
    assert set(payload["latency_ms"]) == {"p50", "p95", "p99"}
