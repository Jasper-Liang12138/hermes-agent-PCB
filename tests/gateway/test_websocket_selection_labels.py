from __future__ import annotations

from gateway.config import PlatformConfig
from gateway.platforms.websocket import WebSocketAdapter


def test_wait_selection_accepts_non_u_candidate_label():
    adapter = WebSocketAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0}))
    session_id = "sess-select-fpga"

    adapter._update_route_state_from_fields(
        session_id,
        {"selection": [{"label": "FPGA1", "detail": "BGA-1156"}]},
    )

    decision = adapter._decide_route(session_id, "选择 FPGA1")

    assert decision.mode == "pcb"
    assert decision.reason == "selection_step"
    assert decision.immediate_reply is None


def test_wait_selection_rejects_label_outside_current_candidates():
    adapter = WebSocketAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0}))
    session_id = "sess-select-filter"

    adapter._update_route_state_from_fields(
        session_id,
        {"selection": [{"label": "FPGA1", "detail": "BGA-1156"}]},
    )

    decision = adapter._decide_route(session_id, "选择 U27")

    assert decision.mode == "pcb"
    assert decision.reason == "invalid_selection_turn"
    assert "选择 FPGA1" in (decision.immediate_reply or "")

