"""Controlled PCB intent loops used by the SWSD controller.

The loops deliberately do not inherit from ``AIAgent``. They provide a small,
side-effect-free lifecycle around the same intent model role: propose
candidate actions, vote on confidence, repair rejected candidates, and produce
a readable fallback prompt when arbitration fails.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from agent.swsd.action_candidates import ActionCandidate, IntentCandidateSet
from agent.swsd.decision_policy import SWSDDecision, WorkflowContext, decide_workflow_action


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
    votes: tuple[bool, ...] = ()


class IntentModelProtocol(Protocol):
    """Side-effect-free intent model contract shared by experts A/B/C."""

    def propose_candidates(self, request: IntentAgentLoopInput, feedback: tuple[str, ...] = ()) -> IntentCandidateSet:
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
        candidates = request.fallback_candidates or (ActionCandidate("chat", 0.8, reason="no intent model candidates", source="default"),)
        return IntentCandidateSet(
            workflow=request.workflow_id,
            current_state=request.workflow_state,
            candidate_actions=tuple(candidates),
            model_source="local_rule_intent_model",
        )

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


def _policy_for(request: IntentAgentLoopInput, candidate_set: IntentCandidateSet) -> SWSDDecision:
    return decide_workflow_action(
        workflow_context=WorkflowContext(
            workflow_state=request.workflow_state,
            current_node=request.workflow_state,
            allowed_transitions=request.allowed_actions,
            tool_context={
                "workflow_id": request.workflow_id,
                "session_id": request.session_id,
                "project_id": request.project_id,
                "experience_hints": request.hints,
            },
        ),
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


def agent_confidence_loop(request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, intent_model: IntentModelProtocol, *, max_rounds: int = 3) -> tuple[SWSDDecision, bool, tuple[bool, ...], tuple[str, ...]]:
    votes: list[bool] = []
    feedback: list[str] = []
    last_policy = _policy_for(request, candidate_set)
    for _ in range(max_rounds):
        last_policy = _policy_for(request, candidate_set)
        pseudo_accept = bool(last_policy.action and not last_policy.requires_confirmation)
        votes.append(pseudo_accept)
        if not pseudo_accept:
            feedback.append(last_policy.reason or "decision_policy_rejected")
        expert_accept = intent_model.judge_candidates(request, candidate_set, last_policy.reason)
        votes.append(bool(expert_accept))
        if len(votes) >= 6 and sum(1 for vote in votes if vote) >= 5:
            return last_policy, True, tuple(votes), tuple(feedback)
    return last_policy, sum(1 for vote in votes if vote) >= 5, tuple(votes), tuple(feedback)


def agent_arbit_loop(request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, intent_model: IntentModelProtocol, rejection_feedback: tuple[str, ...], *, max_rounds: int = 3) -> tuple[IntentCandidateSet, SWSDDecision, bool, tuple[str, ...]]:
    feedback = tuple(rejection_feedback)
    current = candidate_set
    last_policy = _policy_for(request, current)
    for _ in range(max_rounds):
        current = intent_model.revise_candidates(request, current, feedback)
        last_policy = _policy_for(request, current)
        if last_policy.action and not last_policy.requires_confirmation:
            return current, last_policy, True, feedback
        feedback = feedback + (last_policy.reason or "decision_policy_rejected",)
    return current, last_policy, False, feedback


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

    policy, accepted, votes, feedback = agent_confidence_loop(request, candidate_set, model)
    if accepted and policy.action:
        return IntentAgentLoopResult(candidate_set, policy, True, final_action=policy.action, stage="confidence", rejection_feedback=feedback, votes=votes)

    revised_set, revised_policy, arbit_accepted, arbit_feedback = agent_arbit_loop(request, candidate_set, model, feedback)
    if arbit_accepted and revised_policy.action:
        return IntentAgentLoopResult(revised_set, revised_policy, True, final_action=revised_policy.action, stage="arbit", rejection_feedback=arbit_feedback, votes=votes)

    reply = agent_feedback_loop(request, revised_set, model, arbit_feedback)
    return IntentAgentLoopResult(revised_set, revised_policy, False, feedback_reply=reply, stage="feedback", rejection_feedback=arbit_feedback, votes=votes)
