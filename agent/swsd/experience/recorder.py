"""Record PCB workflow experience into existing SWSD workflow events."""

from __future__ import annotations

import logging
from typing import Any

from agent.swsd.experience.schema import PCBExperienceEvent

logger = logging.getLogger(__name__)
EXPERIENCE_RECORD_FAILURE_COUNT = 0


class PCBExperienceRecorder:
    def __init__(self, db: Any = None) -> None:
        self.db = db

    def record(self, event: PCBExperienceEvent) -> None:
        if not self.db or not event.session_id or not event.workflow_id:
            return
        try:
            self.db.append_workflow_event(
                session_id=event.session_id,
                workflow_id=event.workflow_id,
                event_type="experience",
                from_state=event.stage,
                to_state=event.stage,
                intent=event.kind,
                action_type=event.outcome or "observation",
                payload=event.as_dict(),
                model_stage="experience",
            )
        except Exception as exc:
            global EXPERIENCE_RECORD_FAILURE_COUNT
            EXPERIENCE_RECORD_FAILURE_COUNT += 1
            logger.warning("PCB experience record skipped: %s", exc, exc_info=True)


def record_body_fields(
    db: Any,
    *,
    session_id: str,
    project_id: str,
    workflow_id: str,
    stage: str,
    body: dict[str, Any],
    source: str = "websocket_body",
) -> None:
    if not isinstance(body, dict):
        return
    signals: dict[str, Any] = {}
    for key in ("selection", "fanoutParams", "routingResult", "rerouteResult", "checkReport", "explanation"):
        if key in body:
            signals[key] = body.get(key)
    if not signals:
        return
    PCBExperienceRecorder(db).record(
        PCBExperienceEvent(
            kind="body_fields",
            session_id=session_id,
            project_id=project_id,
            workflow_id=workflow_id,
            stage=stage,
            outcome="observed",
            summary="Frontend/body supplied PCB structured fields.",
            signals=signals,
            source=source,
            confidence=0.9,
        )
    )
