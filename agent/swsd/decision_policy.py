"""Probabilistic SWSD4 decision policy."""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from typing import Any

from agent.swsd.action_candidates import ActionCandidate
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



@dataclass(frozen=True)
class SWSDDecision:
    action: str
    confidence: float = 0.0
    accepted_candidates: tuple[ActionCandidate, ...] = ()
    rejected_candidates: tuple[ActionCandidate, ...] = ()
    requires_confirmation: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ActionEvidence:
    action: str
    confidence: float = 0.0
    candidate: ActionCandidate | None = None
    evidence: tuple[str, ...] = ()
    risk: str = ""
    hard_reject: bool = False
    reason: str = ""


@dataclass(frozen=True)
class PolicyEvidenceSet:
    top_candidates: tuple[ActionEvidence, ...] = ()
    rejected_candidates: tuple[ActionEvidence, ...] = ()
    reason: str = ""



def _state_semantic_score(context: WorkflowContext, candidate: ActionCandidate) -> float:
    text = str(context.tool_context.get("user_text") or "")
    workflow_id = str(context.tool_context.get("workflow_id") or "")
    state = context.workflow_state or FLOW_IDLE
    action = candidate.action
    score = 0.0

    if action == "chat":
        if re.search(r"\u4e3a\u4ec0\u4e48|\u89e3\u91ca|\u539f\u56e0|\u600e\u4e48\u7406\u89e3|what|why|how", text, flags=re.IGNORECASE):
            score += 0.35
        if re.search(r"fanout|\u6247\u51fa|\u9003\u9038|\u5e03\u7ebf|\u62c6\u7ebf|reroute|\u7ebf\u5bbd|\u7ebf\u8ddd|\u91cd\u65b0|\u4fee\u6539", text, flags=re.IGNORECASE):
            score -= 0.25

    if action == "reroute_entry" and re.search(r"\u62c6\u7ebf\u91cd\u5e03|reroute|ripup|rip-up|\u5220\u9664.*(?:\u8d70\u7ebf|\u7ebf|trace)", text, flags=re.IGNORECASE):
        score += 0.45

    fanout_inner_states = {"layer_assign", "escape_order", "layer_assign_escape_order", "param_review", "routing", "review", "import"}
    if workflow_id == "pcb_escape_flow" and state in fanout_inner_states:
        if action == "pcb_entry":
            score -= 0.45
        if action == "rerun_fanout" and re.search(r"\u91cd\u65b0\s*fanout|\u91cd\u65b0\u5e03\u7ebf|\u91cd\u8dd1|\u518d\u8dd1|\u91cd\u65b0\u6765", text, flags=re.IGNORECASE):
            score += 0.5
        if action in {"modify_params", "modify_constraints"} and re.search(r"\u7ebf\u5bbd|\u7ebf\u8ddd|\u53c2\u6570|routerType|\u6539|\u4fee\u6539|\u8c03\u6574|mil", text, flags=re.IGNORECASE):
            score += 0.45
        if action == "change_target" and re.search(r"\u6362|\u91cd\u65b0\u9009\u62e9|\u6539\u6210\s*U\d+|U\d+", text, flags=re.IGNORECASE):
            score += 0.35

    if workflow_id == "pcb_reroute_flow" and state in {"report", "import", "drc_loop"}:
        if action == "reroute_again" and re.search(r"\u518d|\u91cd\u65b0|\u91cd\u6765|reroute|\u62c6\u7ebf\u91cd\u5e03", text, flags=re.IGNORECASE):
            score += 0.4

    return score


def _evidence_for_candidate(context: WorkflowContext, candidate: ActionCandidate, *, hard_reject: bool, semantic_score: float) -> ActionEvidence:
    evidence: list[str] = []
    risk = ""
    state = context.workflow_state or FLOW_IDLE
    workflow_id = str(context.tool_context.get("workflow_id") or "")
    if workflow_id:
        evidence.append(f"\u5f53\u524d workflow/state \u4e3a {workflow_id}/{state}")
    if candidate.reason:
        evidence.append(f"\u5019\u9009\u6765\u6e90\u8bf4\u660e\uff1a{candidate.reason}")
    evidence.append(f"\u6a21\u578b\u6216\u89c4\u5219\u7f6e\u4fe1\u5ea6\u4e3a {candidate.confidence:.2f}")
    if semantic_score > 0:
        evidence.append(f"\u72b6\u6001\u8bed\u4e49\u4f18\u5148\u7ea7\u52a0\u5206 {semantic_score:.2f}")
    elif semantic_score < 0:
        risk = f"\u72b6\u6001\u8bed\u4e49\u5b58\u5728\u51b2\u7a81\uff0c\u6263\u5206 {abs(semantic_score):.2f}"
    if hard_reject:
        risk = risk or "action \u4e0d\u5728 allowed_actions \u5185\u6216\u7f6e\u4fe1\u5ea6\u8fc7\u4f4e"
    return ActionEvidence(
        action=candidate.action,
        confidence=max(0.0, min(1.0, candidate.confidence + semantic_score)),
        candidate=candidate,
        evidence=tuple(evidence),
        risk=risk,
        hard_reject=hard_reject,
        reason="hard_reject" if hard_reject else "candidate_evidence",
    )


