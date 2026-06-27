"""Data structures for SWSD jump intent arbitration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class RetrievedJumpPrior:
    path: str
    title: str
    score: float
    content: str
    debug_scores: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class WorkflowJumpPlan:
    workflow_id: str
    from_state: str
    action: str
    target_state: str
    confidence: float = 0.0
    entities: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    requires_clarification: bool = False
    clarification: str = ""
    retrieved_prior: dict[str, Any] = field(default_factory=dict)
    debug: dict[str, Any] = field(default_factory=dict)

    def vote_key(self) -> tuple[str, str, str, bool]:
        return (
            self.workflow_id,
            self.action,
            self.target_state,
            bool(self.requires_clarification),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow_id": self.workflow_id,
            "from_state": self.from_state,
            "action": self.action,
            "target_state": self.target_state,
            "confidence": self.confidence,
            "entities": dict(self.entities),
            "reason": self.reason,
            "requires_clarification": self.requires_clarification,
            "clarification": self.clarification,
            "retrieved_prior": dict(self.retrieved_prior),
            "debug": dict(self.debug),
        }


@dataclass(frozen=True)
class JumpIntentLoopInput:
    user_text: str
    workflow_id: str
    workflow_state: str
    state_graph: dict[str, Any]
    state_payload_summary: dict[str, Any] = field(default_factory=dict)
    entities: dict[str, Any] = field(default_factory=dict)
    candidate_action: str = ""
    rejection_context: str = ""
    session_id: str = ""
    project_id: str = ""


@dataclass(frozen=True)
class JumpIntentLoopResult:
    accepted: bool
    plan: WorkflowJumpPlan | None = None
    clarification: str = ""
    prior: RetrievedJumpPrior | None = None
    valid_votes: tuple[WorkflowJumpPlan, ...] = ()
    invalid_rounds: int = 0
    invalid_feedback: tuple[str, ...] = ()
    raw_outputs: tuple[dict[str, Any], ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class JumpConfirmationResult:
    decision: str
    reason: str = ""
    clarification: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "clarification": self.clarification,
        }
