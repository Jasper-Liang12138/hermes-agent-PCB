"""Workflow-history compression for SWSD context packages."""

from __future__ import annotations

from typing import Any, Iterable


def summarize_events(events: Iterable[dict[str, Any]], max_events: int = 12) -> str:
    items = list(events or [])[-max_events:]
    if not items:
        return ""
    lines: list[str] = []
    for event in items:
        event_type = str(event.get("event_type") or event.get("type") or "event")
        from_state = str(event.get("from_state") or "")
        to_state = str(event.get("to_state") or "")
        intent = str(event.get("intent") or "")
        label = f"{event_type}"
        if from_state or to_state:
            label += f" {from_state}->{to_state}".strip()
        if intent:
            label += f" intent={intent}"
        lines.append(f"- {label}")
    return "\n".join(lines)
