"""End-to-end tests for the WebSocket PCB routing protocol."""

from __future__ import annotations

import json
import socket
import asyncio
from typing import Any

import aiohttp
import pytest

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


def _user_message_camel_project(session_id: str, project_id: str, content: str) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "projectId": project_id,
        "type": "message",
        "body": {"role": "user", "content": content},
    }


async def _recv_json(ws, timeout: float = 5.0) -> dict:
    while True:
        msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
        assert msg.type == aiohttp.WSMsgType.TEXT
        data = json.loads(msg.data)
        body = data.get("body", {})
        if (
            data.get("type") == "message"
            and body.get("content") == "已收到，正在处理..."
            and body.get("isFinal") is False
        ):
            continue
        return data


class _FakeWS:
    def __init__(self):
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, message: dict[str, Any]):
        self.sent.append(message)


def _make_adapter(port: int = 0, **extra: Any) -> WebSocketAdapter:
    merged_extra = {
        "host": "127.0.0.1",
        "port": port,
        "route_intent_llm_enabled": False,
    }
    merged_extra.update(extra)
    return WebSocketAdapter(PlatformConfig(enabled=True, extra=merged_extra))


async def _run_websocket_pcb_flow_round_trip() -> None:
    """Covers selection -> fanoutParams -> routingResult over real WebSocket I/O."""
    port = _free_port()
    adapter = _make_adapter(port)

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
            assert "__CACHED_PROJECT_DATA__" in event.text
            assert "不要再次调用 getProjectData" in event.text
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
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(_user_message(session_id, project_id, "帮我进行BGA逃逸布线"))

                tool_call = await _recv_json(ws)
                assert tool_call["type"] == "tool-calls"
                assert tool_call["body"]["content"]["name"] == "getProjectData"
                assert tool_call["body"]["content"]["arguments"] == {"projectID": project_id}
                await ws.send_str(
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

                await ws.send_str(_user_message(session_id, project_id, "选择 U27"))
                fanout_msg = await _recv_json(ws)
                assert fanout_msg["type"] == "message"
                assert fanout_msg["body"]["fanoutParams"] == fanout_params

                await ws.send_str(_user_message(session_id, project_id, "确认"))
                route_call = await _recv_json(ws)
                assert route_call["type"] == "tool-calls"
                assert route_call["body"]["content"]["name"] == "route"
                assert json.loads(route_call["body"]["content"]["arguments"]["userData"]) == fanout_params
                await ws.send_str(
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
    assert "帮我进行BGA逃逸布线" in observed_user_text[0]
    assert "不要再次调用 getProjectData" in observed_user_text[0]
    assert observed_user_text[1].endswith("选择 U27")
    assert observed_user_text[2].endswith("确认")


def test_websocket_pcb_flow_round_trip():
    asyncio.get_event_loop().run_until_complete(_run_websocket_pcb_flow_round_trip())


async def _run_websocket_chat_turn_not_misrouted() -> None:
    """普通聊天应走 chat 通道，不应强制 auto_skill=pcb。"""
    port = _free_port()
    adapter = _make_adapter(port)

    session_id = "sess-chat-1"
    project_id = "proj-chat-001"
    observed_auto_skill = []

    async def handler(event):
        observed_auto_skill.append(event.auto_skill)
        return "这是普通聊天回复。"

    adapter.set_message_handler(handler)
    await adapter.connect()

    try:
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(_user_message(session_id, project_id, "今天星期几"))
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
    adapter = _make_adapter(port)

    seen_options = []

    async def handler(event):
        raw = event.raw_message if isinstance(event.raw_message, dict) else {}
        seen_options.append(raw.get("options", {}))
        return "ok"

    adapter.set_message_handler(handler)
    await adapter.connect()
    try:
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(
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

    assert seen_options == [
        {
            "streaming": False,
            "thinking": True,
            "reasoningEffort": "high",
            "route_mode": "chat",
        }
    ]


def test_websocket_turn_options_passthrough():
    asyncio.get_event_loop().run_until_complete(_run_websocket_turn_options_passthrough())


async def _run_websocket_selection_stage_fail_closed() -> None:
    """选择阶段收到“确认”应 fail-closed，直接返回纠偏提示。"""
    port = _free_port()
    adapter = _make_adapter(port)

    session_id = "sess-fsm-1"
    project_id = "proj-fsm-001"
    handled_turns = []

    async def handler(event):
        handled_turns.append(event.text)
        if "帮我进行BGA逃逸布线" in event.text:
            assert "不要再次调用 getProjectData" in event.text
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
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(_user_message(session_id, project_id, "帮我进行BGA逃逸布线"))
                tool_call = await _recv_json(ws)
                assert tool_call["type"] == "tool-calls"
                assert tool_call["body"]["content"]["name"] == "getProjectData"
                await ws.send_str(
                    _tool_result(
                        tool_call["body"]["content"]["id"],
                        '(pcb_data (component (name "U27") (package "BGA-256")))',
                    )
                )
                first = await _recv_json(ws)
                assert first["type"] == "message"
                assert first["body"]["selection"] == [{"label": "U27", "detail": "BGA-256"}]

                await ws.send_str(_user_message(session_id, project_id, "确认"))
                second = await _recv_json(ws)
                assert second["type"] == "message"
                assert "当前还在选择阶段" in second["body"]["content"]
    finally:
        await adapter.disconnect()

    assert len(handled_turns) == 1


def test_websocket_selection_stage_fail_closed():
    asyncio.get_event_loop().run_until_complete(_run_websocket_selection_stage_fail_closed())


async def _run_websocket_selection_accepts_non_u_refdes() -> None:
    """选择阶段应接受 selection 列表里的任意合法位号，而不只 U+数字。"""
    port = _free_port()
    adapter = _make_adapter(port)

    session_id = "sess-fsm-fpga"
    project_id = "proj-fsm-fpga"
    handled_turns = []

    async def handler(event):
        handled_turns.append(event.text)
        if "帮我进行BGA逃逸布线" in event.text:
            assert "不要再次调用 getProjectData" in event.text
            return (
                "请选择 BGA 器件。\n\n"
                "##PCB_FIELDS##\n"
                '{"selection":[{"label":"FPGA1","detail":"BGA-1156"}]}\n'
                "##PCB_FIELDS_END##"
            )
        if "选择 FPGA1" in event.text:
            return "已识别到目标器件，继续生成扇出参数。"
        raise AssertionError(f"Unexpected user turn passed to handler: {event.text}")

    adapter.set_message_handler(handler)
    await adapter.connect()

    try:
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(_user_message(session_id, project_id, "帮我进行BGA逃逸布线"))
                tool_call = await _recv_json(ws)
                assert tool_call["type"] == "tool-calls"
                assert tool_call["body"]["content"]["name"] == "getProjectData"
                await ws.send_str(
                    _tool_result(
                        tool_call["body"]["content"]["id"],
                        '(pcb_data (component (name "FPGA1") (package "BGA-1156")))',
                    )
                )
                first = await _recv_json(ws)
                assert first["type"] == "message"
                assert first["body"]["selection"] == [{"label": "FPGA1", "detail": "BGA-1156"}]

                await ws.send_str(_user_message(session_id, project_id, "选择 FPGA1"))
                second = await _recv_json(ws)
                assert second["type"] == "message"
                assert second["body"]["content"] == "已识别到目标器件，继续生成扇出参数。"
    finally:
        await adapter.disconnect()

    assert len(handled_turns) == 2
    assert "帮我进行BGA逃逸布线" in handled_turns[0]
    assert "不要再次调用 getProjectData" in handled_turns[0]
    assert handled_turns[1] == f"[projectid: {project_id}]\n选择 FPGA1"


def test_websocket_selection_accepts_non_u_refdes():
    asyncio.get_event_loop().run_until_complete(_run_websocket_selection_accepts_non_u_refdes())


async def _run_stream_fields_emitted_without_true_final() -> None:
    """完整 PCB_FIELDS 出现在 isFinal=false 时也应立即下发结构字段。"""
    adapter = _make_adapter()
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


def test_extract_pcb_fields_accepts_missing_end_marker():
    content = (
        "请从以下 BGA 元件中选择要进行逃逸布线的目标：\n\n"
        "##PCB_FIELDS##\n"
        "{\n"
        '  "selection": [\n'
        '    {"label": "U27", "detail": "BGA-256, 1.0mm pitch"},\n'
        '    {"label": "U35", "detail": "BGA-484, 0.8mm pitch"}\n'
        "  ]\n"
        "}\n"
        "##PCB_FIELDS请从以下 BGA 元件中选择要进行逃逸布线的目标： ▉"
    )

    clean, fields = WebSocketAdapter._extract_pcb_fields(content)

    assert fields["selection"] == [
        {"label": "U27", "detail": "BGA-256, 1.0mm pitch"},
        {"label": "U35", "detail": "BGA-484, 0.8mm pitch"},
    ]
    assert "##PCB_FIELDS" not in clean
    assert '"selection"' not in clean


async def _run_stream_delta_is_accumulated_and_final_true() -> None:
    """增量流式输入时，WebSocket 输出应始终携带累计全文，最终帧 isFinal=true。"""
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-stream-acc"
    adapter._connections[session_id] = (ws, "proj-stream-002")
    adapter._stream_msg_ids[session_id] = "msg-stream-002"

    first = await adapter.edit_message(
        chat_id=session_id,
        message_id="msg-stream-002",
        content="你好",
        is_final=False,
    )
    assert first.success is True
    assert ws.sent[-1]["body"]["content"] == "你好"
    assert ws.sent[-1]["body"]["isFinal"] is False

    second = await adapter.edit_message(
        chat_id=session_id,
        message_id="msg-stream-002",
        content="，世界",
        is_final=False,
    )
    assert second.success is True
    assert ws.sent[-1]["body"]["content"] == "你好，世界"
    assert ws.sent[-1]["body"]["isFinal"] is False

    third = await adapter.edit_message(
        chat_id=session_id,
        message_id="msg-stream-002",
        content="！",
        is_final=True,
    )
    assert third.success is True
    assert ws.sent[-1]["body"]["content"] == "你好，世界！"
    assert ws.sent[-1]["body"]["isFinal"] is True


def test_stream_delta_is_accumulated_and_final_true():
    asyncio.get_event_loop().run_until_complete(_run_stream_delta_is_accumulated_and_final_true())


def test_resolve_ws_context_reuses_blank_session_and_camel_project():
    adapter = _make_adapter()
    ws = _FakeWS()

    session1, project1 = adapter._resolve_ws_context(
        ws,
        _user_message_camel_project("", "1231_4_arc", "帮我布线"),
    )
    assert session1.startswith("ws_")
    assert project1 == "1231_4_arc"

    session2, project2 = adapter._resolve_ws_context(
        ws,
        {"sessionId": "", "projectId": "", "type": "message", "body": {"role": "user", "content": "继续"}},
    )
    assert session2 == session1
    assert project2 == project1


@pytest.mark.asyncio
async def test_handle_user_message_injects_camel_project_id():
    adapter = _make_adapter()
    seen = {}

    async def handler(event):
        seen["text"] = event.text
        seen["raw"] = event.raw_message
        return None

    adapter.set_message_handler(handler)
    ws = _FakeWS()
    session_id, project_id = adapter._resolve_ws_context(
        ws,
        _user_message_camel_project("", "proj-camel-001", "帮我进行BGA逃逸布线"),
    )
    await adapter._handle_user_message(
        {"type": "message", "body": {"role": "user", "content": "帮我进行BGA逃逸布线"}},
        session_id,
        project_id,
    )

    assert seen["raw"]["projectid"] == "proj-camel-001"
    assert seen["text"].startswith("[projectid: proj-camel-001]")


@pytest.mark.asyncio
async def test_handle_user_message_chat_does_not_inject_project_id():
    adapter = _make_adapter(route_intent_llm_enabled=True)
    seen = {}

    async def handler(event):
        seen["text"] = event.text
        seen["raw"] = event.raw_message
        return None

    async def fake_classify(*, session_id, user_text, project_id):
        return "chat"

    adapter.set_message_handler(handler)
    adapter._classify_route_intent_with_llm = fake_classify

    await adapter._handle_user_message(
        {"type": "message", "body": {"role": "user", "content": "BGA 和 QFP 有什么区别？请简短回答。"}},
        "sess-chat-no-project",
        "proj-chat-001",
    )

    assert seen["raw"]["projectid"] == "proj-chat-001"
    assert seen["raw"]["options"]["route_mode"] == "chat"
    assert seen["text"] == "BGA 和 QFP 有什么区别？请简短回答。"


@pytest.mark.asyncio
async def test_send_tool_call_includes_session_and_project():
    adapter = _make_adapter()
    ws = _FakeWS()
    adapter._connections["sess-tool-1"] = (ws, "proj-tool-1")

    task = asyncio.create_task(
        adapter.send_tool_call(
            session_id="sess-tool-1",
            call_id="call_tool_1",
            tool_name="getProjectData",
            arguments={},
            timeout=1.0,
        )
    )
    await asyncio.sleep(0)
    sent = ws.sent[-1]
    assert sent["sessionId"] == "sess-tool-1"
    assert sent["projectid"] == "proj-tool-1"
    assert sent["type"] == "tool-calls"
    assert sent["body"]["content"]["name"] == "getProjectData"
    assert sent["body"]["content"]["arguments"] == {}

    adapter._resolve_tool_result(json.loads(_tool_result("call_tool_1", "(pcb_data)")))
    result = await task
    assert result == "(pcb_data)"


def test_rule_validation_rejects_llm_chat_for_strong_pcb_request():
    adapter = _make_adapter()

    decision = adapter._decide_route(
        "sess-llm-guard-1",
        "帮我对U27做BGA逃逸布线",
        llm_intent="chat",
    )

    assert decision.mode == "pcb"
    assert decision.reason == "pcb_entry"
    assert decision.bootstrap_get_project is True


@pytest.mark.parametrize(
    ("text", "expected_mode"),
    [
        ("不要解释，直接开始PCB BGA逃逸布线", "pcb"),
        ("开始 PCB 布线", "pcb"),
        ("这个板子跑一下 BGA 扇出", "pcb"),
        ("对 U27 做 BGA fanout", "pcb"),
        ("获取当前版图并找出可布线 BGA", "pcb"),
        ("BGA 和 QFP 有什么区别？", "chat"),
        ("不要布线，只解释一下逃逸布线原理", "chat"),
        ("今天星期几？", "chat"),
    ],
)
def test_route_decision_handles_varied_pcb_language(text, expected_mode):
    adapter = _make_adapter()

    decision = adapter._decide_route("sess-varied-intent", text)

    assert decision.mode == expected_mode


@pytest.mark.parametrize(
    ("raw", "expected_intent", "expected_route"),
    [
        ('{"intent":"pcb_entry","route_mode":"pcb","confidence":0.93,"should_call_get_project_data":true}', "pcb_entry", "pcb"),
        ("```json\n{\"intent\":\"chat\",\"route_mode\":\"chat\",\"confidence\":0.91}\n```", "chat", "chat"),
        ("intent=pcb_entry; route_mode=pcb; confidence=0.86; reason_code=explicit_pcb_action", "pcb_entry", "pcb"),
        ("用户明确要求执行 PCB BGA 逃逸布线，应判定为 pcb_entry，route_mode 为 pcb。", "pcb_entry", "pcb"),
    ],
)
def test_parse_route_intent_output_tolerates_non_json(raw, expected_intent, expected_route):
    adapter = _make_adapter()

    intent = adapter._parse_route_intent_output(raw)

    assert intent is not None
    assert intent.intent == expected_intent
    assert intent.route_mode == expected_route


def test_rule_validation_rejects_followup_without_pcb_context():
    adapter = _make_adapter()

    decision = adapter._decide_route(
        "sess-llm-guard-2",
        "继续",
        llm_intent="pcb_followup",
    )

    assert decision.mode == "chat"
    assert decision.reason == "default_chat"


@pytest.mark.asyncio
async def test_handle_user_message_uses_llm_intent_before_rule_fallback(monkeypatch):
    adapter = _make_adapter(route_intent_llm_enabled=True)
    seen = {}

    async def fake_classify(*, session_id, user_text, project_id):
        seen["llm_args"] = (session_id, user_text, project_id)
        return "pcb_entry"

    async def handler(event):
        seen["auto_skill"] = event.auto_skill
        seen["text"] = event.text
        return None

    monkeypatch.setattr(adapter, "_classify_route_intent_with_llm", fake_classify)
    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {
            "type": "message",
            "body": {"role": "user", "content": "帮我对U27做BGA逃逸布线"},
        },
        "sess-llm-1",
        "proj-llm-1",
    )

    assert seen["llm_args"] == ("sess-llm-1", "帮我对U27做BGA逃逸布线", "proj-llm-1")
    assert seen["auto_skill"] == "hardware/pcb-intelligence"
    assert seen["text"].startswith("[projectid: proj-llm-1]")


@pytest.mark.asyncio
async def test_pcb_entry_bootstrap_reads_project_data_file_path(monkeypatch, tmp_path):
    monkeypatch.setenv("BOARD_DATA_USE_FILE_PATH", "1")
    port = _free_port()
    adapter = _make_adapter(port)
    board_file = tmp_path / "board.txt"
    board_file.write_text('(pcb_data (component (name "FPGA1") (package "BGA-1156")))', encoding="utf-8")
    seen = {}

    async def handler(event):
        seen["text"] = event.text
        seen["options"] = event.raw_message.get("options", {})
        return "ok"

    adapter.set_message_handler(handler)
    await adapter.connect()
    try:
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(
                    _user_message(
                        "sess-bootstrap-file",
                        "proj-bootstrap-file",
                        "直接开始逃逸布线，不要解释",
                    )
                )
                tool_call = await _recv_json(ws)
                assert tool_call["type"] == "tool-calls"
                assert tool_call["body"]["content"]["name"] == "getProjectData"
                await ws.send_str(_tool_result(tool_call["body"]["content"]["id"], str(board_file)))

                msg = await _recv_json(ws)
                assert msg["type"] == "message"
                assert msg["body"]["content"] == "ok"
    finally:
        await adapter.disconnect()

    assert "FPGA1" not in seen["text"]
    assert "__CACHED_PROJECT_DATA__" in seen["text"]
    assert "不要再次调用 getProjectData" in seen["text"]
    assert seen["options"]["route_mode"] == "pcb"
    assert seen["options"]["pcb_bootstrap"]["project_data_loaded"] is True


def test_bga_question_with_polite_phrase_stays_chat():
    adapter = _make_adapter()

    decision = adapter._decide_route(
        "sess-chat-question-1",
        "BGA 和 QFP 有什么区别？请简短回答。",
        llm_intent="chat",
    )

    assert decision.mode == "chat"
    assert decision.reason in {"chat_only", "default_chat"}


def test_stream_snapshot_with_cursor_replaces_instead_of_duplication():
    adapter = _make_adapter()
    buffers = {}
    session_id = "sess-stream-cursor"

    first = adapter._coalesce_stream_fragment(buffers, session_id, "我来帮你获取 ▉")
    second = adapter._coalesce_stream_fragment(
        buffers,
        session_id,
        "我来帮你获取 PCB 项目数据，然后分析区别。 ▉",
    )

    assert first == "我来帮你获取 ▉"
    assert second == "我来帮你获取 PCB 项目数据，然后分析区别。 ▉"
    assert buffers[session_id] == "我来帮你获取 PCB 项目数据，然后分析区别。"
