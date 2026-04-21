"""Mode guard tests for PCB tools."""

from __future__ import annotations

import json

import pytest

from tools import pcb_tools


@pytest.fixture(autouse=True)
def _restore_transport_state():
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    prev_session = transport.current_session_id
    prev_modes = dict(transport._session_modes)
    prev_cache = dict(transport._cached_project_data)
    yield
    transport.current_session_id = prev_session
    transport._session_modes = prev_modes
    transport._cached_project_data = prev_cache


def test_get_project_data_blocked_in_chat_mode(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-chat-guard"
    transport.set_session_mode("sess-chat-guard", "chat")

    def _should_not_call(*args, **kwargs):
        raise AssertionError("call_tool_sync should not be called in chat mode")

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _should_not_call)

    result = pcb_tools.get_project_data("proj-001")
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
        lambda tool_name, arguments, timeout=30.0: '(pcb_data (component (name "U27")))',
    )

    result = pcb_tools.get_project_data("proj-002")
    assert '(component (name "U27"))' in result
    assert transport.get_cached_project_data() == result


def test_route_blocked_in_chat_mode():
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-chat-route"
    transport.set_session_mode("sess-chat-route", "chat")

    result = pcb_tools.route_bga('{"orderLines":[{"net":"GND","layer":"SIG03","order":1}]}')
    payload = json.loads(result)
    assert payload["routingResult"] == ""
    assert "被拒绝" in payload["report"]
