"""Mode guard tests for PCB tools."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from model_tools import handle_function_call
from tools import pcb_tools
from tools import pcb_reroute_drc


def _pin_csv(component: str) -> str:
    return f"PinNumber,Net\n1,{component}.NET1\n"


def _assert_route_summary(result: str, report: str, routing_path: Path, session_id: str) -> None:
    assert result.startswith("布线完成")
    assert report in result
    assert str(routing_path) in result
    pending = {
        "routingResult": str(routing_path),
        "report": report,
    }
    arc_output = routing_path.parent / "ARC_output.txt"
    line_output = routing_path.parent / "line.out"
    if arc_output.exists():
        pending["importLinesFilePath"] = str(arc_output.resolve())
    elif line_output.exists():
        pending["importLinesFilePath"] = str(line_output.resolve())
    assert pcb_tools._transport.pop_pending_pcb_fields(session_id) == pending


@pytest.fixture(autouse=True)
def _restore_transport_state():
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    prev_session = transport.current_session_id
    prev_modes = dict(transport._session_modes)
    prev_cache = dict(transport._cached_project_data)
    prev_reroute_cache = dict(transport._cached_reroute_context)
    prev_pending_fields = dict(transport._pending_pcb_fields)
    prev_adapter = transport._websocket_adapter
    prev_loop = transport._main_loop
    yield
    transport.current_session_id = prev_session
    transport._session_modes = prev_modes
    transport._cached_project_data = prev_cache
    transport._cached_reroute_context = prev_reroute_cache
    transport._pending_pcb_fields = prev_pending_fields
    transport._websocket_adapter = prev_adapter
    transport._main_loop = prev_loop


def test_get_project_data_blocked_in_chat_mode(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-chat-guard"
    transport.set_session_mode("sess-chat-guard", "chat")

    def _should_not_call(*args, **kwargs):
        raise AssertionError("call_tool_sync should not be called in chat mode")

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _should_not_call)

    result = pcb_tools.get_project_data()
    payload = json.loads(result)
    assert "error" in payload
    assert "chat" in payload["error"]


def test_get_project_data_allowed_in_pcb_mode(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-guard"
    transport.set_session_mode("sess-pcb-guard", "pcb")

    monkeypatch.setattr(
        pcb_tools._transport,
        "call_tool_sync",
        lambda tool_name, arguments, timeout=30.0, session_id=None: '(pcb_data (component (name "U27")))',
    )

    result = pcb_tools.get_project_data()
    assert '(component (name "U27"))' in result
    assert transport.get_cached_project_data() == result


def test_get_project_data_calls_frontend_without_arguments(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-empty-args"
    transport.set_session_mode("sess-pcb-empty-args", "pcb")
    seen = {}

    def _fake_call_tool_sync(tool_name, arguments, timeout=30.0, session_id=None):
        seen["tool_name"] = tool_name
        seen["arguments"] = arguments
        return "(pcb_data)"

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _fake_call_tool_sync)

    result = pcb_tools.get_project_data()
    assert result == "(pcb_data)"
    assert seen == {"tool_name": "getProjectData", "arguments": {}}


def test_route_blocked_in_chat_mode():
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-chat-route"
    transport.set_session_mode("sess-chat-route", "chat")

    result = pcb_tools.route_bga('{"orderLines":[{"net":"GND","layer":"SIG03","order":1}]}')
    payload = json.loads(result)
    assert payload["routingResult"] == ""
    assert "被拒绝" in payload["report"]


def test_route_requires_router_type_even_with_active_websocket_adapter(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-route-local"
    transport.set_session_mode("sess-pcb-route-local", "pcb")
    transport._cached_project_data["sess-pcb-route-local"] = '(pcb_data (component (name "U27")))'
    transport._websocket_adapter = object()

    def _should_not_proxy(*args, **kwargs):
        raise AssertionError("route must not be proxied to frontend")

    def _should_not_run(*args, **kwargs):
        raise AssertionError("route must require explicit routerType before running")

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _should_not_proxy)
    monkeypatch.setattr(pcb_tools.subprocess, "run", _should_not_run)
    monkeypatch.setenv("ROUTER_WORK_DIR", str(tmp_path))

    result = pcb_tools.route_bga('{"orderLines":[{"net":"GND","layer":"SIG03","order":1}],"selectedBGA":"U27","constraints":{"LineWidth":4,"LineSpacing":3}}')
    payload = json.loads(result)

    assert payload["routingResult"] == ""
    assert "缺少 routerType" in payload["report"]
    assert not (tmp_path / "版图信息.txt").exists()
    assert not (tmp_path / "order_input.txt").exists()


def test_route_appends_component_from_session_selection(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-route-selected"
    transport.set_session_mode("sess-route-selected", "pcb")
    transport._cached_project_data["sess-route-selected"] = (
        '(pcb_data (component (name "U27") (package "BGA-256")) '
        '(component (name "U35") (package "BGA-484")))'
    )
    transport._websocket_adapter = SimpleNamespace(_session_selected_targets={"sess-route-selected": "U35"})

    router_dir = tmp_path / "arc_runtime"
    router_dir.mkdir()
    for name in ("a.out", "b.out", "c.out"):
        (router_dir / name).write_text("runtime", encoding="utf-8")
    (router_dir / "Turn_QYF.py").write_text(
        "from pathlib import Path\n"
        "Path('routing_input.txt').write_text('(routes (u35))', encoding='utf-8')\n"
        "Path('data.txt').write_text('布线成功', encoding='utf-8')\n",
        encoding="utf-8",
    )

    def _fake_run(cmd, cwd, capture_output, text, encoding, errors, timeout):
        executable = Path(cmd[1]).name if Path(cmd[0]).name.startswith("python") else Path(cmd[0]).name
        if executable == "a.out":
            assert cmd[-2:] == ["layout_input.txt", "component_input.txt"]
            (tmp_path / "U35_pins.csv").write_text(_pin_csv("U35"), encoding="utf-8")
            (tmp_path / "layer_input.txt").write_text("layers", encoding="utf-8")
        elif executable == "b.out":
            assert cmd[-2:] == ["layer_input.txt", "layout_input.txt"]
        elif executable == "c.out":
            assert cmd[-4:] == ["order_input.txt", "layout_input.txt", "constrain.txt", "component_input.txt"]
            (tmp_path / "U35_pins.csv").write_text(_pin_csv("U35"), encoding="utf-8")
            (tmp_path / "ARC_output.txt").write_text("arc", encoding="utf-8")
            (tmp_path / "net_list.txt").write_text("NET_A_P_SIG03 ; J1.A1 U35.A1 ; 3.00\n", encoding="utf-8")
        elif executable == "Turn_QYF.py":
            assert cmd[-3:] == ["layout_input.txt", "ARC_output.txt", "routing_input.txt"]
        else:
            raise AssertionError(f"unexpected command: {cmd}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pcb_tools.subprocess, "run", _fake_run)
    monkeypatch.setenv("ROUTER_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("ROUTER_ARC_DIR", str(router_dir))

    result = pcb_tools.route_bga(
        json.dumps({
            "routerType": "arc",
            "orderLines": [
                {"net": "NET_A_P_SIG03", "layer": "SIG03", "order": 1},
                {"net": "NET_A_N_SIG03", "layer": "SIG03", "order": 2},
            ],
        })
    )

    _assert_route_summary(result, "布线成功", tmp_path / "routing_input.txt", "sess-route-selected")
    assert (tmp_path / "order_input.txt").read_text(encoding="utf-8") == (
        "NET_A_P_SIG03 SIG03 1\nNET_A_N_SIG03 SIG03 2\n\nU35"
    )


def test_handle_function_call_uses_explicit_session_for_get_project_data(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-wrong-global"
    transport.set_session_mode("sess-wrong-global", "chat")
    transport.set_session_mode("sess-explicit-tool", "pcb")
    seen = {}

    def _fake_call_tool_sync(tool_name, arguments, timeout=30.0, session_id=None):
        seen["tool_name"] = tool_name
        seen["arguments"] = arguments
        seen["session_id"] = session_id
        return '(pcb_data (component (name "FPGA1")))'

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _fake_call_tool_sync)

    result = handle_function_call("getProjectData", {}, session_id="sess-explicit-tool")

    assert '(component (name "FPGA1"))' in result
    assert seen == {
        "tool_name": "getProjectData",
        "arguments": {},
        "session_id": "sess-explicit-tool",
    }
    assert transport._cached_project_data["sess-explicit-tool"] == result


def test_unknown_gateway_session_falls_back_to_current_websocket_session():
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "ws-session"
    transport.set_session_mode("ws-session", "pcb")
    transport.cache_project_data("(pcb cached)", session_id="ws-session")

    assert transport.resolve_session_id("gateway-session") == "ws-session"
    assert transport.get_session_mode("gateway-session") == "pcb"
    assert transport.get_cached_project_data("gateway-session") == "(pcb cached)"


def test_handle_function_call_route_uses_explicit_session_cache(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-wrong-global"
    transport.set_session_mode("sess-wrong-global", "chat")
    transport.set_session_mode("sess-explicit-route", "pcb")
    transport._cached_project_data["sess-explicit-route"] = '(pcb_data (component (name "FPGA1")))'

    router_dir = tmp_path / "arc_runtime"
    router_dir.mkdir()
    for name in ("a.out", "b.out", "c.out"):
        (router_dir / name).write_text("runtime", encoding="utf-8")
    (router_dir / "Turn_QYF.py").write_text(
        "from pathlib import Path\n"
        "Path('routing_input.txt').write_text('(routes (fpga1))', encoding='utf-8')\n"
        "Path('data.txt').write_text('布线成功', encoding='utf-8')\n",
        encoding="utf-8",
    )

    def _fake_run(cmd, cwd, capture_output, text, encoding, errors, timeout):
        executable = Path(cmd[1]).name if Path(cmd[0]).name.startswith("python") else Path(cmd[0]).name
        if executable == "a.out":
            assert cwd == tmp_path
            (tmp_path / "FPGA1_pins.csv").write_text(_pin_csv("FPGA1"), encoding="utf-8")
            (tmp_path / "layer_input.txt").write_text("layers", encoding="utf-8")
        elif executable == "b.out":
            pass
        elif executable == "c.out":
            (tmp_path / "FPGA1_pins.csv").write_text(_pin_csv("FPGA1"), encoding="utf-8")
            (tmp_path / "ARC_output.txt").write_text("arc", encoding="utf-8")
            (tmp_path / "net_list.txt").write_text("NET_A_P_SIG03 ; J1.A1 FPGA1.A1 ; 3.00\n", encoding="utf-8")
        elif executable == "Turn_QYF.py":
            pass
        else:
            raise AssertionError(f"unexpected command: {cmd}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pcb_tools.subprocess, "run", _fake_run)
    monkeypatch.setenv("ROUTER_WORK_DIR", str(tmp_path))
    monkeypatch.setenv("ROUTER_ARC_DIR", str(router_dir))

    result = handle_function_call(
        "route",
        {
            "userData": json.dumps(
                {
                    "routerType": "arc",
                    "orderLines": [
                        {"net": "NET_A_P_SIG03", "layer": "SIG03", "order": 1},
                        {"net": "NET_A_N_SIG03", "layer": "SIG03", "order": 2},
                    ],
                    "selectedBGA": "FPGA1",
                },
                ensure_ascii=False,
            )
        },
        session_id="sess-explicit-route",
    )

    _assert_route_summary(result, "布线成功", tmp_path / "routing_input.txt", "sess-explicit-route")
    assert (tmp_path / "版图信息.txt").read_text(encoding="utf-8") == '(pcb_data (component (name "FPGA1")))'
    assert (tmp_path / "order_input.txt").read_text(encoding="utf-8") == (
        "NET_A_P_SIG03 SIG03 1\nNET_A_N_SIG03 SIG03 2\n\nFPGA1"
    )


def test_route_arc_profile_uses_readme_flow(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-arc-route"
    transport.set_session_mode("sess-arc-route", "pcb")
    transport._cached_project_data["sess-arc-route"] = '(pcb_data (component (name "U27") (package "BGA")))'

    router_dir = tmp_path / "arc_runtime"
    work_dir = tmp_path / "arc_work"
    router_dir.mkdir()
    work_dir.mkdir()
    for name in ("a.out", "b.out", "c.out"):
        (router_dir / name).write_text("runtime", encoding="utf-8")
    (router_dir / "Turn_QYF.py").write_text(
        "from pathlib import Path\n"
        "Path('routing_input.txt').write_text('(arc routed)', encoding='utf-8')\n"
        "Path('data.txt').write_text('arc ok', encoding='utf-8')\n",
        encoding="utf-8",
    )

    calls = []

    def _fake_run(cmd, cwd, capture_output, text, encoding, errors, timeout):
        calls.append([str(part) for part in cmd])
        assert cwd == work_dir
        executable = Path(cmd[1]).name if Path(cmd[0]).name.startswith("python") else Path(cmd[0]).name
        args = cmd[2:] if Path(cmd[0]).name.startswith("python") else cmd[1:]
        if executable == "a.out":
            assert args == ["layout_input.txt", "component_input.txt"]
            (work_dir / "U27_pins.csv").write_text(_pin_csv("U27"), encoding="utf-8")
            (work_dir / "layer_input.txt").write_text("layers", encoding="utf-8")
        elif executable == "b.out":
            assert args == ["layer_input.txt", "layout_input.txt"]
            (work_dir / "order_input.txt").write_text((work_dir / "order_input.txt").read_text(encoding="utf-8"), encoding="utf-8")
        elif executable == "c.out":
            assert args == ["order_input.txt", "layout_input.txt", "constrain.txt", "component_input.txt"]
            (work_dir / "U27_pins.csv").write_text(_pin_csv("U27"), encoding="utf-8")
            (work_dir / "ARC_output.txt").write_text("arc-lines", encoding="utf-8")
            (work_dir / "net_list.txt").write_text("NET_A_P_SIG03 ; J1.A1 U27.A1 ; 3.00\n", encoding="utf-8")
        elif str(cmd[1]).endswith("Turn_QYF.py"):
            assert cmd[2:] == ["layout_input.txt", "ARC_output.txt", "routing_input.txt"]
        else:
            raise AssertionError(f"unexpected command: {cmd}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pcb_tools.subprocess, "run", _fake_run)
    monkeypatch.setenv("ROUTER_WORK_DIR", str(work_dir))
    monkeypatch.setenv("ROUTER_ARC_DIR", str(router_dir))

    result = pcb_tools.route_bga(
        json.dumps({
            "routerType": "arc",
            "selectedBGA": "U27",
            "orderLines": [
                {"net": "NET_A_P_SIG03", "layer": "SIG03", "order": 1},
                {"net": "NET_A_N_SIG03", "layer": "SIG03", "order": 2},
            ],
            "constraints": {"LineWidth": 3, "LineSpacing": 4.5},
        })
    )

    _assert_route_summary(result, "arc ok", work_dir / "routing_input.txt", "sess-arc-route")
    assert (work_dir / "layout_input.txt").read_text(encoding="utf-8") == transport._cached_project_data["sess-arc-route"]
    assert (work_dir / "component_input.txt").read_text(encoding="utf-8") == "U27\n"
    assert (work_dir / "order_input.txt").read_text(encoding="utf-8") == (
        "NET_A_P_SIG03 SIG03 1\nNET_A_N_SIG03 SIG03 2\n\nU27"
    )
    assert (work_dir / "constrain.txt").read_text(encoding="utf-8") == "LineWidth:3\nLineSpacing:4.5\n"
    assert [Path(call[0]).name if not call[0].endswith("python.exe") else Path(call[1]).name for call in calls] == ["c.out"]


def test_route_135_profile_uses_readme_flow(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-135-route"
    transport.set_session_mode("sess-135-route", "pcb")
    transport._cached_project_data["sess-135-route"] = '(pcb_data (component (name "U22") (package "BGA")))'

    router_dir = tmp_path / "runtime135"
    work_dir = tmp_path / "work135"
    router_dir.mkdir()
    work_dir.mkdir()
    for name in ("d.out", "e.out", "f.out"):
        (router_dir / name).write_text("runtime", encoding="utf-8")
    (router_dir / "Turn_135_QYF.py").write_text(
        "from pathlib import Path\n"
        "Path('routing_input.txt').write_text('(135 routed)', encoding='utf-8')\n"
        "Path('data.txt').write_text('135 ok', encoding='utf-8')\n",
        encoding="utf-8",
    )

    calls = []

    def _fake_run(cmd, cwd, capture_output, text, encoding, errors, timeout):
        calls.append([str(part) for part in cmd])
        assert cwd == work_dir
        executable = Path(cmd[1]).name if Path(cmd[0]).name.startswith("python") else Path(cmd[0]).name
        args = cmd[2:] if Path(cmd[0]).name.startswith("python") else cmd[1:]
        if executable == "d.out":
            assert args == ["layout_input.txt", "component_input.txt"]
            (work_dir / "U22_pins.csv").write_text(_pin_csv("U22"), encoding="utf-8")
            (work_dir / "net_list.txt").write_text("U22.NET1; layer; 4\n", encoding="utf-8")
        elif executable == "e.out":
            assert args == ["net_list.txt", "layout_input.txt"]
            (work_dir / "order_out.txt").write_text("order", encoding="utf-8")
        elif executable == "f.out":
            assert args == ["order_out.txt", "layout_input.txt"]
            (work_dir / "line.in").write_text("line in", encoding="utf-8")
            (work_dir / "line.out").write_text("TOP!LINE!0!NET1!1!2!3!4!4\n", encoding="utf-8")
        elif str(cmd[1]).endswith("Turn_135_QYF.py"):
            assert cmd[2:] == ["layout_input.txt", "line.out", "routing_input.txt"]
        else:
            raise AssertionError(f"unexpected command: {cmd}")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pcb_tools.subprocess, "run", _fake_run)
    monkeypatch.setenv("ROUTER_WORK_DIR", str(work_dir))
    monkeypatch.setenv("ROUTER_135_DIR", str(router_dir))

    result = pcb_tools.route_bga(
        json.dumps({
            "routerType": "135",
            "selectedBGA": "U22",
            "orderLines": [{"net": "VCC", "layer": "SIG04", "order": 2}],
        })
    )

    _assert_route_summary(result, "135 ok", work_dir / "routing_input.txt", "sess-135-route")
    assert (work_dir / "layout_input.txt").read_text(encoding="utf-8") == transport._cached_project_data["sess-135-route"]
    assert (work_dir / "component_input.txt").read_text(encoding="utf-8") == "U22\n"
    assert (work_dir / "order_input.txt").read_text(encoding="utf-8") == "VCC SIG04 2\n\nU22"
    assert [Path(call[0]).name if not call[0].endswith("python.exe") else Path(call[1]).name for call in calls] == [
        "d.out",
        "e.out",
        "f.out",
    ]


def test_extract_reroute_nets_from_user_text():
    assert pcb_tools.extract_reroute_nets("请把 BGA U2 的 net13、net17 拆线后重新布线") == ["net13", "net17"]
    assert pcb_tools.extract_reroute_nets("reroute NET_A1 and net_A1, then net/B2") == ["NET_A1", "net/B2"]
    assert pcb_tools.extract_reroute_nets("这里只解释概念，不指定网络") == []


def test_drop_net_blocked_in_chat_mode():
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-chat-drop"
    transport.set_session_mode("sess-chat-drop", "chat")

    result = pcb_tools.drop_net("请把 net13 拆线后重布", projectID="proj1")
    payload = json.loads(result)

    assert payload["selectedNets"] == []
    assert "chat" in payload["error"]


def test_drop_net_calls_frontend_and_caches_context(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-drop"
    transport.set_session_mode("sess-pcb-drop", "pcb")
    calls = []

    def _fake_call_tool_sync(tool_name, arguments, timeout=30.0, session_id=None):
        calls.append((tool_name, arguments, timeout, session_id))
        if tool_name == "getSelectedElements":
            return {"ids": ["2386476278", "3424247826"]}
        if tool_name == "deleteTracesById":
            return "已成功删除"
        if tool_name == "getProjectData":
            return "(pcb after delete)"
        raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _fake_call_tool_sync)

    result = pcb_tools.drop_net("reroute selected traces", projectID="proj1")
    payload = json.loads(result)

    assert calls == [
        ("getSelectedElements", {"PFindType": "TRACES"}, 30.0, "sess-pcb-drop"),
        ("deleteTracesById", {"ids": ["2386476278", "3424247826"]}, 60.0, "sess-pcb-drop"),
        ("getProjectData", {}, 30.0, "sess-pcb-drop"),
    ]
    assert payload["selectedTraceIds"] == ["2386476278", "3424247826"]
    assert payload["droppedBoardData"] == "(pcb after delete)"
    cached = transport.get_cached_reroute_context("sess-pcb-drop")
    assert cached["selectedTraceIds"] == ["2386476278", "3424247826"]
    assert cached["localContext"]["source"] == "getSelectedElements/deleteTracesById/getProjectData"


def test_drop_net_rejects_too_many_selected_traces(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-drop-many"
    transport.set_session_mode("sess-pcb-drop-many", "pcb")
    calls = []

    def _fake_call_tool_sync(tool_name, arguments, timeout=30.0, session_id=None):
        calls.append(tool_name)
        if tool_name == "getSelectedElements":
            return {"ids": [str(index) for index in range(41)]}
        raise AssertionError("delete/getProjectData should not be called")

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _fake_call_tool_sync)

    result = pcb_tools.drop_net("reroute selected traces", projectID="proj1")
    payload = json.loads(result)

    assert calls == ["getSelectedElements"]
    assert payload["tooManySelectedElements"] is True
    assert payload["selectionCount"] == 41
    assert transport.get_cached_reroute_context("sess-pcb-drop-many") is None


def test_drop_net_rejects_non_json_selected_trace_string(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-drop-strict"
    transport.set_session_mode("sess-pcb-drop-strict", "pcb")
    calls = []

    def _fake_call_tool_sync(tool_name, arguments, timeout=30.0, session_id=None):
        calls.append(tool_name)
        if tool_name == "getSelectedElements":
            return "['2386476278', '3424247826']"
        raise AssertionError("strict parsing should reject non-JSON selection strings")

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _fake_call_tool_sync)

    result = pcb_tools.drop_net("reroute selected traces", projectID="proj1")
    payload = json.loads(result)

    assert calls == ["getSelectedElements"]
    assert payload["selectedTraceIds"] == []
    assert "No selected traces" in payload["error"]


def test_reroute_uses_cached_drop_context(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-reroute"
    transport.set_session_mode("sess-pcb-reroute", "pcb")
    transport.cache_reroute_context(
        {
            "selectedNets": ["net13", "net17"],
            "droppedBoardData": "(pcb after drop)",
            "droppedObjects": [{"net": "net13"}],
            "localContext": {"bbox": [0, 0, 10, 10]},
        },
        session_id="sess-pcb-reroute",
    )
    monkeypatch.setattr(
        pcb_tools,
        "_generate_reroute_with_model",
        lambda **kwargs: pcb_tools._build_fallback_reroute_payload(**kwargs),
    )

    result = pcb_tools.reroute(session_id="sess-pcb-reroute")
    payload = json.loads(result)

    assert payload["rerouteResult"]["type"] == "local_reroute"
    assert payload["rerouteResult"]["selectedNets"] == ["net13", "net17"]
    assert payload["rerouteResult"]["operations"][0]["action"] == "reroute_net"
    assert payload["checkReport"]["passed"] is True
    assert "局部重布" in payload["explanation"]
    assert "可解释性分析报告" in payload["content"]
    assert "布线较好概率: 0.984707" in payload["content"]


def test_reroute_uses_cached_selected_trace_ids(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-reroute-traces"
    transport.set_session_mode("sess-pcb-reroute-traces", "pcb")
    transport.cache_reroute_context(
        {
            "selectedTraceIds": ["2386476278", "3424247826"],
            "droppedBoardData": "(pcb after delete)",
            "droppedObjects": [{"id": "2386476278", "type": "trace"}],
            "localContext": {"selectionCount": 2},
        },
        session_id="sess-pcb-reroute-traces",
    )
    monkeypatch.setattr(
        pcb_tools,
        "_generate_reroute_with_model",
        lambda **kwargs: pcb_tools._build_fallback_reroute_payload(**kwargs),
    )

    result = pcb_tools.reroute(session_id="sess-pcb-reroute-traces")
    payload = json.loads(result)

    assert payload["rerouteResult"]["mode"] == "selected_traces_after_delete"
    assert payload["rerouteResult"]["selectedTraceIds"] == ["2386476278", "3424247826"]
    assert payload["rerouteResult"]["operations"][0]["action"] == "reroute_selected_traces"
    assert payload["checkReport"]["passed"] is True


def test_reroute_invokes_model_generation_with_dropped_board_file(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-reroute-file"
    transport.set_session_mode("sess-pcb-reroute-file", "pcb")
    board_path = tmp_path / "after_drop.kicad_pcb"
    board_path.write_text("(pcb after drop model input)", encoding="utf-8")
    transport.cache_reroute_context(
        {
            "selectedNets": ["net13"],
            "droppedBoardDataFilePath": str(board_path),
            "droppedObjects": [],
            "localContext": {},
        },
        session_id="sess-pcb-reroute-file",
    )
    seen = {}

    def _fake_generate(**kwargs):
        seen.update(kwargs)
        payload = pcb_tools._build_fallback_reroute_payload(**kwargs)
        payload["rerouteResult"]["source"] = "fake_model"
        return payload

    monkeypatch.setattr(pcb_tools, "_generate_reroute_with_model", _fake_generate)

    result = pcb_tools.reroute(session_id="sess-pcb-reroute-file")
    payload = json.loads(result)

    assert seen["dropped_board_data"] == "(pcb after drop model input)"
    assert seen["dropped_board_path"] == str(board_path)
    assert payload["rerouteResult"]["source"] == "fake_model"


def test_reroute_converts_frontend_txt_input_internally(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-reroute-txt"
    transport.set_session_mode("sess-pcb-reroute-txt", "pcb")
    frontend_txt = "(layout (Pcb-Design_Version \"PCB Builder V1.0\"))"
    transport.cache_reroute_context(
        {
            "selectedTraceIds": ["2386476278"],
            "droppedBoardData": frontend_txt,
            "droppedObjects": [],
            "localContext": {},
        },
        session_id="sess-pcb-reroute-txt",
    )
    seen = {}

    def _fake_internal_board_data(*, board_data, board_path, output_dir, session_id, label):
        assert board_data == frontend_txt
        assert label in {"dropped", "original"}
        internal_path = tmp_path / f"{label}.kicad_pcb"
        internal_path.write_text("(kicad_pcb\n)\n", encoding="utf-8")
        return "(kicad_pcb\n)\n", str(internal_path), []

    def _fake_generate(**kwargs):
        seen.update(kwargs)
        return pcb_tools._build_fallback_reroute_payload(**kwargs)

    monkeypatch.setattr(pcb_tools, "_write_internal_board_data", _fake_internal_board_data)
    monkeypatch.setattr(pcb_tools, "_generate_reroute_with_model", _fake_generate)

    result = pcb_tools.reroute(json.dumps({"maxDrcIterations": 0}, ensure_ascii=False), session_id="sess-pcb-reroute-txt")
    payload = json.loads(result)

    assert seen["dropped_board_data"].startswith("(kicad_pcb")
    assert seen["dropped_board_path"].endswith("dropped.kicad_pcb")
    assert "routedBoardDataFilePath" not in payload
    assert ".kicad_pcb" not in json.dumps(payload, ensure_ascii=False)


def test_reroute_max_drc_iterations_zero_skips_validation(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-reroute-drc-zero"
    transport.set_session_mode("sess-pcb-reroute-drc-zero", "pcb")
    original_path = tmp_path / "original.kicad_pcb"
    original_path.write_text("(kicad_pcb\n)\n", encoding="utf-8")
    transport.cache_reroute_context(
        {
            "selectedNets": ["net13"],
            "droppedBoardData": "(kicad_pcb\n)\n",
            "originalBoardDataFilePath": str(original_path),
        },
        session_id="sess-pcb-reroute-drc-zero",
    )

    def _fake_generate(**kwargs):
        payload = pcb_tools._build_fallback_reroute_payload(
            **{key: value for key, value in kwargs.items() if key not in {"drc_feedback", "drc_iteration_history"}}
        )
        payload["kicadPatch"] = "(segment (start 1 1) (end 2 2) (width 0.2) (layer F.Cu) (net 13))"
        return payload

    def _should_not_validate(**kwargs):
        raise AssertionError("maxDrcIterations=0 must skip DRC validation")

    monkeypatch.setattr(pcb_tools, "_generate_reroute_with_model", _fake_generate)
    monkeypatch.setattr(pcb_reroute_drc, "validate_kicad_patch_with_drc", _should_not_validate)

    result = pcb_tools.reroute(json.dumps({"maxDrcIterations": 0}, ensure_ascii=False), session_id="sess-pcb-reroute-drc-zero")
    payload = json.loads(result)

    assert "drcPassed" not in payload["rerouteResult"]
    assert "routedLayoutTxtFilePath" not in payload


def test_reroute_drc_pass_returns_public_txt_path(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-reroute-drc-pass"
    transport.set_session_mode("sess-pcb-reroute-drc-pass", "pcb")
    original_path = tmp_path / "original.kicad_pcb"
    original_path.write_text("(kicad_pcb\n)\n", encoding="utf-8")
    dropped_path = tmp_path / "after_drop.kicad_pcb"
    dropped_path.write_text("(kicad_pcb\n)\n", encoding="utf-8")
    transport.cache_reroute_context(
        {
            "selectedNets": ["net13"],
            "droppedBoardDataFilePath": str(dropped_path),
            "originalBoardDataFilePath": str(original_path),
            "droppedObjects": [],
            "localContext": {},
        },
        session_id="sess-pcb-reroute-drc-pass",
    )

    def _fake_generate(**kwargs):
        payload = pcb_tools._build_fallback_reroute_payload(
            **{key: value for key, value in kwargs.items() if key not in {"drc_feedback", "drc_iteration_history"}}
        )
        payload["kicadPatch"] = "(segment (start 1 1) (end 2 2) (width 0.2) (layer F.Cu) (net 13))"
        return payload

    def _fake_validate(**kwargs):
        return pcb_reroute_drc.RerouteDrcAttempt(
            iteration=kwargs["iteration"],
            passed=True,
            filled_board_data_file_path=str(tmp_path / "routed.kicad_pcb"),
            drc_result={"ok": True, "pass": True},
        )

    def _fake_convert(*, kicad_path, output_dir, session_id):
        assert kicad_path == str(tmp_path / "routed.kicad_pcb")
        txt_path = tmp_path / "routed.txt"
        txt_path.write_text("(layout routed)", encoding="utf-8")
        return str(txt_path), []

    monkeypatch.setattr(pcb_tools, "_generate_reroute_with_model", _fake_generate)
    monkeypatch.setattr(pcb_reroute_drc, "validate_kicad_patch_with_drc", _fake_validate)
    monkeypatch.setattr(pcb_tools, "_convert_internal_kicad_to_public_txt", _fake_convert)

    result = pcb_tools.reroute(session_id="sess-pcb-reroute-drc-pass")
    payload = json.loads(result)

    assert payload["rerouteResult"]["drcPassed"] is True
    assert payload["rerouteResult"]["routedLayoutTxtFilePath"] == str(tmp_path / "routed.txt")
    assert payload["routedLayoutTxtFilePath"] == str(tmp_path / "routed.txt")
    assert "routedBoardDataFilePath" not in payload
    assert "originalBoardDataFilePath" not in payload["rerouteResult"]
    assert ".kicad_pcb" not in json.dumps(payload, ensure_ascii=False)
    assert payload["checkReport"]["passed"] is True


def test_reroute_drc_failure_feedback_retries_until_pass(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-reroute-drc-retry"
    transport.set_session_mode("sess-pcb-reroute-drc-retry", "pcb")
    original_path = tmp_path / "original.kicad_pcb"
    original_path.write_text("(kicad_pcb\n)\n", encoding="utf-8")
    transport.cache_reroute_context(
        {
            "selectedNets": ["net13"],
            "droppedBoardData": "(kicad_pcb\n)\n",
            "originalBoardDataFilePath": str(original_path),
        },
        session_id="sess-pcb-reroute-drc-retry",
    )
    generate_feedback: list[list[str]] = []
    generate_history: list[list[dict]] = []

    def _fake_generate(**kwargs):
        generate_feedback.append(list(kwargs.get("drc_feedback") or []))
        generate_history.append(list(kwargs.get("drc_iteration_history") or []))
        payload = pcb_tools._build_fallback_reroute_payload(
            **{key: value for key, value in kwargs.items() if key not in {"drc_feedback", "drc_iteration_history"}}
        )
        payload["kicadPatch"] = "(segment (start 1 1) (end 2 2) (width 0.2) (layer F.Cu) (net 13))"
        return payload

    def _fake_validate(**kwargs):
        if kwargs["iteration"] == 1:
            return pcb_reroute_drc.RerouteDrcAttempt(
                iteration=1,
                passed=False,
                failure_summary='hard_rule_counts={"HR_DRC_SEGMENT_CROSSING":1}',
            )
        return pcb_reroute_drc.RerouteDrcAttempt(
            iteration=2,
            passed=True,
            filled_board_data_file_path=str(tmp_path / "routed_iter2.kicad_pcb"),
            drc_result={"ok": True, "pass": True},
        )

    monkeypatch.setattr(pcb_tools, "_generate_reroute_with_model", _fake_generate)
    monkeypatch.setattr(pcb_reroute_drc, "validate_kicad_patch_with_drc", _fake_validate)

    result = pcb_tools.reroute(json.dumps({"maxDrcIterations": 3}, ensure_ascii=False), session_id="sess-pcb-reroute-drc-retry")
    payload = json.loads(result)

    assert payload["rerouteResult"]["drcPassed"] is True
    assert payload["rerouteResult"]["drcIterations"] == 2
    assert generate_feedback[0] == []
    assert generate_feedback[1] == ['hard_rule_counts={"HR_DRC_SEGMENT_CROSSING":1}']
    assert generate_history[0] == []
    assert generate_history[1][0]["iteration"] == 1
    assert generate_history[1][0]["kicadPatch"].startswith("(segment")
    assert generate_history[1][0]["failureSummary"] == 'hard_rule_counts={"HR_DRC_SEGMENT_CROSSING":1}'


def test_reroute_drc_failure_does_not_export_public_txt(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-reroute-drc-fail"
    transport.set_session_mode("sess-pcb-reroute-drc-fail", "pcb")
    original_path = tmp_path / "original.kicad_pcb"
    original_path.write_text("(kicad_pcb\n)\n", encoding="utf-8")
    transport.cache_reroute_context(
        {
            "selectedNets": ["net13"],
            "droppedBoardData": "(kicad_pcb\n)\n",
            "originalBoardDataFilePath": str(original_path),
        },
        session_id="sess-pcb-reroute-drc-fail",
    )

    def _fake_generate(**kwargs):
        payload = pcb_tools._build_fallback_reroute_payload(
            **{key: value for key, value in kwargs.items() if key not in {"drc_feedback", "drc_iteration_history"}}
        )
        payload["kicadPatch"] = "(segment (start 1 1) (end 2 2) (width 0.2) (layer F.Cu) (net 13))"
        return payload

    def _fake_validate(**kwargs):
        return pcb_reroute_drc.RerouteDrcAttempt(
            iteration=kwargs["iteration"],
            passed=False,
            failure_summary=f"iteration {kwargs['iteration']} failed",
        )

    def _fake_convert(**kwargs):
        raise AssertionError("DRC failure must not export txt for importLines")

    monkeypatch.setattr(pcb_tools, "_generate_reroute_with_model", _fake_generate)
    monkeypatch.setattr(pcb_reroute_drc, "validate_kicad_patch_with_drc", _fake_validate)
    monkeypatch.setattr(pcb_tools, "_convert_internal_kicad_to_public_txt", _fake_convert)

    result = pcb_tools.reroute(json.dumps({"maxDrcIterations": 2}, ensure_ascii=False), session_id="sess-pcb-reroute-drc-fail")
    payload = json.loads(result)

    assert payload["rerouteResult"]["drcPassed"] is False
    assert "routedLayoutTxtFilePath" not in payload["rerouteResult"]
    assert "routedLayoutTxtFilePath" not in payload
    assert "routedBoardDataFilePath" not in payload["rerouteResult"]
    assert payload["rerouteResult"]["drcFailureReasons"] == ["iteration 1 failed", "iteration 2 failed"]
    assert ".kicad_pcb" not in json.dumps(payload, ensure_ascii=False)
    assert payload["checkReport"]["passed"] is False
