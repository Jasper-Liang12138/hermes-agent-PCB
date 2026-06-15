"""Conservative PCB workflow trace distillation hooks."""

from __future__ import annotations

from typing import Any


def should_distill_trace(events: list[dict[str, Any]]) -> bool:
    """Return True when a workflow trace is useful enough for future skill work."""

    if len(events or []) < 3:
        return False
    intents = {str(event.get("intent") or "") for event in events}
    return bool({"route_complete", "reroute_result", "final_response", "fanout_params_generated"} & intents)


def summarize_trace_for_skill(events: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for event in (events or [])[-12:]:
        intent = str(event.get("intent") or event.get("event_type") or "").strip()
        action = str(event.get("action_type") or "").strip()
        if intent or action:
            parts.append(f"- {intent or 'event'}: {action or 'observed'}")
    return "\n".join(parts)
