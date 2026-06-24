"""Action candidate and agent advisory contracts for SWSD.

These structures let LLM-backed components provide suggestions without owning
workflow state, tool execution, or final PCB facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True)
class ActionCandidate:
    action: str
    confidence: float = 0.0
    entities: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    source: str = "unknown"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any], *, source: str = "intent_model") -> "ActionCandidate":
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.0))))
        except (TypeError, ValueError):
            confidence = 0.0
        entities = data.get("entities")
        return cls(
            action=str(data.get("action") or data.get("intent") or "").strip(),
            confidence=confidence,
            entities=dict(entities) if isinstance(entities, Mapping) else {},
            reason=str(data.get("reason") or data.get("why") or "").strip(),
            source=str(data.get("source") or source).strip() or source,
        )


@dataclass(frozen=True)
class IntentCandidateSet:
    workflow: str
    current_state: str
    candidate_actions: tuple[ActionCandidate, ...] = ()
    model_source: str = "intent_model"

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "IntentCandidateSet":
        raw_actions = data.get("candidateActions") or data.get("candidate_actions") or ()
        candidates = tuple(
            ActionCandidate.from_mapping(item, source=str(data.get("modelSource") or "intent_model"))
            for item in raw_actions
            if isinstance(item, Mapping)
        )
        return cls(
            workflow=str(data.get("workflow") or data.get("workflowId") or "").strip(),
            current_state=str(data.get("currentState") or data.get("current_state") or "").strip(),
            candidate_actions=candidates,
            model_source=str(data.get("modelSource") or data.get("model_source") or "intent_model").strip(),
        )


@dataclass(frozen=True)
class AgentAssistRequest:
    purpose: str
    workflow_id: str
    workflow_state: str
    facts: dict[str, Any] = field(default_factory=dict)
    allowed_outputs: tuple[str, ...] = ()
    forbidden_actions: tuple[str, ...] = (
        "call_tool",
        "change_workflow_state",
        "invent_structured_fields",
    )


@dataclass(frozen=True)
class AgentAssistResult:
    purpose: str
    candidates: tuple[ActionCandidate, ...] = ()
    narrative_text: str = ""
    recovery_advice: str = ""
    facts_used: tuple[str, ...] = ()

