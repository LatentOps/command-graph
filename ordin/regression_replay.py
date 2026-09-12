from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .trajectory_corpus import (
    AgentTrajectory,
    TrajectoryResult,
    run_agent_trajectory_corpus,
)


REGRESSION_SCHEMA_VERSION = "ordin.regression_case.v1"
REPORT_SCHEMA_VERSION = "ordin.regression_report.v1"
VALID_FAILURE_CLASSES = frozenset(
    {
        "false_allow",
        "false_block",
        "semantic_effect",
        "parser_normalizer",
        "identity_handling",
        "policy_temporal",
        "provenance_audit",
        "post_action_observation",
        "integration_translation",
        "performance_regression",
    }
)
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "authorization",
    "cookie",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "private_key",
)
_SENSITIVE_VALUE_PATTERNS = (
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bAKIA[A-Z0-9]{16}\b"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
)


def _non_empty_text(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty text")
    return value


def _scan_sensitive(value: Any, *, path: str = "$") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in _SENSITIVE_KEY_PARTS):
                raise ValueError(
                    f"sensitive field is not allowed in regression fixture: {path}.{key}"
                )
            _scan_sensitive(item, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _scan_sensitive(item, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        for pattern in _SENSITIVE_VALUE_PATTERNS:
            if pattern.search(value):
                raise ValueError(
                    f"sensitive-looking value is not allowed in regression fixture: {path}"
                )


@dataclass(frozen=True)
class FailureRegressionCase:
    id: str
    failure_class: str
    invariant: str
    source: str
    trajectory: AgentTrajectory
    schema_version: str = REGRESSION_SCHEMA_VERSION
    audit_control: str | None = None

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "FailureRegressionCase":
        _scan_sensitive(payload)
        if payload.get("schema_version") != REGRESSION_SCHEMA_VERSION:
            raise ValueError(f"unsupported regression schema: {payload.get('schema_version')!r}")
        case_id = _non_empty_text(payload.get("id"), name="regression id")
        failure_class = _non_empty_text(payload.get("failure_class"), name="failure_class")
        if failure_class not in VALID_FAILURE_CLASSES:
            raise ValueError(
                f"regression {case_id!r} failure_class must be one of "
                f"{sorted(VALID_FAILURE_CLASSES)}"
            )
        invariant = _non_empty_text(payload.get("invariant"), name="invariant")
        source = _non_empty_text(payload.get("source"), name="source")
        trajectory_raw = payload.get("trajectory")
        if not isinstance(trajectory_raw, Mapping):
            raise ValueError(f"regression {case_id!r} requires trajectory object")
        trajectory = AgentTrajectory.from_dict(trajectory_raw)
        audit_control = payload.get("audit_control")
        if audit_control is not None and (
            failure_class != "provenance_audit"
            or audit_control not in {"interleaved_writers", "ambiguous_json", "tail_checkpoint"}
        ):
            raise ValueError("unknown or misplaced audit regression control")
        return cls(
            id=case_id,
            failure_class=failure_class,
            invariant=invariant,
            source=source,
            trajectory=trajectory,
            audit_control=audit_control,
        )


@dataclass(frozen=True)
class FailureRegressionResult:
    case: FailureRegressionCase
    trajectory_result: TrajectoryResult
    audit_errors: tuple[str, ...] = ()

    @property
    def matches(self) -> bool:
        return self.trajectory_result.matches and not self.audit_errors

    @property
    def critical_miss(self) -> bool:
        return any(
            step.expected == "block" and step.actual != "block"
            for step in self.trajectory_result.steps
        )

    def diagnostic(self) -> str:
        if self.matches:
            return f"{self.case.id}: pass"
        if self.audit_errors:
            return f"{self.case.id}: {'; '.join(self.audit_errors)}"
        return f"{self.case.id}: {self.trajectory_result.diagnostic()}"


@dataclass(frozen=True)
class FailureRegressionReport:
    results: tuple[FailureRegressionResult, ...]

    @property
    def case_count(self) -> int:
        return len(self.results)

    @property
    def matches(self) -> int:
        return sum(result.matches for result in self.results)

    @property
    def critical_misses(self) -> int:
        return sum(result.critical_miss for result in self.results)

    @property
    def failure_class_coverage(self) -> dict[str, int]:
        return dict(sorted(Counter(result.case.failure_class for result in self.results).items()))

    def regression_errors(self) -> list[str]:
        return [result.diagnostic() for result in self.results if not result.matches]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "cases": self.case_count,
            "matches": self.matches,
            "critical_misses": self.critical_misses,
            "failure_class_coverage": self.failure_class_coverage,
            "errors": self.regression_errors(),
            "results": [
                {
                    "id": result.case.id,
                    "failure_class": result.case.failure_class,
                    "invariant": result.case.invariant,
                    "source": result.case.source,
                    "matches": result.matches,
                    "critical_miss": result.critical_miss,
                    "audit_errors": list(result.audit_errors),
                }
                for result in self.results
            ],
        }


def load_failure_regressions(
    path: str | Path,
    *,
    case_id: str | None = None,
) -> list[FailureRegressionCase]:
    target = Path(path)
    cases: list[FailureRegressionCase] = []
    seen: set[str] = set()
    with target.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"invalid regression JSON at line {line_number}: {exc.msg}"
                ) from exc
            if not isinstance(payload, Mapping):
                raise ValueError(f"regression line {line_number} must contain an object")
            case = FailureRegressionCase.from_dict(payload)
            if case.id in seen:
                raise ValueError(f"duplicate regression id {case.id!r}")
            seen.add(case.id)
            if case_id is None or case.id == case_id:
                cases.append(case)
    if case_id is not None and not cases:
        raise ValueError(f"regression case {case_id!r} was not found")
    if not cases:
        raise ValueError("regression corpus must contain at least one case")
    return cases


