"""Controlled PCB intent loops used by the SWSD controller.

The loops deliberately do not inherit from ``AIAgent``. They provide a small,
side-effect-free lifecycle around the same intent model role: propose
candidate actions, vote on confidence, repair rejected candidates, and produce
a readable fallback prompt when arbitration fails.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor

from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.swsd.action_candidates import ActionCandidate, IntentCandidateSet
from agent.swsd.decision_policy import ActionEvidence, PolicyEvidenceSet, SWSDDecision, WorkflowContext, build_policy_evidence, decide_workflow_action


@dataclass(frozen=True)
class IntentAgentLoopInput:
    user_text: str
    workflow_id: str
    workflow_state: str
    allowed_actions: tuple[str, ...]
    explicit_fields: dict[str, Any] = field(default_factory=dict)
    hints: dict[str, Any] = field(default_factory=dict)
    fallback_candidates: tuple[ActionCandidate, ...] = ()
    explicit_action: str = ""
    experience_actions: tuple[ActionCandidate, ...] = ()
    session_id: str = ""
    project_id: str = ""


@dataclass(frozen=True)
class IntentAgentLoopResult:
    candidate_set: IntentCandidateSet
    policy: SWSDDecision
    accepted: bool
    final_action: str = ""
    feedback_reply: str | None = None
    stage: str = ""
    rejection_feedback: tuple[str, ...] = ()
    votes: tuple[str, ...] = ()
    debug: dict[str, Any] = field(default_factory=dict)


class IntentModelProtocol(Protocol):
    """Side-effect-free intent model contract shared by experts A/B/C."""

    def propose_candidates(self, request: IntentAgentLoopInput, feedback: tuple[str, ...] = ()) -> IntentCandidateSet:
        ...

    def vote_action(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        evidence_set: PolicyEvidenceSet,
        *,
        model_stage: str = "tool_planning_chat",
        negative_feedback: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        ...

    def refine_evidence(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        evidence_set: PolicyEvidenceSet,
        rejection_feedback: tuple[str, ...],
    ) -> PolicyEvidenceSet:
        ...

    def judge_candidates(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        policy_feedback: str = "",
    ) -> bool:
        ...

    def revise_candidates(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        rejection_feedback: tuple[str, ...],
    ) -> IntentCandidateSet:
        ...

    def build_feedback_reply(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        rejection_feedback: tuple[str, ...],
    ) -> str:
        ...


class LocalRuleIntentModel:
    """Default intent model adapter until an LLM-backed adapter is configured.

    It keeps realtime routing deterministic by using the controller's current
    rule candidates as the expert A proposal and applies strict JSON-like
    candidate validation without performing any tool side effects.
    """

    def propose_candidates(self, request: IntentAgentLoopInput, feedback: tuple[str, ...] = ()) -> IntentCandidateSet:
        candidates = request.fallback_candidates or self._rule_candidates(request)
        if not candidates:
            candidates = (ActionCandidate("chat", 0.8, reason="no intent model candidates", source="default"),)
        return IntentCandidateSet(
            workflow=request.workflow_id,
            current_state=request.workflow_state,
            candidate_actions=tuple(candidates),
            model_source="local_rule_intent_model",
        )

    def _rule_candidates(self, request: IntentAgentLoopInput) -> tuple[ActionCandidate, ...]:
        text = request.user_text or ""
        state = request.workflow_state or "idle"
        allowed = set(request.allowed_actions or ())
        candidates: list[ActionCandidate] = []

        def add(action: str, confidence: float, *, entities: dict[str, Any] | None = None, reason: str = "") -> None:
            if allowed and action not in allowed:
                return
            candidates.append(ActionCandidate(action, confidence, entities or {}, reason, "local_rule_intent_model"))

        if re.search(r"回到上一步|上一步|rollback|撤回|退回", text, flags=re.IGNORECASE):
            add("rollback_checkpoint", 0.97, reason="local rollback signal")
        if re.search(r"拆线重布|reroute|ripup|rip-up|删除.*(?:走线|线|trace|traces)", text, flags=re.IGNORECASE):
            add("reroute_entry", 0.96, reason="local reroute signal")
        if re.search(r"\b(arc|135|rl|ga|auto)\b|北科大|遗传|自动", text, flags=re.IGNORECASE):
            add("layer_assigned", 0.9, entities={"routerText": text}, reason="local router choice signal")
            add("modify_params", 0.86, entities={"routerText": text}, reason="local router modify signal")
        if re.search(r"线宽|线距|spacing|width|mil|参数|改成|修改", text, flags=re.IGNORECASE):
            add("modify_params", 0.84, entities={"rawText": text}, reason="local parameter modify signal")
        if re.search(r"fanout|扇出|逃逸|布线|BGA", text, flags=re.IGNORECASE) and state in {"idle", "select_bga"}:
            add("pcb_entry", 0.9, reason="local fanout entry signal")
        if not candidates:
            add("chat", 0.8, reason="local default chat")
        return tuple(candidates)

    def vote_action(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        evidence_set: PolicyEvidenceSet,
        *,
        model_stage: str = "tool_planning_chat",
        negative_feedback: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        if not evidence_set.top_candidates:
            return {}
        return {
            "selected_action": evidence_set.top_candidates[0].action,
            "confidence": evidence_set.top_candidates[0].confidence,
            "reason": "local_rule_vote_highest_evidence",
        }

    def refine_evidence(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        evidence_set: PolicyEvidenceSet,
        rejection_feedback: tuple[str, ...],
    ) -> PolicyEvidenceSet:
        return evidence_set

    def judge_candidates(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        policy_feedback: str = "",
    ) -> bool:
        policy = _policy_for(request, candidate_set)
        return bool(policy.action and not policy.requires_confirmation)

    def revise_candidates(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        rejection_feedback: tuple[str, ...],
    ) -> IntentCandidateSet:
        allowed = set(request.allowed_actions)
        repaired = tuple(
            candidate
            for candidate in candidate_set.candidate_actions
            if candidate.action and (not allowed or candidate.action in allowed)
        )
        if not repaired:
            repaired = (ActionCandidate("chat", 0.75, reason="arbitration fallback", source="local_rule_intent_model"),)
        return IntentCandidateSet(
            workflow=request.workflow_id,
            current_state=request.workflow_state,
            candidate_actions=repaired,
            model_source=candidate_set.model_source,
        )

    def build_feedback_reply(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        rejection_feedback: tuple[str, ...],
    ) -> str:
        if request.workflow_state != "idle":
            return "我还不能确定要执行哪一步，请补充目标器件、router 类型，或回复“确认/取消”。"
        return "我还不能确定你想进行哪类 PCB 操作，请补充目标器件或具体操作。"


@dataclass
class BaseSWSDIntentLoop:
    intent_model: IntentModelProtocol
    max_rounds: int = 3

    def validate_candidate_set(self, candidate_set: IntentCandidateSet, request: IntentAgentLoopInput) -> tuple[bool, tuple[str, ...]]:
        errors: list[str] = []
        if not candidate_set.workflow:
            errors.append("missing workflow")
        if not candidate_set.current_state:
            errors.append("missing currentState")
        if not candidate_set.candidate_actions:
            errors.append("empty candidateActions")
        allowed = set(request.allowed_actions)
        for index, candidate in enumerate(candidate_set.candidate_actions):
            if not candidate.action:
                errors.append(f"candidate[{index}] missing action")
            if candidate.confidence < 0.0 or candidate.confidence > 1.0:
                errors.append(f"candidate[{index}] confidence out of range")
        return not errors, tuple(errors)



def _workflow_context_for(request: IntentAgentLoopInput) -> WorkflowContext:
    return WorkflowContext(
        workflow_state=request.workflow_state,
        current_node=request.workflow_state,
        allowed_transitions=request.allowed_actions,
        tool_context={
            "workflow_id": request.workflow_id,
            "session_id": request.session_id,
            "project_id": request.project_id,
            "experience_hints": request.hints,
            "user_text": request.user_text,
        },
    )


def _policy_evidence_for(request: IntentAgentLoopInput, candidate_set: IntentCandidateSet) -> PolicyEvidenceSet:
    return build_policy_evidence(
        workflow_context=_workflow_context_for(request),
        candidates=candidate_set.candidate_actions,
        experience_actions=request.experience_actions,
        top_n=3,
    )


def _candidate_for_action(candidate_set: IntentCandidateSet, request: IntentAgentLoopInput, action: str) -> ActionCandidate | None:
    for candidate in tuple(candidate_set.candidate_actions) + tuple(request.experience_actions):
        if candidate.action == action:
            return candidate
    return None


def _policy_from_vote(request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, action: str, confidence: float, reason: str) -> SWSDDecision:
    candidate = _candidate_for_action(candidate_set, request, action) or ActionCandidate(action, confidence, reason=reason, source="expert_b_vote")
    return SWSDDecision(
        action=action,
        confidence=max(0.0, min(1.0, confidence)),
        accepted_candidates=(candidate,),
        requires_confirmation=False,
        reason=reason or "expert_b_vote_majority",
    )


def _clean_vote(raw_vote: Any, evidence_set: PolicyEvidenceSet) -> tuple[str, float, str] | None:
    if not isinstance(raw_vote, dict):
        return None
    action = str(
        raw_vote.get("selected_action")
        or raw_vote.get("selectedAction")
        or raw_vote.get("action")
        or raw_vote.get("vote")
        or raw_vote.get("accepted_action")
        or ""
    ).strip()
    allowed = {item.action for item in evidence_set.top_candidates if not item.hard_reject}
    if action not in allowed:
        return None
    try:
        confidence = float(raw_vote.get("confidence", raw_vote.get("score", 0.0)))
    except (TypeError, ValueError):
        confidence = 0.0
    reason = str(raw_vote.get("reason") or raw_vote.get("why") or "").strip()
    return action, max(0.0, min(1.0, confidence)), reason


def _vote_counts(votes: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for action in votes:
        counts[action] = counts.get(action, 0) + 1
    return counts

def _policy_for(request: IntentAgentLoopInput, candidate_set: IntentCandidateSet) -> SWSDDecision:
    return decide_workflow_action(
        workflow_context=_workflow_context_for(request),
        candidates=candidate_set.candidate_actions,
        explicit_action=request.explicit_action,
        experience_actions=request.experience_actions,
    )


def agent_proposal_loop(request: IntentAgentLoopInput, intent_model: IntentModelProtocol, *, max_rounds: int = 3) -> tuple[IntentCandidateSet, tuple[str, ...]]:
    loop = BaseSWSDIntentLoop(intent_model=intent_model, max_rounds=max_rounds)
    feedback: tuple[str, ...] = ()
    candidate_set = IntentCandidateSet(request.workflow_id, request.workflow_state, ())
    for _ in range(max_rounds):
        candidate_set = intent_model.propose_candidates(request, feedback)
        ok, feedback = loop.validate_candidate_set(candidate_set, request)
        if ok:
            return candidate_set, ()
    return candidate_set, feedback





def _run_vote_lane(
    request: IntentAgentLoopInput,
    candidate_set: IntentCandidateSet,
    intent_model: IntentModelProtocol,
    evidence_set: PolicyEvidenceSet,
    *,
    model_stage: str,
    rounds: int,
    max_calls: int,
) -> tuple[list[str], list[str], list[dict[str, Any]], int]:
    votes: list[str] = []
    feedback: list[str] = []
    raw_votes: list[dict[str, Any]] = []
    invalid_votes = 0
    call_index = 0
    while len(votes) < rounds and call_index < max_calls:
        raw_vote = intent_model.vote_action(
            request,
            candidate_set,
            evidence_set,
            model_stage=model_stage,
            negative_feedback=tuple(feedback),
        )
        raw_votes.append({"stage": model_stage, "vote": raw_vote})
        cleaned = _clean_vote(raw_vote, evidence_set)
        call_index += 1
        if cleaned is None:
            invalid_votes += 1
            feedback.append(f"invalid_{model_stage}_expert_b_vote_format")
            continue
        action, _confidence, _reason = cleaned
        votes.append(action)
    return votes, feedback, raw_votes, invalid_votes


def _run_expert_b_vote(request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, intent_model: IntentModelProtocol, evidence_set: PolicyEvidenceSet, *, max_rounds: int = 6, max_calls: int = 10, majority: int = 4) -> tuple[SWSDDecision, bool, tuple[str, ...], tuple[str, ...], dict[str, Any]]:
    if not evidence_set.top_candidates:
        policy = _policy_for(request, candidate_set)
        return policy, False, (), (evidence_set.reason,), {"top3_evidence": [], "invalid_votes": 0, "vote_counts": {}}

    lane_rounds = max(1, max_rounds // 2)
    lane_max_calls = max(lane_rounds, max_calls // 2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                _run_vote_lane,
                request,
                candidate_set,
                intent_model,
                evidence_set,
                model_stage="tool_planning_chat",
                rounds=lane_rounds,
                max_calls=lane_max_calls,
            ),
            executor.submit(
                _run_vote_lane,
                request,
                candidate_set,
                intent_model,
                evidence_set,
                model_stage="reroute",
                rounds=max_rounds - lane_rounds,
                max_calls=max_calls - lane_max_calls,
            ),
        ]
        lane_results = [future.result() for future in futures]

    votes: list[str] = []
    feedback: list[str] = []
    raw_votes: list[dict[str, Any]] = []
    invalid_votes = 0
    for lane_votes, lane_feedback, lane_raw_votes, lane_invalid_votes in lane_results:
        votes.extend(lane_votes)
        feedback.extend(lane_feedback)
        raw_votes.extend(lane_raw_votes)
        invalid_votes += lane_invalid_votes

    counts = _vote_counts(votes)
    winner = max(counts, key=counts.get) if counts else ""
    accepted = bool(winner and counts[winner] >= majority and len(votes) >= max_rounds)
    policy = _policy_from_vote(request, candidate_set, winner, counts.get(winner, 0) / max_rounds, "expert_b_vote_majority") if winner else _policy_for(request, candidate_set)
    return policy, accepted, tuple(votes), tuple(feedback or (() if accepted else ("expert_b_no_majority",))), {
        "top3_evidence": [_action_evidence_dict(item) for item in evidence_set.top_candidates],
        "expert_b_votes": raw_votes,
        "vote_counts": counts,
        "invalid_votes": invalid_votes,
    }

def agent_confidence_loop(request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, intent_model: IntentModelProtocol, *, max_rounds: int = 6, max_calls: int = 10, majority: int = 4) -> tuple[SWSDDecision, bool, tuple[str, ...], tuple[str, ...], dict[str, Any]]:
    return _run_expert_b_vote(
        request,
        candidate_set,
        intent_model,
        _policy_evidence_for(request, candidate_set),
        max_rounds=max_rounds,
        max_calls=max_calls,
        majority=majority,
    )

def agent_arbit_loop(request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, intent_model: IntentModelProtocol, rejection_feedback: tuple[str, ...], *, max_rounds: int = 1) -> tuple[IntentCandidateSet, SWSDDecision, bool, tuple[str, ...], tuple[str, ...], dict[str, Any]]:
    feedback = tuple(rejection_feedback)
    evidence_set = _policy_evidence_for(request, candidate_set)
    debug: dict[str, Any] = {"arbit_rounds": []}
    votes: tuple[str, ...] = ()
    last_policy = _policy_for(request, candidate_set)
    evidence_set = intent_model.refine_evidence(request, candidate_set, evidence_set, feedback)
    policy, accepted, votes, vote_feedback, vote_debug = _run_expert_b_vote(request, candidate_set, intent_model, evidence_set)
    debug["arbit_rounds"].append({"round": 1, "vote_debug": vote_debug})
    last_policy = policy
    if accepted and policy.action:
        return candidate_set, policy, True, feedback + vote_feedback, votes, debug
    feedback = feedback + vote_feedback + (policy.reason or "expert_b_no_majority",)
    return candidate_set, last_policy, False, feedback, votes, debug


def _action_evidence_dict(item: ActionEvidence) -> dict[str, Any]:
    return {
        "action": item.action,
        "confidence": item.confidence,
        "evidence": list(item.evidence),
        "risk": item.risk,
        "hard_reject": item.hard_reject,
        "reason": item.reason,
    }

def agent_feedback_loop(request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, intent_model: IntentModelProtocol, rejection_feedback: tuple[str, ...], *, max_rounds: int = 3) -> str:
    reply = ""
    for _ in range(max_rounds):
        reply = intent_model.build_feedback_reply(request, candidate_set, rejection_feedback).strip()
        if 4 <= len(reply) <= 240:
            return reply
    return reply or "我还需要更多信息才能继续。"


def run_pcb_intent_agent_loops(request: IntentAgentLoopInput, intent_model: IntentModelProtocol | None = None) -> IntentAgentLoopResult:
    model = intent_model or LocalRuleIntentModel()
    candidate_set, proposal_errors = agent_proposal_loop(request, model)
    if proposal_errors:
        reply = agent_feedback_loop(request, candidate_set, model, proposal_errors)
        return IntentAgentLoopResult(candidate_set, _policy_for(request, candidate_set), False, feedback_reply=reply, stage="proposal", rejection_feedback=proposal_errors)

    policy, accepted, votes, feedback, vote_debug = agent_confidence_loop(request, candidate_set, model)
    if accepted and policy.action:
        return IntentAgentLoopResult(candidate_set, policy, True, final_action=policy.action, stage="confidence", rejection_feedback=feedback, votes=votes, debug=vote_debug)

    revised_set, revised_policy, arbit_accepted, arbit_feedback, arbit_votes, arbit_debug = agent_arbit_loop(request, candidate_set, model, feedback)
    if arbit_accepted and revised_policy.action:
        return IntentAgentLoopResult(revised_set, revised_policy, True, final_action=revised_policy.action, stage="arbit", rejection_feedback=arbit_feedback, votes=arbit_votes, debug={**vote_debug, **arbit_debug})

    reply = agent_feedback_loop(request, revised_set, model, arbit_feedback)
    return IntentAgentLoopResult(revised_set, revised_policy, False, feedback_reply=reply, stage="feedback", rejection_feedback=arbit_feedback, votes=arbit_votes or votes, debug={**vote_debug, **arbit_debug})
