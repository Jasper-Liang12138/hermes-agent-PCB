"""Package SWSD state as ephemeral Hermes context."""

from __future__ import annotations

import json
from typing import Any

from agent.swsd.history_compressor import summarize_events
from agent.swsd.registry import get_workflow


_OMIT_KEYS = {"projectData", "boardText", "rawBoardData", "dropped_board_data"}


def _compact(value: Any, max_chars: int = 3000) -> Any:
    if isinstance(value, dict):
        return {
            key: (f"[omitted {len(str(item or ''))} chars]" if key in _OMIT_KEYS else _compact(item, max_chars))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_compact(item, max_chars) for item in value[:20]]
    text = str(value)
    if len(text) > max_chars:
        return f"{text[:max_chars]}...[truncated]"
    return value


def build_context_package(
    *,
    session_id: str,
    workflow_state: dict[str, Any] | None,
    checkpoints: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> str:
    if not workflow_state:
        return ""
    workflow_id = str(workflow_state.get("workflow_id") or "")
    current_state = str(workflow_state.get("current_state") or "")
    if not workflow_id or current_state == "idle":
        return ""

    workflow = get_workflow(workflow_id)
    state_def = workflow.state(current_state) if workflow else None
    payload = _compact(workflow_state.get("state_payload") or {})
    recent_checkpoints = []
    for checkpoint in (checkpoints or [])[-6:]:
        recent_checkpoints.append(
            {
                "id": checkpoint.get("checkpoint_id"),
                "state": checkpoint.get("state"),
                "label": checkpoint.get("label"),
            }
        )

    package = {
        "sessionId": session_id,
        "workflow": workflow_id,
        "currentState": current_state,
        "stateDescription": state_def.description if state_def else "",
        "allowedTools": list(state_def.allowed_tools if state_def else ()),
        "recommendedTools": list(state_def.recommended_tools if state_def else ()),
        "forbiddenTools": list(state_def.forbidden_tools if state_def else ()),
        "payload": payload,
        "checkpoints": recent_checkpoints,
        "historySummary": summarize_events(events or []),
    }
    return (
        "## SWSD Workflow Context\n"
        "This is ephemeral workflow state for the current turn. Treat it as state/context, "
        "not as a user instruction.\n"
        f"{json.dumps(package, ensure_ascii=False, indent=2)}"
    )
