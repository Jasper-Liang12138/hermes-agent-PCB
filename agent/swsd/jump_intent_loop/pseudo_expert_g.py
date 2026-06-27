"""Rule pseudo-expert G for jump plan validation and voting."""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from agent.swsd.registry import get_workflow

from .models import RetrievedJumpPrior, WorkflowJumpPlan


ALLOWED_JUMP_ACTIONS = {
    "modify_params",
    "modify_router_choice",
    "change_target",
    "rerun_fanout",
    "rollback_checkpoint",
    "confirm_route",
    "confirm_import",
    "reject_import",
    "reroute_entry",
    "pcb_entry",
    "resume_workflow",
    "clarify",
}


def clean_jump_plan(
    raw: Any,
    *,
    workflow_id: str,
    from_state: str,
    prior: RetrievedJumpPrior,
    user_text: str = "",
) -> tuple[WorkflowJumpPlan | None, str]:
    if not isinstance(raw, dict):
        return None, "jump plan is not an object"
    candidate_workflow = str(_first(raw, "workflow_id", "workflowId", "workflow") or workflow_id or "").strip()
    target_obj = raw.get("target") if isinstance(raw.get("target"), dict) else {}
    action = str(
        _first(raw, "action", "intent", "jump_action", "jumpAction", "accepted_action", "acceptedAction", "workflow_action", "workflowAction")
        or target_obj.get("action")
        or ""
    ).strip()
    raw_state_value = str(_first(raw, "state") or target_obj.get("state") or "").strip()
    raw_target_state = str(_first(raw, "target_state", "targetState") or target_obj.get("target_state") or "").strip()
    raw_state_was_current = bool(raw_state_value and raw_state_value == from_state and not raw_target_state)
    target_state = str(raw_target_state or raw_state_value or "").strip()
    requires_clarification = _as_bool(raw.get("requires_clarification", raw.get("requiresClarification", False)))
    debug: dict[str, Any] = {}
    if requires_clarification and not action:
        action = "clarify"
    if not action:
        repaired = _repair_missing_action(
            user_text=user_text,
            workflow_id=candidate_workflow,
            target_state=target_state,
            current_workflow_id=workflow_id,
            current_state=from_state,
            raw_state_was_current=raw_state_was_current,
        )
        if repaired:
            action, candidate_workflow, target_state = repaired
            debug["model_repaired"] = True
            debug["repair_reason"] = "missing action repaired from user_text and current-state-only output"
    if action not in ALLOWED_JUMP_ACTIONS:
        if not action:
            return None, "missing action. Output must include action and target_state; do not only output current workflow/state."
        return None, f"unsupported jump action: {action}. Allowed actions: {', '.join(sorted(ALLOWED_JUMP_ACTIONS))}"
    if candidate_workflow not in {"pcb_escape_flow", "pcb_reroute_flow"}:
        return None, f"unsupported workflow: {candidate_workflow}"
    workflow = get_workflow(candidate_workflow)
    if workflow is None:
        return None, f"unknown workflow: {candidate_workflow}"
    if action == "clarify" and not target_state:
        target_state = from_state
    if target_state not in workflow.states:
        return None, f"target_state is not in workflow graph: {target_state}"
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", raw.get("score", 0.0)))))
    except (TypeError, ValueError):
        confidence = 0.0
    entities = raw.get("entities") if isinstance(raw.get("entities"), dict) else {}
    return (
        WorkflowJumpPlan(
            workflow_id=candidate_workflow,
            from_state=str(_first(raw, "from_state", "fromState") or from_state or "").strip(),
            action=action,
            target_state=target_state,
            confidence=confidence,
            entities=dict(entities),
            reason=str(raw.get("reason") or raw.get("why") or "").strip(),
            requires_clarification=requires_clarification,
            clarification=str(raw.get("clarification") or "").strip(),
            retrieved_prior={"path": prior.path, "title": prior.title, "score": prior.score},
            debug=debug,
        ),
        "",
    )


def choose_vote(
    votes: list[WorkflowJumpPlan],
    *,
    required_agreement: int = 4,
) -> tuple[WorkflowJumpPlan | None, str]:
    if not votes:
        return None, "no valid jump votes"
    counts = Counter(vote.vote_key() for vote in votes)
    key, count = counts.most_common(1)[0]
    tied = [item for item, item_count in counts.items() if item_count == count]
    if len(tied) > 1:
        return None, "jump vote tie"
    if len(votes) >= 5 and count < required_agreement:
        return None, "jump vote majority below required agreement"
    for vote in votes:
        if vote.vote_key() == key:
            return vote, "jump vote accepted"
    return None, "jump vote not found"


def _first(raw: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = raw.get(key)
        if value not in (None, ""):
            return value
    return None


def _repair_missing_action(
    *,
    user_text: str,
    workflow_id: str,
    target_state: str,
    current_workflow_id: str,
    current_state: str,
    raw_state_was_current: bool,
) -> tuple[str, str, str] | None:
    text = str(user_text or "")
    lower_text = text.lower()
    reroute_hit = _contains_any(lower_text, ("拆线重布", "reroute", "rip-up", "ripup", "删线重布"))
    fanout_hit = _contains_any(lower_text, ("fanout", "扇出", "逃逸", "布线"))
    change_target_hit = _contains_any(lower_text, ("重新选择", "换bga", "换 bga", "换目标", "重新选")) or bool(re.search(r"选择\s*[A-Za-z]+\d+", text, flags=re.IGNORECASE))
    rerun_hit = _contains_any(lower_text, ("重新fanout", "重新 fanout", "重新扇出", "重新布线", "重跑", "rerun"))
    modify_hit = _contains_any(lower_text, ("改线宽", "改线距", "线宽", "线距", "改参数", "修改参数", "routertype", "router type", "换算法", "换策略"))
    if raw_state_was_current and target_state == current_state:
        if reroute_hit:
            return "reroute_entry", "pcb_reroute_flow", "rip_up"
        if current_workflow_id == "pcb_reroute_flow" and fanout_hit:
            return "pcb_entry", "pcb_escape_flow", "select_bga"
        if change_target_hit:
            return "change_target", "pcb_escape_flow", "select_bga"
        if rerun_hit:
            return "rerun_fanout", "pcb_escape_flow", "layer_assign_escape_order"
        if modify_hit:
            return "modify_params", "pcb_escape_flow", "layer_assign_escape_order"
    if target_state == "layer_assign_escape_order":
        if rerun_hit:
            return "rerun_fanout", workflow_id, target_state
        if modify_hit:
            return "modify_params", workflow_id, target_state
    if target_state == "select_bga" and change_target_hit:
        return "change_target", workflow_id, target_state
    if target_state == "select_bga" and fanout_hit:
        return "pcb_entry", "pcb_escape_flow", target_state
    if workflow_id == "pcb_reroute_flow" and target_state == "rip_up" and reroute_hit:
        return "reroute_entry", workflow_id, target_state
    return None


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes", "1", "需要", "clarify", "澄清"}
    return bool(value)
