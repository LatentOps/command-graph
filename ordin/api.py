from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .action import ActionEnvelope, ActionHistory, ActionReview, review_action
from .action_policy import ActionPolicySet, CompiledActionPolicySet
from .audit import AuditSink
from .context import ExecutionContext, ReviewRequest
from .execution import ObservationHistory
from .mcp_contracts import MCPContractCheck
from .policy import ReviewPolicy
from .review import CommandReview, review_command
from .risk import RiskReview, check_command
from .schema import validate_named_schema
from .search import SearchResult, search
from .temporal import CompiledTemporalPolicySet, TemporalPolicySet
from .tool_calls import CompiledToolSemanticsRegistry, ToolSemanticsRegistry
from .trace import ActionTrace


@dataclass(frozen=True)
class Ordin:
    """Stable library entry point for command discovery and pre-execution review.

    Ordin never executes shell commands or generic actions. Review is read-only
    unless the caller explicitly configures a local audit sink.
    """

    context: ExecutionContext | None = None
    trace: ActionTrace | None = None
    policy: ReviewPolicy = field(default_factory=ReviewPolicy)
    action_policy: ActionPolicySet | CompiledActionPolicySet | None = None
    temporal_policy: TemporalPolicySet | CompiledTemporalPolicySet | None = None
    tool_semantics: ToolSemanticsRegistry | CompiledToolSemanticsRegistry | None = None
    audit: AuditSink | None = None

    def __post_init__(self) -> None:
        if isinstance(self.action_policy, ActionPolicySet):
            object.__setattr__(self, "action_policy", self.action_policy.compile())
        elif self.action_policy is not None and not isinstance(
            self.action_policy, CompiledActionPolicySet
        ):
            raise ValueError("action_policy must be an ActionPolicySet, compiled policy, or null")
        if isinstance(self.temporal_policy, TemporalPolicySet):
            object.__setattr__(self, "temporal_policy", self.temporal_policy.compile())
        elif self.temporal_policy is not None and not isinstance(
            self.temporal_policy, CompiledTemporalPolicySet
        ):
            raise ValueError(
                "temporal_policy must be a TemporalPolicySet, compiled policy, or null"
            )
        if isinstance(self.tool_semantics, ToolSemanticsRegistry):
            object.__setattr__(self, "tool_semantics", self.tool_semantics.compile())
        elif self.tool_semantics is not None and not isinstance(
            self.tool_semantics, CompiledToolSemanticsRegistry
        ):
            raise ValueError(
                "tool_semantics must be a ToolSemanticsRegistry, compiled registry, or null"
            )
        if self.audit is not None and not callable(getattr(self.audit, "record", None)):
            raise ValueError("audit must provide a callable record(review) method or be null")

    def search(self, query: str, *, limit: int = 5) -> list[SearchResult]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("search query must be non-empty text")
        if limit < 1:
            raise ValueError("search limit must be at least 1")
        return search(query, limit=limit)

    def check(
        self,
        command: str,
        *,
        context: ExecutionContext | None = None,
    ) -> RiskReview:
        return check_command(command, context=context if context is not None else self.context)

    def review(
        self,
        command: str,
        *,
        intent: str | None = None,
        context: ExecutionContext | None = None,
        trace: ActionTrace | None = None,
    ) -> CommandReview:
        return review_command(
            command,
            intent=intent,
            context=context if context is not None else self.context,
            trace=trace if trace is not None else self.trace,
        )

    def review_request(
        self,
        request: ReviewRequest | Mapping[str, Any],
    ) -> CommandReview:
        parsed = self._parse_request(request)
        return review_command(
            parsed.command,
            intent=parsed.intent,
            context=parsed.context if parsed.context is not None else self.context,
            trace=parsed.trace if parsed.trace is not None else self.trace,
        )

    def review_action(
        self,
        action: ActionEnvelope | Mapping[str, Any],
        *,
        history: ActionHistory | Mapping[str, Any] | None = None,
        observations: ObservationHistory | Mapping[str, Any] | None = None,
        contract_check: MCPContractCheck | None = None,
    ) -> ActionReview:
        parsed = self._parse_action(action)
        if parsed.context is None and self.context is not None:
            parsed = ActionEnvelope(
                kind=parsed.kind,
                operation=parsed.operation,
                parameters=parsed.parameters,
                intent=parsed.intent,
                context=self.context,
                action_id=parsed.action_id,
            )
        parsed_history = self._parse_history(history)
        parsed_observations = self._parse_observations(observations)
        temporal = self.temporal_policy
        tool_semantics = self.tool_semantics
        result = review_action(
            parsed,
            history=parsed_history,
            observations=parsed_observations,
            temporal_policy=(temporal if isinstance(temporal, CompiledTemporalPolicySet) else None),
            tool_semantics=(
                tool_semantics
                if isinstance(tool_semantics, CompiledToolSemanticsRegistry)
                else None
            ),
        )
        compiled_policy = self.action_policy
        if isinstance(compiled_policy, CompiledActionPolicySet):
            result = compiled_policy.apply(result)
        if contract_check is not None:
            result = contract_check.apply(result)
        if self.audit is not None:
            self.audit.record(result)
        return result

    def allows(self, review: RiskReview | CommandReview | ActionReview) -> bool:
        if isinstance(review, ActionReview) and review.provenance is not None:
            if any(
                record.code.startswith("mcp.contract.") and record.code != "mcp.contract.matched"
                for record in review.provenance.records
            ):
                return False
        return self.policy.allows(review)

    def _parse_request(self, request: ReviewRequest | Mapping[str, Any]) -> ReviewRequest:
        if isinstance(request, ReviewRequest):
            return request
        if not isinstance(request, Mapping):
            raise ValueError("review request must be a ReviewRequest or mapping")
        payload = dict(request)
        errors = validate_named_schema("review_request", payload)
        if errors:
            raise ValueError("review request schema validation failed: " + "; ".join(errors))
        return ReviewRequest.from_dict(payload)

    def _parse_history(
        self, history: ActionHistory | Mapping[str, Any] | None
    ) -> ActionHistory | None:
        if history is None or isinstance(history, ActionHistory):
            return history
        if not isinstance(history, Mapping):
            raise ValueError("history must be an ActionHistory, mapping, or null")
        payload = dict(history)
        errors = validate_named_schema("action_history", payload)
        if errors:
            raise ValueError("action history schema validation failed: " + "; ".join(errors))
        return ActionHistory.from_dict(payload)

    def _parse_observations(
        self,
        observations: ObservationHistory | Mapping[str, Any] | None,
    ) -> ObservationHistory | None:
        if observations is None or isinstance(observations, ObservationHistory):
            return observations
        if not isinstance(observations, Mapping):
            raise ValueError("observations must be an ObservationHistory, mapping, or null")
        payload = dict(observations)
        errors = validate_named_schema("observation_history", payload)
        if errors:
            raise ValueError("observation history schema validation failed: " + "; ".join(errors))
        return ObservationHistory.from_dict(payload)

    def _parse_action(self, action: ActionEnvelope | Mapping[str, Any]) -> ActionEnvelope:
        if isinstance(action, ActionEnvelope):
            return action
        if not isinstance(action, Mapping):
            raise ValueError("action must be an ActionEnvelope or mapping")
        payload = dict(action)
        errors = validate_named_schema("action_envelope", payload)
        if errors:
            raise ValueError("action envelope schema validation failed: " + "; ".join(errors))
        return ActionEnvelope.from_dict(payload)
