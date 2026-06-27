"""Runner for the SWSD jump intent loop."""

from __future__ import annotations

from typing import Any

from .models import JumpIntentLoopInput, JumpIntentLoopResult, WorkflowJumpPlan
from .pseudo_expert_g import choose_vote, clean_jump_plan
from .retriever import DEFAULT_NO_PRIOR_CLARIFICATION, retrieve_jump_prior
from .tool_planning_chat_jump_model import ToolPlanningChatJumpModel


def run_jump_intent_loop(
    request: JumpIntentLoopInput,
    *,
    model: Any = None,
    docs_root: str | None = None,
    max_attempts: int = 10,
    valid_vote_target: int = 5,
    required_agreement: int = 4,
) -> JumpIntentLoopResult:
    prior = retrieve_jump_prior(
        user_text=request.user_text,
        workflow_id=request.workflow_id,
        workflow_state=request.workflow_state,
        docs_root=docs_root,
        candidate_action=request.candidate_action,
        entities=request.entities,
    )
    if prior is None:
        return JumpIntentLoopResult(
            accepted=False,
            clarification=DEFAULT_NO_PRIOR_CLARIFICATION,
            reason="no_jump_prior",
        )
    expert = model or ToolPlanningChatJumpModel()
    votes: list[WorkflowJumpPlan] = []
    feedback: tuple[str, ...] = ()
    invalid_rounds = 0
    invalid_feedback: list[str] = []
    raw_outputs: list[dict[str, Any]] = []
    attempts = max(1, max_attempts)
    while attempts > 0 and len(votes) < valid_vote_target:
        attempts -= 1
        try:
            raw = expert.propose_jump_plan(request, prior, feedback=feedback)
        except Exception as exc:
            raw = {}
            feedback = (f"expert_g_error: {exc}",)
        raw_trace = {}
        clean_raw = raw
        if isinstance(raw, dict):
            raw_trace = {"raw_output": raw.get("__raw_output", ""), "meta": raw.get("__meta", {})}
            clean_raw = {key: value for key, value in raw.items() if not str(key).startswith("__")}
        plan, error = clean_jump_plan(clean_raw, workflow_id=request.workflow_id, from_state=request.workflow_state, prior=prior, user_text=request.user_text)
        if plan is None:
            invalid_rounds += 1
            feedback = (error or "invalid jump plan",)
            invalid_feedback.append(feedback[0])
            raw_outputs.append({**raw_trace, "parsed": clean_raw, "validation_error": feedback[0]})
            continue
        raw_outputs.append({**raw_trace, "parsed": clean_raw, "validation_error": "", "vote_key": plan.vote_key(), "debug": plan.debug})
        votes.append(plan)
        feedback = ()
    selected, reason = choose_vote(votes, required_agreement=required_agreement)
    if selected is None:
        return JumpIntentLoopResult(
            accepted=False,
            clarification=DEFAULT_NO_PRIOR_CLARIFICATION,
            prior=prior,
            valid_votes=tuple(votes),
            invalid_rounds=invalid_rounds,
            invalid_feedback=tuple(invalid_feedback),
            raw_outputs=tuple(raw_outputs),
            reason=reason,
        )
    if selected.requires_clarification:
        return JumpIntentLoopResult(
            accepted=False,
            plan=selected,
            clarification=selected.clarification or DEFAULT_NO_PRIOR_CLARIFICATION,
            prior=prior,
            valid_votes=tuple(votes),
            invalid_rounds=invalid_rounds,
            invalid_feedback=tuple(invalid_feedback),
            raw_outputs=tuple(raw_outputs),
            reason="jump_requires_clarification",
        )
    return JumpIntentLoopResult(
        accepted=True,
        plan=selected,
        prior=prior,
        valid_votes=tuple(votes),
        invalid_rounds=invalid_rounds,
        invalid_feedback=tuple(invalid_feedback),
        raw_outputs=tuple(raw_outputs),
        reason=reason,
    )
