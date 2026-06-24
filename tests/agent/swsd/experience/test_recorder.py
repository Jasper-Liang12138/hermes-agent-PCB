import logging

from agent.swsd.experience.recorder import PCBExperienceRecorder
from agent.swsd.experience.schema import PCBExperienceEvent


def test_experience_record_failure_is_warning_and_best_effort(caplog):
    class DB:
        def append_workflow_event(self, **kwargs):
            raise RuntimeError("db down")

    event = PCBExperienceEvent(
        kind="body_fields",
        session_id="s",
        project_id="p",
        workflow_id="pcb_escape_flow",
        stage="review",
        outcome="observed",
        summary="summary",
    )

    with caplog.at_level(logging.WARNING):
        PCBExperienceRecorder(DB()).record(event)

    assert "PCB experience record skipped" in caplog.text
    assert "db down" in caplog.text
