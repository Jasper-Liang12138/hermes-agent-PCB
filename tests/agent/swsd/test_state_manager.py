from agent.swsd.state_manager import WorkflowStateManager


def test_memory_checkpoints_are_capped():
    manager = WorkflowStateManager(persist=False, max_memory_checkpoints=3)
    manager.update("s", "pcb_escape_flow", current_state="select_bga", payload={"i": 0})

    for index in range(5):
        manager.checkpoint("s", "pcb_escape_flow", state="review", label=f"ckpt-{index}", payload={"i": index})

    checkpoints = manager._memory_checkpoints[("s", "pcb_escape_flow")]
    assert len(checkpoints) == 3
    assert [item["payload"]["i"] for item in checkpoints] == [2, 3, 4]


def test_clear_session_removes_memory_state_and_checkpoints():
    manager = WorkflowStateManager(persist=False)
    manager.update("s", "pcb_escape_flow", current_state="review", payload={"a": 1})
    manager.update("s", "pcb_reroute_flow", current_state="report", payload={"b": 2})
    manager.checkpoint("s", "pcb_escape_flow", state="review", label="one", payload={"a": 1})

    manager.clear_session("s", "pcb_escape_flow")

    assert manager.load("s", "pcb_escape_flow") is None
    assert manager.load("s", "pcb_reroute_flow")["current_state"] == "report"
    assert ("s", "pcb_escape_flow") not in manager._memory_checkpoints


def test_persist_rollback_updates_memory_cache():
    class DB:
        def rollback_workflow_checkpoint(self, session_id, workflow_id, checkpoint_id=None):
            return {
                "session_id": session_id,
                "workflow_id": workflow_id,
                "current_state": "report",
                "state_payload": {"from": "db"},
            }

    manager = WorkflowStateManager(DB(), persist=True)
    state = manager.rollback("s", "pcb_reroute_flow")

    assert state["current_state"] == "report"
    assert manager._memory[("s", "pcb_reroute_flow")]["state_payload"] == {"from": "db"}


def test_validate_entry_allows_fanout_bootstrap_and_rejects_wrong_action():
    manager = WorkflowStateManager(persist=False)

    allowed = manager.validate_entry("pcb_escape_flow", "idle", "pcb_entry")
    rejected = manager.validate_entry("pcb_escape_flow", "review", "reroute_entry")

    assert allowed.valid is True
    assert "pcb_entry" in allowed.allowed_actions
    assert rejected.valid is False
    assert "not allowed" in rejected.reason


def test_record_step_merges_payload_and_checkpoint():
    manager = WorkflowStateManager(persist=False)
    manager.update("s", "pcb_escape_flow", current_state="select_bga", payload={"projectData": {"status": "old"}})

    state = manager.record_step(
        "s",
        "pcb_escape_flow",
        state="select_bga",
        step_id="get_project_data",
        payload={"projectData": {"status": "requested"}, "targetBGAs": ["U5"]},
        checkpoint_label="getProjectData requested",
    )

    assert state["state_payload"]["step_id"] == "get_project_data"
    assert state["state_payload"]["projectData"]["status"] == "requested"
    assert state["state_payload"]["targetBGAs"] == ["U5"]
    assert manager.latest_checkpoint("s", "pcb_escape_flow")["state"] == "select_bga"