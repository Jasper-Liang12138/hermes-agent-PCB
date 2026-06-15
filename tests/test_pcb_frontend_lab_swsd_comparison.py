from __future__ import annotations

import sqlite3

from pcb_frontend_lab.run_swsd_comparison import (
    compare_protocol,
    inspect_swsd_db,
    summarize_lab_results,
)


def test_summarize_lab_results_keeps_protocol_observable_fields():
    summary = summarize_lab_results(
        [
            {
                "id": "case-a",
                "passed": True,
                "actual": {
                    "tool_calls": ["getProjectData"],
                    "body_fields": ["content", "selection"],
                    "error_count": 0,
                },
                "failures": [],
            },
            {
                "id": "case-b",
                "passed": False,
                "actual": {"tool_calls": [], "body_fields": ["content"], "error_count": 1},
                "failures": [{"type": "unexpected_error"}],
            },
        ]
    )

    assert summary["total"] == 2
    assert summary["passed"] == 1
    assert summary["failed"] == 1
    assert summary["cases"]["case-a"]["tool_calls"] == ["getProjectData"]
    assert summary["cases"]["case-b"]["failures"] == [{"type": "unexpected_error"}]


def test_compare_protocol_reports_only_observable_differences():
    baseline = {
        "cases": {
            "same": {"tool_calls": [], "body_fields": ["content"], "error_count": 0},
            "diff": {"tool_calls": ["getProjectData"], "body_fields": ["selection"], "error_count": 0},
        }
    }
    swsd = {
        "cases": {
            "same": {"tool_calls": [], "body_fields": ["content"], "error_count": 0},
            "diff": {"tool_calls": ["getProjectData"], "body_fields": ["selection", "fanoutParams"], "error_count": 0},
        }
    }

    assert compare_protocol(baseline, swsd) == [
        {
            "case": "diff",
            "type": "body_fields_diff",
            "baseline": ["selection"],
            "swsd": ["selection", "fanoutParams"],
        }
    ]


def test_inspect_swsd_db_counts_workflow_tables(tmp_path):
    db_path = tmp_path / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE workflow_sessions (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                workflow_id TEXT,
                current_state TEXT,
                state_payload TEXT,
                history_summary TEXT,
                created_at REAL,
                updated_at REAL
            );
            CREATE TABLE workflow_events (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                workflow_id TEXT,
                event_type TEXT,
                from_state TEXT,
                to_state TEXT,
                intent TEXT,
                action_type TEXT,
                payload TEXT,
                model_stage TEXT,
                timestamp REAL
            );
            CREATE TABLE workflow_checkpoints (
                id INTEGER PRIMARY KEY,
                session_id TEXT,
                workflow_id TEXT,
                checkpoint_id TEXT,
                state TEXT,
                label TEXT,
                payload TEXT,
                event_id INTEGER,
                created_at REAL
            );
            INSERT INTO workflow_sessions
                (session_id, workflow_id, current_state, state_payload, history_summary, created_at, updated_at)
                VALUES ('s1', 'pcb_escape_flow', 'review', '{}', '', 1, 2);
            INSERT INTO workflow_events
                (session_id, workflow_id, event_type, from_state, to_state, intent, action_type, payload, model_stage, timestamp)
                VALUES ('s1', 'pcb_escape_flow', 'state_sync', 'select_bga', 'routing', '', '', '{}', '', 1);
            INSERT INTO workflow_checkpoints
                (session_id, workflow_id, checkpoint_id, state, label, payload, event_id, created_at)
                VALUES ('s1', 'pcb_escape_flow', 'ckpt_1', 'routing', 'routing result', '{}', 1, 1);
            """
        )

    result = inspect_swsd_db(db_path)

    assert result["db_exists"] is True
    assert result["sessions"] == 1
    assert result["events"] == 1
    assert result["checkpoints"] == 1
    assert result["states_by_workflow"] == {"pcb_escape_flow": {"routing": 1}}
