"""Persistent SWSD state manager backed by SessionDB."""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, Optional

from agent.swsd.graph import WorkflowEvent
from agent.swsd.registry import get_workflow


class WorkflowStateManager:
    def __init__(self, db=None, *, persist: bool = True):
        self.db = db
        self.persist = persist and db is not None
        self._memory: dict[tuple[str, str], dict[str, Any]] = {}
        self._memory_checkpoints: dict[tuple[str, str], list[dict[str, Any]]] = {}

    def load(self, session_id: str, workflow_id: str | None = None) -> Optional[dict[str, Any]]:
        if self.persist and hasattr(self.db, "get_workflow_state"):
            state = self.db.get_workflow_state(session_id, workflow_id=workflow_id)
            if state:
                return state
        if workflow_id:
            return self._memory.get((session_id, workflow_id))
        candidates = [value for (sid, _wid), value in self._memory.items() if sid == session_id]
        return candidates[-1] if candidates else None

    def start(
        self,
        session_id: str,
        workflow_id: str,
        *,
        initial_state: str | None = None,
        payload: Dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        workflow = get_workflow(workflow_id)
        if workflow is None:
            raise ValueError(f"unknown workflow: {workflow_id}")
        current_state = initial_state or workflow.initial_state
        return self.update(session_id, workflow_id, current_state=current_state, payload=payload or {})

    def update(
        self,
        session_id: str,
        workflow_id: str,
        *,
        current_state: str,
        payload: Dict[str, Any] | None = None,
        merge: bool = True,
    ) -> dict[str, Any]:
        existing = self.load(session_id, workflow_id) or {}
        state_payload: Dict[str, Any] = {}
        if merge and isinstance(existing.get("state_payload"), dict):
            state_payload.update(existing["state_payload"])
        if payload:
            state_payload.update(payload)
        record = {
            "session_id": session_id,
            "workflow_id": workflow_id,
            "current_state": current_state,
            "state_payload": state_payload,
            "updated_at": time.time(),
        }
        self._memory[(session_id, workflow_id)] = record
        if self.persist and hasattr(self.db, "upsert_workflow_state"):
            self.db.upsert_workflow_state(session_id, workflow_id, current_state, state_payload)
        return record

    def append_event(
        self,
        session_id: str,
        workflow_id: str,
        event: WorkflowEvent,
    ) -> int | None:
        if self.persist and hasattr(self.db, "append_workflow_event"):
            return self.db.append_workflow_event(
                session_id=session_id,
                workflow_id=workflow_id,
                event_type=event.event_type,
                from_state=event.from_state,
                to_state=event.to_state,
                intent=event.intent,
                action_type=event.action_type,
                payload=event.payload,
                model_stage=event.model_stage,
            )
        return None

    def checkpoint(
        self,
        session_id: str,
        workflow_id: str,
        *,
        state: str,
        label: str,
        payload: Dict[str, Any] | None = None,
        event_id: int | None = None,
    ) -> str:
        checkpoint_id = f"ckpt_{uuid.uuid4().hex[:12]}"
        checkpoint_record = {
            "session_id": session_id,
            "workflow_id": workflow_id,
            "checkpoint_id": checkpoint_id,
            "state": state,
            "label": label,
            "payload": payload or {},
            "event_id": event_id,
            "created_at": time.time(),
        }
        self._memory_checkpoints.setdefault((session_id, workflow_id), []).append(checkpoint_record)
        if self.persist and hasattr(self.db, "write_workflow_checkpoint"):
            self.db.write_workflow_checkpoint(
                session_id=session_id,
                workflow_id=workflow_id,
                checkpoint_id=checkpoint_id,
                state=state,
                label=label,
                payload=payload or {},
                event_id=event_id,
            )
        return checkpoint_id

    def rollback(self, session_id: str, workflow_id: str, checkpoint_id: str | None = None) -> Optional[dict[str, Any]]:
        if self.persist and hasattr(self.db, "rollback_workflow_checkpoint"):
            return self.db.rollback_workflow_checkpoint(session_id, workflow_id, checkpoint_id=checkpoint_id)
        checkpoints = list(self._memory_checkpoints.get((session_id, workflow_id), ()))
        if not checkpoints:
            return self.load(session_id, workflow_id)
        if checkpoint_id:
            target = next((item for item in checkpoints if item.get("checkpoint_id") == checkpoint_id), None)
        else:
            target = checkpoints[-2] if len(checkpoints) >= 2 else checkpoints[-1]
        if not target:
            return self.load(session_id, workflow_id)
        return self.update(
            session_id,
            workflow_id,
            current_state=str(target.get("state") or ""),
            payload=dict(target.get("payload") or {}),
            merge=False,
        )

    def latest_checkpoint(self, session_id: str, workflow_id: str) -> Optional[dict[str, Any]]:
        if self.persist and hasattr(self.db, "list_workflow_checkpoints"):
            checkpoints = self.db.list_workflow_checkpoints(session_id, workflow_id=workflow_id)
            return checkpoints[-1] if checkpoints else None
        checkpoints = self._memory_checkpoints.get((session_id, workflow_id), ())
        if checkpoints:
            return checkpoints[-1]
        state = self.load(session_id, workflow_id)
        if not state:
            return None
        return {
            "session_id": session_id,
            "workflow_id": workflow_id,
            "state": state.get("current_state") or "",
            "payload": state.get("state_payload") or {},
        }
