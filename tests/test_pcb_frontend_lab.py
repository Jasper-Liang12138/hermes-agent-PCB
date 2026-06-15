from __future__ import annotations

import ast
from pathlib import Path

from pcb_frontend_lab.runner import (
    CaseRuntime,
    collect_actual,
    evaluate_case,
    make_tool_result,
    make_user_message,
    safe_file_stem,
    tool_result_for_call,
)


def test_runner_does_not_import_agent_modules():
    source = Path("pcb_frontend_lab/runner.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = ("gateway", "tools", "run_agent", "model_tools", "toolsets")

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)

    assert not [name for name in imports if name.split(".")[0] in forbidden]


def test_message_builders_use_frontend_protocol():
    msg = make_user_message("sess-1", "proj-1", "hello")

    assert msg == {
        "sessionId": "sess-1",
        "projectid": "proj-1",
        "type": "message",
        "body": {"role": "user", "content": "hello"},
    }

    result = make_tool_result("call-1", {"success": True}, "sess-1", "proj-1")
    assert result["sessionId"] == "sess-1"
    assert result["projectid"] == "proj-1"
    assert result["type"] == "tool-results"
    assert result["body"]["role"] == "tool"
    assert result["body"]["content"] == {"id": "call-1", "result": {"success": True}}


def test_safe_file_stem_removes_path_characters():
    assert safe_file_stem("fanout/basic:U27") == "fanout_basic_U27"
    assert safe_file_stem("...") == "case"


def test_tool_result_for_call_supports_lists_and_call_numbers():
    runtime = CaseRuntime(
        case={
            "tool_results": {
                "getProjectData#1": "board-1",
                "importLines": [{"success": True}, {"success": False}],
            }
        },
        case_id="case",
        session_id="sess",
        project_id="proj",
    )

    runtime.tool_calls.append({"id": "call-1", "name": "getProjectData"})
    assert tool_result_for_call(runtime, "getProjectData", {"id": "call-1"}) == "board-1"

    runtime.tool_calls.append({"id": "call-2", "name": "importLines"})
    assert tool_result_for_call(runtime, "importLines", {"id": "call-2"}) == {"success": True}

    runtime.tool_calls.append({"id": "call-3", "name": "importLines"})
    assert tool_result_for_call(runtime, "importLines", {"id": "call-3"}) == {"success": False}


def test_evaluate_case_checks_protocol_observable_results():
    runtime = CaseRuntime(case={}, case_id="case", session_id="sess", project_id="proj")
    runtime.frames.extend(
        [
            {
                "direction": "agent_to_frontend",
                "time": 1.0,
                "frame": {
                    "type": "tool-calls",
                    "body": {"role": "agent", "content": {"id": "call-1", "name": "getProjectData", "arguments": {}}},
                },
                "summary": {},
            },
            {
                "direction": "agent_to_frontend",
                "time": 2.0,
                "frame": {
                    "type": "message",
                    "body": {"role": "agent", "content": "请确认", "isFinal": True, "fanoutParams": "{}"},
                },
                "summary": {},
            },
        ]
    )

    actual = collect_actual(runtime)
    assert actual["tool_calls"] == ["getProjectData"]
    assert "fanoutParams" in actual["body_fields"]

    failures = evaluate_case(
        {
            "expect": {
                "tool_calls": ["getProjectData"],
                "body_fields": ["fanoutParams"],
                "no_error": True,
                "tool_call_arguments": [{"name": "getProjectData", "arguments": {}}],
            }
        },
        runtime,
    )

    assert failures == []


def test_evaluate_case_reports_missing_fields_and_unexpected_import():
    runtime = CaseRuntime(case={}, case_id="case", session_id="sess", project_id="proj")
    runtime.frames.append(
        {
            "direction": "agent_to_frontend",
            "time": 1.0,
            "frame": {
                "type": "tool-calls",
                "body": {"role": "agent", "content": {"id": "call-1", "name": "importLines"}},
            },
            "summary": {},
        }
    )

    failures = evaluate_case(
        {"expect": {"no_importLines": True, "body_fields": ["fanoutParams"]}},
        runtime,
    )

    assert {item["type"] for item in failures} == {"unexpected_importLines", "missing_body_field"}