def build_policy_evidence(
    *,
    workflow_context: WorkflowContext,
    candidates: list[ActionCandidate] | tuple[ActionCandidate, ...] | None = None,
    experience_actions: list[ActionCandidate] | tuple[ActionCandidate, ...] | None = None,
    top_n: int = 3,
) -> PolicyEvidenceSet:
    """Score candidates and produce evidence for ExpertB action voting."""
    allowed = set(workflow_context.allowed_transitions or ())
    merged = tuple(candidates or ()) + tuple(experience_actions or ())
    by_action: dict[str, ActionEvidence] = {}
    rejected: list[ActionEvidence] = []
    for candidate in merged:
        hard_reject = (
            not candidate.action
            or candidate.confidence < 0.55
            or bool(allowed and candidate.action not in allowed)
        )
        semantic = _state_semantic_score(workflow_context, candidate)
        evidence = _evidence_for_candidate(workflow_context, candidate, hard_reject=hard_reject, semantic_score=semantic)
        if hard_reject:
            rejected.append(evidence)
            continue
        existing = by_action.get(candidate.action)
        if existing is None or evidence.confidence > existing.confidence:
            by_action[candidate.action] = evidence
    top = tuple(sorted(by_action.values(), key=lambda item: item.confidence, reverse=True)[:top_n])
    reason = "top_candidate_evidence" if top else "no_viable_candidate_evidence"
    return PolicyEvidenceSet(top_candidates=top, rejected_candidates=tuple(rejected), reason=reason)

def decide_workflow_action(
    *,
    workflow_context: WorkflowContext,
    candidates: list[ActionCandidate] | tuple[ActionCandidate, ...] | None = None,
    explicit_action: str = "",
    tool_result_action: str = "",
    experience_actions: list[ActionCandidate] | tuple[ActionCandidate, ...] | None = None,
) -> SWSDDecision:
    """Pick one SWSD action from advisory candidates."""
    allowed = set(workflow_context.allowed_transitions or ())
    if tool_result_action:
        if not allowed or tool_result_action in allowed:
            return SWSDDecision(tool_result_action, 1.0, reason="tool_result_priority")
        return SWSDDecision(
            "",
            0.0,
            rejected_candidates=(ActionCandidate(tool_result_action, 1.0, source="tool_result"),),
            requires_confirmation=True,
            reason="tool_result_action_not_allowed",
        )
    if explicit_action:
        if not allowed or explicit_action in allowed:
            return SWSDDecision(explicit_action, 1.0, reason="explicit_action_priority")
        return SWSDDecision("", 0.0, requires_confirmation=True, reason="explicit_action_not_allowed")

    evidence_set = build_policy_evidence(
        workflow_context=workflow_context,
        candidates=candidates,
        experience_actions=experience_actions,
        top_n=1,
    )
    rejected: list[ActionCandidate] = []
    for evidence in evidence_set.rejected_candidates:
        if evidence.candidate is not None:
            rejected.append(evidence.candidate)
    for evidence in evidence_set.top_candidates:
        candidate = evidence.candidate
        if candidate is None:
            continue
        return SWSDDecision(
            candidate.action,
            evidence.confidence,
            accepted_candidates=(candidate,),
            rejected_candidates=tuple(rejected),
            requires_confirmation=evidence.confidence < 0.75,
            reason=evidence.reason,
        )
    return SWSDDecision(
        "",
        0.0,
        rejected_candidates=tuple(rejected),
        requires_confirmation=bool(tuple(candidates or ()) + tuple(experience_actions or ())),
        reason="no_candidate_accepted" if (candidates or experience_actions) else "no_candidates",
    )

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
