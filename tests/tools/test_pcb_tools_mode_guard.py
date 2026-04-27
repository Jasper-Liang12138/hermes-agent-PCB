"""Mode guard tests for PCB tools."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from model_tools import handle_function_call
from tools import pcb_tools


@pytest.fixture(autouse=True)
def _restore_transport_state():
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    prev_session = transport.current_session_id
    prev_modes = dict(transport._session_modes)
    prev_cache = dict(transport._cached_project_data)
    prev_adapter = transport._websocket_adapter
    prev_loop = transport._main_loop
    yield
    transport.current_session_id = prev_session
    transport._session_modes = prev_modes
    transport._cached_project_data = prev_cache
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


def test_route_runs_local_router_even_with_active_websocket_adapter(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-route-local"
    transport.set_session_mode("sess-pcb-route-local", "pcb")
    transport._cached_project_data["sess-pcb-route-local"] = '(pcb_data (component (name "U27")))'
    transport._websocket_adapter = object()

    def _should_not_proxy(*args, **kwargs):
        raise AssertionError("route must not be proxied to frontend")

    def _fake_run(cmd, cwd, capture_output, text, encoding, errors, timeout):
        assert cmd == ["router.exe"]
        assert cwd == tmp_path
        assert capture_output is True
        assert text is True
        assert encoding == "utf-8"
        assert errors == "replace"
        assert timeout == 300
        (tmp_path / "routing_input.txt").write_text("(routes (done))", encoding="utf-8")
        (tmp_path / "data.txt").write_text("布线成功", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _should_not_proxy)
    monkeypatch.setattr(pcb_tools.subprocess, "run", _fake_run)
    monkeypatch.setenv("ROUTER_CMD", "router.exe")
    monkeypatch.setenv("ROUTER_WORK_DIR", str(tmp_path))

    result = pcb_tools.route_bga('{"orderLines":[{"net":"GND","layer":"SIG03","order":1}],"constraints":{"LineWidth":4,"LineSpacing":3}}')
    payload = json.loads(result)

    assert payload == {"routingResult": "(routes (done))", "report": "布线成功"}
    assert (tmp_path / "版图信息.txt").read_text(encoding="utf-8") == '(pcb_data (component (name "U27")))'
    assert (tmp_path / "order_input.txt").read_text(encoding="utf-8") == "GND SIG03 1"
    assert (tmp_path / "constraint.txt").read_text(encoding="utf-8") == "LineWidth 4\nLineSpacing 3"


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

    def _fake_run(cmd, cwd, capture_output, text, encoding, errors, timeout):
        assert cmd == ["router.exe"]
        assert cwd == tmp_path
        (tmp_path / "routing_input.txt").write_text("(routes (fpga1))", encoding="utf-8")
        (tmp_path / "data.txt").write_text("布线成功", encoding="utf-8")
        return SimpleNamespace(returncode=0, stderr="")

    monkeypatch.setattr(pcb_tools.subprocess, "run", _fake_run)
    monkeypatch.setenv("ROUTER_CMD", "router.exe")
    monkeypatch.setenv("ROUTER_WORK_DIR", str(tmp_path))

    result = handle_function_call(
        "route",
        {"userData": '{"orderLines":[{"net":"GND","layer":"SIG03","order":1}]}'},
        session_id="sess-explicit-route",
    )
    payload = json.loads(result)

    assert payload == {"routingResult": "(routes (fpga1))", "report": "布线成功"}
    assert (tmp_path / "版图信息.txt").read_text(encoding="utf-8") == '(pcb_data (component (name "FPGA1")))'
