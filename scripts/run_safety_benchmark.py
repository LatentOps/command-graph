from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from ordin.safety_benchmark import (
    DEFAULT_FUZZ_SEED,
    SafetyThresholds,
    adversarial_equivalence_errors,
    evaluate_safety,
    generic_action_fuzz_errors,
    load_safety_fixtures,
    policy_accuracy_errors,
)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / "benchmarks" / "safety.jsonl"


def _human_report(payload: dict[str, Any]) -> str:
    latency = payload["latency_ms"]
    lines = [
        "Ordin safety benchmark",
        f"cases: {payload['cases']}",
        f"exact matches: {payload['exact_matches']}",
        f"false allows: {payload['false_allows']}",
        f"critical misses: {payload['critical_misses']}",
        f"false blocks: {payload['false_blocks']}",
        f"semantic misses: {payload['semantic_misses']}",
        f"ask rate: {payload['ask_rate']:.4f}",
        f"trajectory detection rate: {payload['trajectory_detection_rate']:.4f}",
        f"latency p50/p95/p99: {latency['p50']:.4f}/{latency['p95']:.4f}/{latency['p99']:.4f} ms",
        f"policy errors: {len(payload['policy_errors'])}",
        f"shell fuzz errors: {len(payload['fuzz_errors'])}",
        f"generic action fuzz errors: {len(payload['generic_fuzz_errors'])}",
        f"status: {'PASS' if payload['ok'] else 'FAIL'}",
    ]
    for domain, coverage in sorted(payload["domain_coverage"].items()):
        lines.append(
            f"domain {domain}: {coverage['semantic_matches']}/{coverage['cases']} semantic matches"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Ordin's deterministic safety benchmark.")
    parser.add_argument("--fixtures", type=Path, default=DEFAULT_FIXTURES)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--seed", type=int, default=DEFAULT_FUZZ_SEED)
    parser.add_argument("--fuzz-iterations", type=int, default=32)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument(
        "--deep",
        action="store_true",
        help="Run a deeper local benchmark with more fuzz iterations and repeated latency samples.",
    )
    parser.add_argument("--max-p95-ms", type=float, default=250.0)
    parser.add_argument("--max-p99-ms", type=float, default=500.0)
    args = parser.parse_args()

    fuzz_iterations = max(args.fuzz_iterations, 128) if args.deep else args.fuzz_iterations
    repetitions = max(args.repetitions, 10) if args.deep else args.repetitions

    fixtures = load_safety_fixtures(args.fixtures)
    report = evaluate_safety(fixtures, repetitions=repetitions)
    thresholds = SafetyThresholds(max_p95_ms=args.max_p95_ms, max_p99_ms=args.max_p99_ms)
    regression_errors = report.regression_errors(thresholds)
    fuzz_errors = adversarial_equivalence_errors(seed=args.seed)
    policy_errors = policy_accuracy_errors()
    generic_fuzz_errors = generic_action_fuzz_errors(
        seed=args.seed,
        iterations=fuzz_iterations,
    )

    if len(policy_errors) > thresholds.max_policy_misses:
        regression_errors.append(
            f"policy misses {len(policy_errors)} exceed {thresholds.max_policy_misses}"
        )
    if len(generic_fuzz_errors) > thresholds.max_generic_fuzz_errors:
        regression_errors.append(
            "generic fuzz errors "
            f"{len(generic_fuzz_errors)} exceed {thresholds.max_generic_fuzz_errors}"
        )

    payload = report.as_dict()
    payload["fixture_schema_version"] = "ordin.safety_fixture.v1"
    payload["mode"] = "deep" if args.deep else "ci-smoke"
    payload["repetitions"] = repetitions
    payload["fuzz_seed"] = args.seed
    payload["fuzz_iterations"] = fuzz_iterations
    payload["fuzz_errors"] = fuzz_errors
    payload["policy_errors"] = policy_errors
    payload["generic_fuzz_errors"] = generic_fuzz_errors
    payload["performance_budgets_ms"] = {
        "p95": thresholds.max_p95_ms,
        "p99": thresholds.max_p99_ms,
    }
    payload["regression_errors"] = regression_errors
    payload["ok"] = not regression_errors and not fuzz_errors and not policy_errors

    rendered_json = json.dumps(payload, indent=2, sort_keys=True)
    if args.format == "text":
        print(_human_report(payload))
    else:
        print(rendered_json)
    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered_json + "\n", encoding="utf-8")

    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
