"""Resolve PCB memory/model/skill experience into runtime hints."""

from __future__ import annotations

from typing import Any

from agent.swsd.experience.model import load_project_model, model_to_hints
from agent.swsd.experience.schema import PCBContextHints, PCBExperienceHint
from agent.swsd.experience.skill_bank import procedural_hints_from_skills


class PCBExperienceResolver:
    def __init__(self, db: Any = None) -> None:
        self.db = db

    def resolve(
        self,
        *,
        session_id: str,
        project_id: str = "",
        query: str = "",
        workflow_id: str = "",
        workflow_state: str = "idle",
        limit: int = 6,
    ) -> PCBContextHints:
        memory_hints = self._memory_hints(session_id, workflow_id, limit=limit)
        model_hints = tuple(model_to_hints(load_project_model(project_id)))
        skill_hints = tuple(
            PCBExperienceHint(
                layer="procedural_skill",
                key=str(item.get("operation") or item.get("source") or "skill"),
                value=item,
                source=str(item.get("source") or ""),
                confidence=float(item.get("score") or 0.5),
                reason="Retrieved PCB procedural skill grounding.",
            )
            for item in procedural_hints_from_skills(query, workflow_state, limit=3)
        )
        influenced: list[str] = []
        if memory_hints:
            influenced.append("state_recovery")
        if model_hints:
            influenced.append("defaults_and_output_contract")
        if skill_hints:
            influenced.append("procedural_recovery")
        return PCBContextHints(
            session_id=session_id,
            project_id=project_id,
            memory_hints=tuple(memory_hints),
            model_hints=model_hints,
            skill_hints=skill_hints,
            decisions_influenced=tuple(influenced),
        )

    def _memory_hints(self, session_id: str, workflow_id: str, limit: int) -> list[PCBExperienceHint]:
        if not self.db or not session_id:
            return []
        hints: list[PCBExperienceHint] = []
        try:
            events = self.db.list_workflow_events(session_id, workflow_id=workflow_id or None, limit=limit)
        except Exception:
            return []
        for event in events:
            payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
            if event.get("event_type") != "experience" and payload.get("kind") not in {"body_fields", "target_resolution", "final_fields", "fanout_version"}:
                continue
            signals = payload.get("signals") if isinstance(payload.get("signals"), dict) else payload
            raw_key = str(payload.get("kind") or event.get("intent") or "workflow_fact")
            key = "fanoutVersionHistory" if raw_key == "fanout_version" else raw_key
            hints.append(
                PCBExperienceHint(
                    layer="memory_fact",
                    key=key,
                    value=signals,
                    source=str(payload.get("source") or "workflow_events"),
                    confidence=float(payload.get("confidence") or 0.75),
                    reason=str(payload.get("summary") or event.get("action_type") or "Recent PCB workflow experience."),
                )
            )
        return hints


def build_experience_context_block(
    *,
    db: Any,
    session_id: str,
    project_id: str = "",
    query: str = "",
    workflow_id: str = "",
    workflow_state: str = "idle",
) -> str:
    return PCBExperienceResolver(db).resolve(
        session_id=session_id,
        project_id=project_id,
        query=query,
        workflow_id=workflow_id,
        workflow_state=workflow_state,
    ).to_prompt_block()
