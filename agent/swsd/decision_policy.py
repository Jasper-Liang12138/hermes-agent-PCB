"""Probabilistic SWSD4 decision policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent.swsd.intent_field import IntentFieldOutput
from agent.swsd.intent_policy import (
    FLOW_IDLE,
    INTENT_CHAT,
    INTENT_PCB_ENTRY,
    INTENT_PCB_REROUTE_SELECTED,
    INTENT_UNCLEAR,
    ROUTE_MODE_CHAT,
    ROUTE_MODE_PCB,
    PolicyDecision,
    apply_swsd2_policy,
)
from agent.swsd.skill_grounding import SkillGroundingItem


@dataclass(frozen=True)
class WorkflowContext:
    workflow_state: str = FLOW_IDLE
    current_node: str = FLOW_IDLE
    allowed_transitions: tuple[str, ...] = ()
    tool_context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbabilisticDecision:
    intent: str
    route_mode: str
    should_call_get_project_data: bool = False
    reason: str = ""
    intent_field: IntentFieldOutput | None = None
    skill_grounding: tuple[SkillGroundingItem, ...] = ()

    def as_policy_decision(self) -> PolicyDecision:
        return PolicyDecision(
            intent=self.intent,
            route_mode=self.route_mode,
            should_call_get_project_data=self.should_call_get_project_data,
            confidence=max(
                self.intent_field.chat,
                self.intent_field.analyze,
                self.intent_field.execute,
                self.intent_field.meta,
            )
            if self.intent_field
            else 0.0,
            reason=self.reason,
        )


def _workflow_allows_execute(context: WorkflowContext) -> bool:
    if context.workflow_state != FLOW_IDLE:
        return True
    if not context.allowed_transitions:
        return True
    return any(item in {"pcb_entry", "pcb_reroute_selected", "select_target", "confirm_route"} for item in context.allowed_transitions)


def _candidate_intent(candidate: Any) -> str:
    if isinstance(candidate, dict):
        return str(candidate.get("intent") or "")
    return str(getattr(candidate, "intent", "") or "")


def decide_with_intent_field(
    *,
    text: str,
    session_mode: str,
    candidate: Any,
    intent_field: IntentFieldOutput,
    workflow_context: WorkflowContext,
    skill_grounding: list[SkillGroundingItem] | None = None,
) -> ProbabilisticDecision:
    max_score = max(intent_field.chat, intent_field.analyze, intent_field.execute, intent_field.meta)
    grounding = tuple(skill_grounding or ())
    flow_state = workflow_context.workflow_state or FLOW_IDLE

    if intent_field.uncertainty >= 0.35 or max_score < 0.55:
        mode = ROUTE_MODE_PCB if flow_state != FLOW_IDLE or session_mode == ROUTE_MODE_PCB else ROUTE_MODE_CHAT
        return ProbabilisticDecision(INTENT_UNCLEAR, mode, False, "swsd4_uncertain", intent_field, grounding)

    if intent_field.meta >= 0.65:
        mode = ROUTE_MODE_PCB if flow_state != FLOW_IDLE or session_mode == ROUTE_MODE_PCB else ROUTE_MODE_CHAT
        return ProbabilisticDecision(INTENT_UNCLEAR, mode, False, "swsd4_meta_defer", intent_field, grounding)

    if intent_field.chat >= 0.65 or intent_field.analyze >= 0.60:
        return ProbabilisticDecision(INTENT_CHAT, ROUTE_MODE_CHAT, False, "swsd4_discussion", intent_field, grounding)

    if intent_field.execute >= 0.65 and _workflow_allows_execute(workflow_context):
        swsd2 = apply_swsd2_policy(text=text, flow_state=flow_state, session_mode=session_mode, candidate=candidate)
        if swsd2.intent in {INTENT_CHAT, INTENT_UNCLEAR}:
            raw_intent = _candidate_intent(candidate)
            if raw_intent in {INTENT_PCB_ENTRY, INTENT_PCB_REROUTE_SELECTED}:
                return ProbabilisticDecision(
                    raw_intent,
                    ROUTE_MODE_PCB,
                    raw_intent == INTENT_PCB_ENTRY,
                    "swsd4_execute_candidate",
                    intent_field,
                    grounding,
                )
        return ProbabilisticDecision(
            swsd2.intent,
            swsd2.route_mode,
            swsd2.should_call_get_project_data,
            "swsd4_execute_swsd2",
            intent_field,
            grounding,
        )

    mode = ROUTE_MODE_PCB if flow_state != FLOW_IDLE or session_mode == ROUTE_MODE_PCB else ROUTE_MODE_CHAT
    return ProbabilisticDecision(INTENT_UNCLEAR, mode, False, "swsd4_no_policy_threshold", intent_field, grounding)
