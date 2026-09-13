"""Summarize manually classified reports locally, without identities or telemetry."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import math
from pathlib import Path
from typing import Any, Sequence

from ordin._json_contracts import load_configuration


INTEGRATIONS = (
    "claude-code",
    "codex",
    "cursor",
    "mcp-proxy",
    "mcp-http",
    "shell",
    "python",
    "unknown",
)
FAILURES = (
    "false_allow",
    "false_block",
    "unnecessary_escalation",
    "identity_mismatch",
    "contract_drift",
    "session_history",
    "observation_linkage",
    "parser_transport",
    "policy_semantics",
    "setup_failure",
    "documentation",
    "performance",
    "platform",
)
DISPOSITIONS = ("open", "fixed", "expected_behavior", "documentation_only", "not_reproducible")
SETUP_STAGES = (
    "installation",
    "discovery",
    "configuration",
    "host_enablement",
    "semantics_review",
    "smoke",
    "none",
)


def summarize(payload: dict[str, Any]) -> dict[str, Any]:
    if (
        set(payload) != {"schema_version", "reports"}
        or payload["schema_version"] != "ordin.feedback_summary_input.v1"
    ):
        raise ValueError("invalid feedback summary input")
    reports = payload["reports"]
    if not isinstance(reports, list) or len(reports) > 10000:
        raise ValueError("reports must be a bounded array")
    counts: dict[str, Counter[str]] = {
        key: Counter() for key in ("integration", "failure", "disposition", "setup_stage")
    }
    minutes: list[float] = []
    promoted = 0
    for report in reports:
        if not isinstance(report, dict) or set(report) - {
            "integration",
            "failure",
            "disposition",
            "setup_stage",
            "regression",
            "minutes_to_first_working",
        }:
            raise ValueError(
                "report allows classification fields only; remove identities and free text"
            )
        for key, choices in (
            ("integration", INTEGRATIONS),
            ("failure", FAILURES),
            ("disposition", DISPOSITIONS),
            ("setup_stage", SETUP_STAGES),
        ):
            value = report.get(key)
            if not isinstance(value, str) or value not in choices:
                raise ValueError("invalid feedback classification")
            counts[key][value] += 1
        regression = report.get("regression")
        if not isinstance(regression, bool):
            raise ValueError("regression must be a boolean")
        promoted += regression
        value = report.get("minutes_to_first_working")
        if value is not None:
            if (
                isinstance(value, bool)
                or not isinstance(value, (float, int))
                or not math.isfinite(value)
                or not 0 <= value <= 43200
            ):
                raise ValueError("reported duration must be finite minutes between zero and 43200")
            minutes.append(float(value))
    return {
        "schema_version": "ordin.feedback_summary.v1",
        "reports": len(reports),
        "counts": {key: dict(sorted(value.items())) for key, value in counts.items()},
        "permanent_regressions": promoted,
        "reported_setup_time": {
            "samples": len(minutes),
            "mean_minutes": sum(minutes) / len(minutes) if minutes else None,
        },
        "limitations": "Manually classified reports, not unique users or production prevalence. Durations are optional self-reports.",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument(
        "--output", type=Path, help="Create a new aggregate JSON file; never overwrite"
    )
    args = parser.parse_args(argv)
    try:
        summary = summarize(load_configuration(args.input, label="feedback", maximum=1048576))
        text = json.dumps(summary, indent=2, sort_keys=True) + "\n"
        if args.output:
            with args.output.open("x", encoding="utf-8") as handle:
                handle.write(text)
        print(text, end="")
        return 0
    except (ValueError, OSError, TypeError, RecursionError):
        print(json.dumps({"ok": False, "error": "invalid_feedback_or_output"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
