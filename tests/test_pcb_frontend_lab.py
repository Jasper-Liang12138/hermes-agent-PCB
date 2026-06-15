from __future__ import annotations

import ast
from pathlib import Path

from pcb_frontend_lab.runner import (
    DEFAULT_REROUTE_BOARD_PATH,
    CaseRuntime,
    LabConfig,
    _case_reroute_mock_mode,
    _parse_reroute_board_fixture,
    _build_dynamic_reroute_tool_result,
    collect_actual,
    evaluate_case,
    is_tool_progress_message,
    make_tool_result,
    make_user_message,
    safe_file_stem,
    tool_result_for_call,
    transcript_frame,
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


def test_evaluate_case_required_tool_calls_allows_extra_tools():
    runtime = CaseRuntime(case={}, case_id="case", session_id="sess", project_id="proj")
    runtime.frames.append(
        {
            "direction": "agent_to_frontend",
            "time": 1.0,
            "frame": {
                "type": "tool-calls",
                "body": {"role": "agent", "content": {"id": "call-1", "name": "deleteTracesForRerouting"}},
            },
            "summary": {},
        }
    )
    runtime.frames.append(
        {
            "direction": "agent_to_frontend",
            "time": 2.0,
            "frame": {
                "type": "tool-calls",
                "body": {"role": "agent", "content": {"id": "call-2", "name": "importLines"}},
            },
            "summary": {},
        }
    )

    failures = evaluate_case(
        {"expect": {"required_tool_calls": ["deleteTracesForRerouting"]}},
        runtime,
    )

    assert failures == []


def test_tool_progress_message_is_not_terminal_when_it_has_no_result_fields():
    progress = {
        "type": "message",
        "body": {
            "msgId": "msg-1",
            "role": "agent",
            "content": "⚙️ deleteTracesForRerouting...",
            "isFinal": True,
        },
    }
    result_like = {
        "type": "message",
        "body": {
            "msgId": "msg-1",
            "role": "agent",
            "content": "⚙️ reroute...",
            "isFinal": True,
            "rerouteResult": {"drcPassed": True},
        },
    }

    assert is_tool_progress_message(progress) is True
    assert is_tool_progress_message(result_like) is False


def test_transcript_frame_omits_large_tool_results():
    frame = make_tool_result("call-1", "x" * 1200, "sess", "proj")

    compact = transcript_frame(frame)

    result = compact["body"]["content"]["result"]
    assert result["__omitted_large_tool_result__"] is True
    assert result["length"] == 1200
    assert len(result["preview"]) == 500


def test_case_reroute_mock_mode_prefers_case_override():
    config = LabConfig(ws_url="ws://x", cases_path=Path("cases.jsonl"), out_path=Path("out.jsonl"), reroute_mock_mode="off")

    assert _case_reroute_mock_mode({"labels": {"reroute_mock": True}}, config) == "dynamic-segment-delete"
    assert _case_reroute_mock_mode({"reroute_mock_mode": "dynamic-segment-delete"}, config) == "dynamic-segment-delete"
    assert _case_reroute_mock_mode({}, LabConfig(ws_url="ws://x", cases_path=Path("cases.jsonl"), out_path=Path("out.jsonl"), reroute_mock_mode="dynamic-segment-delete")) == "dynamic-segment-delete"


def test_parse_reroute_board_fixture_extracts_first_lineseg():
    board_text = """(layout
(wires
    (wire
        (net "NET_A")
        (path
            (lineseg
                (pt 10 20)
                (w 3)
            )
            (lineseg
                (pt 10 40)
                (w 3)
            )
            (props)
            (layer "Conductor/Top")
        )
    )
)
)"""

    payload = _parse_reroute_board_fixture(board_text)

    assert payload["selectedNets"] == ["NET_A"]
    assert payload["selectedTraceIds"] == []
    assert payload["missing_routes"][0]["start"] == {"layer": "Conductor/Top", "x": 10.0, "y": 20.0}
    assert payload["missing_routes"][0]["end"] == {"layer": "Conductor/Top", "x": 10.0, "y": 40.0}
    assert "(lineseg" in payload["projectData"]
    assert '"NET_A"' in payload["projectData"]
    assert payload["localContext"]["source"] == "pcb_frontend_lab_mock"


def test_dynamic_reroute_tool_result_uses_configured_board(tmp_path):
    board = tmp_path / "board.txt"
    board.write_text(
        """(layout
(wires
    (wire
        (net "NET_MOCK")
        (path
            (lineseg
                (pt 1 2)
                (w 3)
            )
            (lineseg
                (pt 1 5)
                (w 3)
            )
            (props)
            (layer "Conductor/Bottom")
        )
    )
)
)""",
        encoding="utf-8",
    )
    runtime = CaseRuntime(case={}, case_id="case", session_id="sess", project_id="proj")
    config = LabConfig(
        ws_url="ws://x",
        cases_path=Path("cases.jsonl"),
        out_path=Path("out.jsonl"),
        reroute_board_path=board,
        reroute_mock_mode="dynamic-segment-delete",
    )

    payload = _build_dynamic_reroute_tool_result(runtime, config)

    assert payload["selectedNets"] == ["NET_MOCK"]
    assert payload["mockSource"] == "dynamic-segment-delete"
    assert payload["boardFile"] == str(board)
    assert payload["originalBoardDataFilePath"] == str(board)
    assert Path(payload["projectData"]).is_absolute()
    assert payload["projectData"].endswith("_reroute_drop_1.txt")
    assert payload["projectDataFilePath"] == payload["projectData"]
    assert Path(payload["projectData"]).is_file()
    assert '"NET_MOCK"' in Path(payload["projectData"]).read_text(encoding="utf-8")
    assert payload["droppedBoardDataFilePath"] == payload["projectData"]
    assert payload["missingRoutes"] == payload["missing_routes"]
    assert payload["droppedObjects"][0]["net"] == "NET_MOCK"
    assert runtime.mock_artifacts["reroute_mock"]["board_path"] == str(board)
    assert runtime.mock_artifacts["reroute_mock"]["dropped_board_path"] == payload["projectData"]
    assert runtime.mock_artifacts["reroute_mock"]["deleted_segment"]["net"] == "NET_MOCK"
