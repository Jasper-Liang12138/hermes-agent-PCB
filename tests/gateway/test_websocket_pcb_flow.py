"""End-to-end tests for the WebSocket PCB routing protocol."""

from __future__ import annotations

import json
import socket
import asyncio
from typing import Any

import websockets

from gateway.config import PlatformConfig
from gateway.platforms.websocket import WebSocketAdapter


def _free_port() -> int:
    """Reserve a free localhost TCP port for the test server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _user_message(session_id: str, project_id: str, content: str, options: dict | None = None) -> str:
    body = {"role": "user", "content": content}
    if options is not None:
        body["options"] = options
    return json.dumps(
        {
            "sessionId": session_id,
            "projectid": project_id,
            "type": "message",
            "body": body,
        },
        ensure_ascii=False,
    )


def _tool_result(call_id: str, result) -> str:
    return json.dumps(
        {
            "type": "tool-results",
            "body": {"role": "tool", "content": {"id": call_id, "result": result}},
        },
        ensure_ascii=False,
    )


async def _recv_json(ws, timeout: float = 5.0) -> dict:
    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
    return json.loads(raw)


class _FakeWS:
    def __init__(self):
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]):
        self.sent.append(message)


async def _run_websocket_pcb_flow_round_trip() -> None:
    """Covers selection -> fanoutParams -> routingResult over real WebSocket I/O."""
    port = _free_port()
    adapter = WebSocketAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": port})
    )

    session_id = "sess-pcb-1"
    project_id = "proj-autotest-001"
    observed_user_text: list[str] = []

    fanout_params = {
        "orderLines": [
            {"net": "GND", "layer": "SIG03", "order": 1},
            {"net": "VCC", "layer": "SIG03", "order": 2},
            {"net": "DDR_D0", "layer": "SIG04", "order": 3},
        ],
        "constraints": {"LineWidth": 4, "LineSpacing": 3},
    }
    route_result = {
        "routingResult": (
            '(routes (route (net "GND") (layer "SIG03") '
            '(path (line (start 1 2) (end 3 4) (width 3)))))'
        ),
        "report": "布线连通率: 100%",
    }

    async def handler(event):
        observed_user_text.append(event.text)

        if "帮我进行BGA逃逸布线" in event.text:
            assert f"[projectid: {project_id}]" in event.text
            board_data = await adapter.send_tool_call(
                session_id=event.source.chat_id,
                call_id="call_get_project",
                tool_name="getProjectData",
                arguments={"projectID": project_id},
                timeout=3.0,
            )
            assert 'name "U27"' in board_data
            return (
                "已获取项目版图数据，请选择一个 BGA。\n\n"
                "##PCB_FIELDS##\n"
                '{"selection":[{"label":"U27","detail":"BGA-256, 1.0mm pitch"},'
                '{"label":"U35","detail":"BGA-484, 0.8mm pitch"}]}\n'
                "##PCB_FIELDS_END##"
            )

        if "选择 U27" in event.text:
            return (
                "已生成扇出参数，请确认。\n\n"
                "##PCB_FIELDS##\n"
                f'{json.dumps({"fanoutParams": fanout_params}, ensure_ascii=False)}\n'
                "##PCB_FIELDS_END##"
            )

        if "确认" in event.text:
            routed = await adapter.send_tool_call(
                session_id=event.source.chat_id,
                call_id="call_route",
                tool_name="route",
                arguments={"userData": json.dumps(fanout_params, ensure_ascii=False)},
                timeout=3.0,
            )
            routed_obj = json.loads(routed) if isinstance(routed, str) else routed
            assert routed_obj["routingResult"] == route_result["routingResult"]
            return (
                "布线完成。\n\n"
                "##PCB_FIELDS##\n"
                f'{json.dumps({"routingResult": routed_obj["routingResult"]}, ensure_ascii=False)}\n'
                "##PCB_FIELDS_END##"
            )

        raise AssertionError(f"Unexpected user turn: {event.text}")

    adapter.set_message_handler(handler)
    await adapter.connect()

    try:
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri, ping_interval=None) as ws:
            await ws.send(_user_message(session_id, project_id, "帮我进行BGA逃逸布线"))

            tool_call = await _recv_json(ws)
            assert tool_call["type"] == "tool-calls"
            assert tool_call["body"]["content"]["name"] == "getProjectData"
            assert tool_call["body"]["content"]["arguments"] == {"projectID": project_id}
            await ws.send(
                _tool_result(
                    tool_call["body"]["content"]["id"],
                    '(pcb_data (component (name "U27") (package "BGA-256")) '
                    '(component (name "U35") (package "BGA-484")))',
                )
            )

            selection_msg = await _recv_json(ws)
            assert selection_msg["type"] == "message"
            assert selection_msg["body"]["selection"] == [
                {"label": "U27", "detail": "BGA-256, 1.0mm pitch"},
                {"label": "U35", "detail": "BGA-484, 0.8mm pitch"},
            ]

            await ws.send(_user_message(session_id, project_id, "选择 U27"))
            fanout_msg = await _recv_json(ws)
            assert fanout_msg["type"] == "message"
            assert fanout_msg["body"]["fanoutParams"] == fanout_params

            await ws.send(_user_message(session_id, project_id, "确认"))
            route_call = await _recv_json(ws)
            assert route_call["type"] == "tool-calls"
            assert route_call["body"]["content"]["name"] == "route"
            assert json.loads(route_call["body"]["content"]["arguments"]["userData"]) == fanout_params
            await ws.send(
                _tool_result(
                    route_call["body"]["content"]["id"],
                    route_result,
                )
            )

            routed_msg = await _recv_json(ws)
            assert routed_msg["type"] == "message"
            assert routed_msg["body"]["routingResult"] == route_result["routingResult"]

    finally:
        await adapter.disconnect()

    assert len(observed_user_text) == 3
    assert observed_user_text[0].endswith("帮我进行BGA逃逸布线")
    assert observed_user_text[1].endswith("选择 U27")
    assert observed_user_text[2].endswith("确认")


def test_websocket_pcb_flow_round_trip():
    asyncio.get_event_loop().run_until_complete(_run_websocket_pcb_flow_round_trip())


async def _run_websocket_chat_turn_not_misrouted() -> None:
    """普通聊天应走 chat 通道，不应强制 auto_skill=pcb。"""
    port = _free_port()
    adapter = WebSocketAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": port})
    )

    session_id = "sess-chat-1"
    project_id = "proj-chat-001"
    observed_auto_skill = []

    async def handler(event):
        observed_auto_skill.append(event.auto_skill)
        return "这是普通聊天回复。"

    adapter.set_message_handler(handler)
    await adapter.connect()

    try:
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri, ping_interval=None) as ws:
            await ws.send(_user_message(session_id, project_id, "今天星期几"))
            msg = await _recv_json(ws)
            assert msg["type"] == "message"
            assert msg["body"]["content"] == "这是普通聊天回复。"
    finally:
        await adapter.disconnect()

    assert observed_auto_skill == [None]


def test_websocket_chat_turn_not_misrouted():
    asyncio.get_event_loop().run_until_complete(_run_websocket_chat_turn_not_misrouted())


async def _run_websocket_turn_options_passthrough() -> None:
    """WebSocket body.options 应透传到 MessageEvent.raw_message.options。"""
    port = _free_port()
    adapter = WebSocketAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": port})
    )

    seen_options = []

    async def handler(event):
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        seen_options.append(raw.get("options", {}))
        return "ok"

    adapter.set_message_handler(handler)
    await adapter.connect()
    try:
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri, ping_interval=None) as ws:
            await ws.send(
                _user_message(
                    "sess-opt-1",
                    "proj-opt-1",
                    "只聊天",
                    options={"streaming": False, "thinking": True, "reasoningEffort": "high"},
                )
            )
            _ = await _recv_json(ws)
    finally:
        await adapter.disconnect()

    assert seen_options == [{"streaming": False, "thinking": True, "reasoningEffort": "high"}]


def test_websocket_turn_options_passthrough():
    asyncio.get_event_loop().run_until_complete(_run_websocket_turn_options_passthrough())


async def _run_websocket_selection_stage_fail_closed() -> None:
    """选择阶段收到“确认”应 fail-closed，直接返回纠偏提示。"""
    port = _free_port()
    adapter = WebSocketAdapter(
        PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": port})
    )

    session_id = "sess-fsm-1"
    project_id = "proj-fsm-001"
    handled_turns = []

    async def handler(event):
        handled_turns.append(event.text)
        if "帮我进行BGA逃逸布线" in event.text:
            return (
                "请选择 BGA 器件。\n\n"
                "##PCB_FIELDS##\n"
                '{"selection":[{"label":"U27","detail":"BGA-256"}]}\n'
                "##PCB_FIELDS_END##"
            )
        raise AssertionError(f"Unexpected user turn passed to handler: {event.text}")

    adapter.set_message_handler(handler)
    await adapter.connect()

    try:
        uri = f"ws://127.0.0.1:{port}"
        async with websockets.connect(uri, ping_interval=None) as ws:
            await ws.send(_user_message(session_id, project_id, "帮我进行BGA逃逸布线"))
            first = await _recv_json(ws)
            assert first["type"] == "message"
            assert first["body"]["selection"] == [{"label": "U27", "detail": "BGA-256"}]

            await ws.send(_user_message(session_id, project_id, "确认"))
            second = await _recv_json(ws)
            assert second["type"] == "message"
            assert "当前还在选择阶段" in second["body"]["content"]
    finally:
        await adapter.disconnect()

    assert len(handled_turns) == 1


def test_websocket_selection_stage_fail_closed():
    asyncio.get_event_loop().run_until_complete(_run_websocket_selection_stage_fail_closed())


async def _run_stream_fields_emitted_without_true_final() -> None:
    """完整 PCB_FIELDS 出现在 isFinal=false 时也应立即下发结构字段。"""
    adapter = WebSocketAdapter(PlatformConfig(enabled=True, extra={"host": "127.0.0.1", "port": 0}))
    ws = _FakeWS()
    session_id = "sess-stream-field"
    adapter._connections[session_id] = (ws, "proj-stream-001")
    adapter._stream_msg_ids[session_id] = "msg-stream-001"

    content = (
        "检测到项目中存在 2 个 BGA 元件。\n"
        "##PCB_FIELDS##\n"
        "```json\n"
        '{"selection":[{"label":"U27","detail":"BGA-256"}]}\n'
        "```\n"
        "##PCB_FIELDS_END##\n"
        "请选择一个器件。"
    )
    result = await adapter.edit_message(
        chat_id=session_id,
        message_id="msg-stream-001",
        content=content,
        is_final=False,
    )
    assert result.success is True
    first = ws.sent[-1]["body"]
    assert first["isFinal"] is None
    assert first["selection"] == [{"label": "U27", "detail": "BGA-256"}]
    assert "##PCB_FIELDS##" not in first["content"]
    assert adapter._session_flow_states.get(session_id) == "wait_selection"

    # 同一份累计内容重复到达时，不应重复发同样的结构字段
    result2 = await adapter.edit_message(
        chat_id=session_id,
        message_id="msg-stream-001",
        content=content,
        is_final=False,
    )
    assert result2.success is True
    second = ws.sent[-1]["body"]
    assert second["isFinal"] is False
    assert "selection" not in second


def test_stream_fields_emitted_without_true_final():
    asyncio.get_event_loop().run_until_complete(_run_stream_fields_emitted_without_true_final())
