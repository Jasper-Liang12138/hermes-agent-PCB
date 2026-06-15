"""Core SWSD workflow graph data structures."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional


class ActionType(str, Enum):
    NORMAL = "normal"
    USER_JUMP = "user_jump"
    FALLBACK = "fallback"
    ROLLBACK = "rollback"
    CANCEL = "cancel"
    TOOL = "tool"
    OBSERVATION = "observation"


@dataclass(frozen=True)
class StateDef:
    name: str
    description: str = ""
    allowed_tools: tuple[str, ...] = ()
    recommended_tools: tuple[str, ...] = ()
    forbidden_tools: tuple[str, ...] = ()


@dataclass(frozen=True)
class Transition:
    from_state: str
    to_state: str
    intent: str
    action_type: ActionType = ActionType.NORMAL
    description: str = ""


@dataclass(frozen=True)
class WorkflowDef:
    workflow_id: str
    description: str
    initial_state: str
    terminal_states: tuple[str, ...] = ("idle",)
    states: Dict[str, StateDef] = field(default_factory=dict)
    transitions: tuple[Transition, ...] = ()

    def state(self, name: str) -> Optional[StateDef]:
        return self.states.get(name)

    def validate(self) -> None:
        if self.initial_state not in self.states:
            raise ValueError(f"workflow {self.workflow_id}: missing initial state {self.initial_state}")
        missing: list[str] = []
        for transition in self.transitions:
            if transition.from_state not in self.states:
                missing.append(transition.from_state)
            if transition.to_state not in self.states:
                missing.append(transition.to_state)
        if missing:
            raise ValueError(f"workflow {self.workflow_id}: transition references unknown states {sorted(set(missing))}")

    def next_transition(
        self,
        current_state: str,
        intent: str,
        action_type: ActionType | None = None,
    ) -> Optional[Transition]:
        for transition in self.transitions:
            if transition.from_state != current_state:
                continue
            if transition.intent != intent:
                continue
            if action_type is not None and transition.action_type != action_type:
                continue
            return transition
        return None


@dataclass
class WorkflowEvent:
    event_type: str
    from_state: str = ""
    to_state: str = ""
    intent: str = ""
    action_type: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    model_stage: str = ""


@dataclass
class Checkpoint:
    checkpoint_id: str
    state: str
    label: str
    payload: Dict[str, Any] = field(default_factory=dict)
    event_id: int | None = None


@dataclass
class WorkflowSessionState:
    session_id: str
    workflow_id: str
    current_state: str
    payload: Dict[str, Any] = field(default_factory=dict)
    checkpoints: List[Checkpoint] = field(default_factory=list)
    history_summary: str = ""

    def compact_payload(self, max_chars: int = 5000) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in self.payload.items():
            if key in {"projectData", "boardText", "rawBoardData", "dropped_board_data"}:
                text = str(value or "")
                result[key] = f"[omitted {len(text)} chars]"
                continue
            text = str(value)
            result[key] = value if len(text) <= max_chars else f"{text[:max_chars]}...[truncated]"
        return result


def states(items: Iterable[StateDef]) -> Dict[str, StateDef]:
    return {item.name: item for item in items}
