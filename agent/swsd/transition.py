"""SWSD transition policy."""

from __future__ import annotations

from agent.swsd.graph import ActionType, Transition
from agent.swsd.registry import get_workflow


def transition_for(
    workflow_id: str,
    current_state: str,
    intent: str,
    *,
    fallback_state: str = "idle",
) -> Transition:
    workflow = get_workflow(workflow_id)
    if workflow is None:
        return Transition(current_state, fallback_state, intent, ActionType.FALLBACK, "unknown workflow")
    if intent == "cancel":
        return Transition(current_state, "idle", intent, ActionType.CANCEL, "user cancelled workflow")
    if intent == "rollback":
        return Transition(current_state, current_state, intent, ActionType.ROLLBACK, "rollback requested")
    transition = workflow.next_transition(current_state, intent)
    if transition:
        return transition
    if intent in {"change_target", "select_target"} and "select_bga" in workflow.states:
        return Transition(current_state, "select_bga", intent, ActionType.USER_JUMP, "jump to BGA selection")
    return Transition(current_state, current_state, intent, ActionType.NORMAL, "no state change")
