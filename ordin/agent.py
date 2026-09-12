from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, TypeAlias

from .action import ActionEnvelope, ActionHistory, ActionReview
from .adapters import MCPAdapter, ToolCallAdapter
from .api import Ordin
from .context import ExecutionContext
from .execution import ObservationHistory
from .mcp_contracts import MCPContractCheck
from .review import CommandReview
from .trace import ActionTrace


AgentDisposition: TypeAlias = Literal["execute", "escalate", "deny"]
AgentReview: TypeAlias = CommandReview | ActionReview


@dataclass(frozen=True)
class AgentDecision:
    """Caller-facing disposition for a proposed agent action."""

    disposition: AgentDisposition
    review: AgentReview

    @property
    def may_execute(self) -> bool:
        return self.disposition == "execute"

    @property
    def requires_approval(self) -> bool:
        return self.disposition == "escalate"

    @property
    def denied(self) -> bool:
        return self.disposition == "deny"


@dataclass(frozen=True)
class AgentGate:
    """Translate Ordin reviews into runtime dispositions without executing actions.

    Explicit blocks are always denied. Other decisions are evaluated with the
    `ReviewPolicy` configured on the wrapped `Ordin` instance. A policy failure
    becomes `escalate`, leaving approval, sandboxing, execution, retries, and
    history persistence to the caller.
    """

    ordin: Ordin = field(default_factory=Ordin)

    def evaluate(
        self,
        command: str,
        *,
        intent: str | None = None,
        context: ExecutionContext | None = None,
        trace: ActionTrace | None = None,
    ) -> AgentDecision:
        """Backward-compatible shell-command entry point."""

        review = self.ordin.review(
            command,
            intent=intent,
            context=context,
            trace=trace,
        )
        return AgentDecision(
            disposition=self._disposition(review),
            review=review,
        )

    def evaluate_action(
        self,
        action: ActionEnvelope | Mapping[str, Any],
        *,
        history: ActionHistory | Mapping[str, Any] | None = None,
        observations: ObservationHistory | Mapping[str, Any] | None = None,
        contract_check: MCPContractCheck | None = None,
    ) -> AgentDecision:
        """Review a generic action through the same Ordin policy boundary."""

        review = self.ordin.review_action(
            action,
            history=history,
            observations=observations,
            contract_check=contract_check,
        )
        return AgentDecision(
            disposition=self._disposition(review),
            review=review,
        )

    def evaluate_tool(
        self,
        adapter: ToolCallAdapter,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        intent: str | None = None,
        context: ExecutionContext | None = None,
        action_id: str | None = None,
        history: ActionHistory | Mapping[str, Any] | None = None,
        observations: ObservationHistory | Mapping[str, Any] | None = None,
    ) -> AgentDecision:
        action = adapter.adapt(
            tool,
            arguments,
            intent=intent,
            context=context,
            action_id=action_id,
        )
        return self.evaluate_action(
            action,
            history=history,
            observations=observations,
        )

    def evaluate_mcp(
        self,
        adapter: MCPAdapter,
        tool: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        intent: str | None = None,
        context: ExecutionContext | None = None,
        action_id: str | None = None,
        history: ActionHistory | Mapping[str, Any] | None = None,
        observations: ObservationHistory | Mapping[str, Any] | None = None,
    ) -> AgentDecision:
        action = adapter.adapt(
            tool,
            arguments,
            intent=intent,
            context=context,
            action_id=action_id,
        )
        return self.evaluate_action(
            action,
            history=history,
            observations=observations,
        )

    def _disposition(self, review: AgentReview) -> AgentDisposition:
        if review.blocked:
            return "deny"
        if self.ordin.allows(review):
            return "execute"
        return "escalate"