def run_failure_regressions(cases: list[FailureRegressionCase]) -> FailureRegressionReport:
    results: list[FailureRegressionResult] = []
    for case in cases:
        trajectory_report = run_agent_trajectory_corpus([case.trajectory])
        results.append(
            FailureRegressionResult(
                case=case,
                trajectory_result=trajectory_report.results[0],
                audit_errors=_audit_control_errors(case),
            )
        )
    return FailureRegressionReport(results=tuple(results))


def _audit_control_errors(case: FailureRegressionCase) -> tuple[str, ...]:
    if case.audit_control is None:
        return ()
    from tempfile import TemporaryDirectory
    from .action import ActionEnvelope
    from .api import Ordin
    from .audit import JsonlAuditSink, verify_audit_jsonl

    try:
        with TemporaryDirectory(prefix="ordin-audit-regression-") as directory:
            path = Path(directory) / "audit.jsonl"
            first = JsonlAuditSink(path, hash_chain=True)
            second = JsonlAuditSink(path, hash_chain=True)
            review = Ordin().review_action(ActionEnvelope.shell("git status --short"))
            head = first.record(review).event_hash
            if case.audit_control == "interleaved_writers":
                second.record(review)
                first.record(review)
                result = verify_audit_jsonl(path, require_hash_chain=True)
                ok = result.ok and result.event_count == 3
            elif case.audit_control == "ambiguous_json":
                raw = path.read_text()
                path.write_text(
                    raw.replace(
                        '"schema_version":', '"schema_version":"invalid","schema_version":', 1
                    )
                )
                ok = not verify_audit_jsonl(path, require_hash_chain=True).ok
            else:
                path.write_bytes(b"")
                ok = not verify_audit_jsonl(
                    path, require_hash_chain=True, expected_last_hash=head
                ).ok
            return () if ok else (f"audit control {case.audit_control} failed",)
    except (OSError, ValueError) as exc:
        return (f"audit control {case.audit_control}: {exc}",)
