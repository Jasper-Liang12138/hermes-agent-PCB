from __future__ import annotations

from hermes_state import SessionDB


def test_workflow_state_checkpoint_roundtrip(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        db.upsert_workflow_state(
            "sess",
            "pcb_escape_flow",
            "select_bga",
            {"selectedBGA": "U22"},
        )
        event_id = db.append_workflow_event(
            session_id="sess",
            workflow_id="pcb_escape_flow",
            event_type="state_update",
            from_state="idle",
            to_state="select_bga",
            intent="pcb_entry",
            action_type="normal",
            payload={"ok": True},
            model_stage="tool_planning_chat",
        )
        db.write_workflow_checkpoint(
            session_id="sess",
            workflow_id="pcb_escape_flow",
            checkpoint_id="c1",
            state="select_bga",
            label="BGA analysis",
            payload={"selectedBGA": "U22"},
            event_id=event_id,
        )

        state = db.get_workflow_state("sess", "pcb_escape_flow")
        checkpoints = db.list_workflow_checkpoints("sess", "pcb_escape_flow")
        events = db.list_workflow_events("sess", "pcb_escape_flow")

        assert state["current_state"] == "select_bga"
        assert state["state_payload"]["selectedBGA"] == "U22"
        assert checkpoints[0]["checkpoint_id"] == "c1"
        assert checkpoints[0]["payload"]["selectedBGA"] == "U22"
        assert events[0]["model_stage"] == "tool_planning_chat"

        db.upsert_workflow_state("sess", "pcb_escape_flow", "routing", {"selectedBGA": "U23"})
        rolled = db.rollback_workflow_checkpoint("sess", "pcb_escape_flow", checkpoint_id="c1")

        assert rolled["current_state"] == "select_bga"
        assert rolled["state_payload"]["selectedBGA"] == "U22"
    finally:
        db.close()
