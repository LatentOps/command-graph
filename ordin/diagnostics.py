from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .action import ActionReview
from .integration_conformance import run_integration_conformance


DIAGNOSTIC_SCHEMA_VERSION = "ordin.integration_diagnostic.v1"
INTEGRATION_HEALTH_SCHEMA_VERSION = "ordin.integration_health.v1"


@dataclass(frozen=True)
class DiagnosticRemediation:
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _identity_summary(review: ActionReview) -> dict[str, str]:
    parameters = review.action.parameters
    identity: dict[str, str] = {}
    for key in ("runtime", "server", "tool"):
        value = parameters.get(key)
        if isinstance(value, str) and value:
            identity[key] = value
    return identity


def _private_literals(review: ActionReview) -> tuple[str, ...]:
    values: set[str] = {resource.value for resource in review.resources if resource.value}

    def collect(value: Any, *, key: str | None = None) -> None:
        if isinstance(value, Mapping):
            for child_key, child in value.items():
                collect(child, key=str(child_key))
            return
        if isinstance(value, list):
            for child in value:
                collect(child, key=key)
            return
        if isinstance(value, str) and value and key not in {"runtime", "server", "tool"}:
            values.add(value)

    collect(review.action.parameters)
    return tuple(sorted(values, key=len, reverse=True))


def _redact_text(value: str, review: ActionReview) -> str:
    redacted = value
    for literal in _private_literals(review):
        if len(literal) >= 3:
            redacted = redacted.replace(literal, "<redacted>")
    return redacted


def _capability_summary(review: ActionReview) -> dict[str, Any] | None:
    if review.capabilities is None:
        return None
    payload = review.capabilities.as_dict()
    return {
        "filesystem": payload.get("filesystem"),
        "network": payload.get("network"),
        "privilege_escalation": payload.get("privilege_escalation"),
        "process_execution": payload.get("process_execution"),
    }


def _policy_summary(review: ActionReview) -> dict[str, Any]:
    matches = []
    for match in review.policy_matches:
        matches.append(
            {
                "rule_id": match.get("rule_id"),
                "decision": match.get("decision"),
            }
        )
    return {
        "applied": review.policy is not None,
        "matches": matches,
    }


def _remediations(review: ActionReview) -> list[DiagnosticRemediation]:
    items: list[DiagnosticRemediation] = []
    if review.provenance is not None and any(
        record.code.startswith("mcp.contract.") and record.code != "mcp.contract.matched"
        for record in review.provenance.records
    ):
        items.append(
            DiagnosticRemediation(
                code="mcp_contract_mismatch",
                message="Inspect the live MCP inventory and compare the reviewed semantics and contract lock. Resolve missing or changed mappings before approving execution.",
            )
        )
    if review.decision == "block":
        items.append(
            DiagnosticRemediation(
                code="blocked_action",
                message="Do not execute this action. Change the proposed action or satisfy the safety requirement instead of bypassing the decision.",
            )
        )
    elif review.uncertain:
        items.append(
            DiagnosticRemediation(
                code="uncertain_semantics",
                message="The action semantics or identity are not established. Supply a supported exact runtime/tool identity or request caller-owned approval.",
            )
        )
    elif review.decision in {"warn", "ask"}:
        items.append(
            DiagnosticRemediation(
                code="approval_required",
                message="Review the reported effects and resources, then use the caller-owned approval flow if the action is intended.",
            )
        )

    if review.adapter is None and review.action.kind in {"tool", "mcp"}:
        items.append(
            DiagnosticRemediation(
                code="untrusted_tool_identity",
                message="No trusted semantics matched this exact identity. Use ordin mcp inspect and ordin semantics scaffold, then review the effect/resource mappings before loading them.",
            )
        )
    if review.safer_next_step:
        items.append(
            DiagnosticRemediation(
                code="safer_next_step",
                message=_redact_text(review.safer_next_step, review),
            )
        )
    return items


def action_review_diagnostic(review: ActionReview) -> dict[str, Any]:
    """Return a stable redacted explanation of a generic action review.

    Raw action parameters and resource values are intentionally excluded. The
    diagnostic is safe to place in ordinary local logs subject to the caller's
    own handling of action IDs and integration identity names.
    """

    provenance = review.provenance.as_dict() if review.provenance is not None else None
    if provenance is not None:
        provenance = {
            "schema_version": provenance.get("schema_version"),
            "final_decision": provenance.get("final_decision"),
            "final_risk": provenance.get("final_risk"),
            "record_count": len(provenance.get("records", [])),
        }

    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "action": {
            "action_id": review.action.action_id,
            "kind": review.action.kind,
            "operation": review.action.operation,
            "identity": _identity_summary(review),
            "parameter_keys": sorted(review.action.parameters),
        },
        "decision": {
            "decision": review.decision,
            "risk": review.risk,
            "uncertain": review.uncertain,
            "reasons": [_redact_text(reason, review) for reason in review.reasons],
            "effects": list(review.effects),
            "resource_types": sorted({resource.type for resource in review.resources}),
            "resource_count": len(review.resources),
        },
        "contributors": {
            "adapter": review.adapter,
            "trajectory_categories": list(review.trajectory_categories),
            "policy": _policy_summary(review),
            "capabilities": _capability_summary(review),
            "provenance": provenance,
        },
        "remediation": [item.as_dict() for item in _remediations(review)],
        "redaction": {
            "raw_parameters_included": False,
            "resource_values_included": False,
            "caller_literals_scrubbed_from_prose": True,
        },
    }


def integration_health() -> dict[str, Any]:
    report = run_integration_conformance()
    return {
        "schema_version": INTEGRATION_HEALTH_SCHEMA_VERSION,
        "ok": report.failed == 0,
        "integrations": list(report.integrations),
        "checks": len(report.checks),
        "passed": report.passed,
        "failed": report.failed,
        "errors": report.errors(),
    }
