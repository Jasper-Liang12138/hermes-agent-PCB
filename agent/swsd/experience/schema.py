"""Schemas for PCB workflow experience hints."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any


def _compact(value: Any, max_chars: int = 1200) -> Any:
    if isinstance(value, dict):
        return {str(key): _compact(item, max_chars=max_chars) for key, item in value.items()}
    if isinstance(value, list):
        return [_compact(item, max_chars=max_chars) for item in value[:20]]
    text = str(value)
    if len(text) > max_chars:
        return f"{text[:max_chars]}...[truncated]"
    return value


@dataclass(frozen=True)
class PCBExperienceEvent:
    kind: str
    session_id: str
    project_id: str = ""
    workflow_id: str = ""
    stage: str = ""
    outcome: str = ""
    summary: str = ""
    signals: dict[str, Any] = field(default_factory=dict)
    source: str = "runtime"
    confidence: float = 1.0
    timestamp: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sessionId": self.session_id,
            "projectId": self.project_id,
            "workflowId": self.workflow_id,
            "stage": self.stage,
            "outcome": self.outcome,
            "summary": self.summary,
            "signals": _compact(self.signals),
            "source": self.source,
            "confidence": round(float(self.confidence), 3),
            "timestamp": self.timestamp,
        }


@dataclass(frozen=True)
class PCBExperienceHint:
    layer: str
    key: str
    value: Any
    source: str = ""
    confidence: float = 1.0
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "key": self.key,
            "value": _compact(self.value),
            "source": self.source,
            "confidence": round(float(self.confidence), 3),
            "reason": self.reason,
        }


@dataclass(frozen=True)
class PCBProjectModel:
    interaction_preference: dict[str, Any] = field(default_factory=dict)
    pcb_preference: dict[str, Any] = field(default_factory=dict)
    project_aliases: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "interactionPreference": self.interaction_preference,
            "pcbPreference": self.pcb_preference,
            "projectAliases": self.project_aliases,
        }


@dataclass(frozen=True)
class PCBContextHints:
    session_id: str
    project_id: str = ""
    memory_hints: tuple[PCBExperienceHint, ...] = ()
    model_hints: tuple[PCBExperienceHint, ...] = ()
    skill_hints: tuple[PCBExperienceHint, ...] = ()
    decisions_influenced: tuple[str, ...] = ()

    @property
    def experience_used(self) -> bool:
        return bool(self.memory_hints or self.model_hints or self.skill_hints)

    def hint_value(self, key: str, default: Any = None) -> Any:
        for hint in (*self.memory_hints, *self.model_hints, *self.skill_hints):
            if hint.key == key:
                return hint.value
        return default

    def as_dict(self) -> dict[str, Any]:
        return {
            "experienceUsed": self.experience_used,
            "sessionId": self.session_id,
            "projectId": self.project_id,
            "memoryHints": [hint.as_dict() for hint in self.memory_hints],
            "modelHints": [hint.as_dict() for hint in self.model_hints],
            "skillHints": [hint.as_dict() for hint in self.skill_hints],
            "decisionsInfluenced": list(self.decisions_influenced),
        }

    def to_prompt_block(self) -> str:
        if not self.experience_used:
            return ""
        return (
            "## PCB Experience Hints\n"
            "These are runtime hints from PCB workflow memory, project model, and procedural skills. "
            "Use them as recovery/default guidance only; do not override explicit user input or SWSD hard constraints.\n"
            f"{json.dumps(self.as_dict(), ensure_ascii=False, indent=2)}"
        )
