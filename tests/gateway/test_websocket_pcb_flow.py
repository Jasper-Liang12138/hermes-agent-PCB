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


_INTERIM_STATUS_CONTENTS = {
    "已收到，正在处理...",
    "已收到，进入拆线重布 skill，正在处理...",
    "已收到，进入PCB 智能布线 skill，正在处理...",
}


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
            and body.get("content") in _INTERIM_STATUS_CONTENTS
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
        "trace_pcb_messages": False,
    }
    merged_extra.update(extra)
    return WebSocketAdapter(PlatformConfig(enabled=True, extra=merged_extra))


async def _run_websocket_pcb_flow_round_trip(monkeypatch) -> None:
    """Covers selection -> fanoutParams -> routingResult over real WebSocket I/O."""
    port = _free_port()
    adapter = _make_adapter(port)

    session_id = "sess-pcb-1"
    project_id = "proj-autotest-001"
    observed_user_text: list[str] = []

    fanout_params = {
        "routerType": "arc",
        "orderLines": [
            {"net": "GND", "layer": "SIG03", "order": 1},
            {"net": "VCC", "layer": "SIG03", "order": 2},
            {"net": "DDR_D0", "layer": "SIG04", "order": 3},
        ],
        "constraints": {"LineWidth": 4, "LineSpacing": 3},
    }
    route_result = {
        "routingResult": r"F:\router_work\routing_input.txt",
        "report": "布线连通率: 100%",
    }
    from tools import pcb_tools

    def _fake_route_bga(user_data: str, session_id: str | None = None) -> str:
        assert session_id == "sess-pcb-1"
        assert json.loads(user_data) == {**fanout_params, "selectedBGA": "U27"}
        return json.dumps(route_result, ensure_ascii=False)

    monkeypatch.setattr(pcb_tools, "route_bga", _fake_route_bga)

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

        if "routerType 已确定为 arc" in event.text:
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
                router_msg = await _recv_json(ws)
                assert router_msg["type"] == "message"
                assert "请选择走线算法类型" in router_msg["body"]["content"]

                await ws.send_str(_user_message(session_id, project_id, "arc"))
                fanout_msg = await _recv_json(ws)
                assert fanout_msg["type"] == "message"
                assert fanout_msg["body"]["fanoutParams"] == fanout_params

                await ws.send_str(_user_message(session_id, project_id, "确认"))
                routed_msg = await _recv_json(ws)
                saw_import_status = False
                while routed_msg["type"] == "message":
                    content = routed_msg["body"].get("content", "")
                    if content == "正在导入版图，请稍候...":
                        saw_import_status = True
                        assert routed_msg["body"]["isFinal"] is False
                    elif content.startswith("已确认，正在调用"):
                        assert routed_msg["body"]["isFinal"] is True
                    else:
                        raise AssertionError(f"Unexpected message before importLines: {routed_msg}")
                    routed_msg = await _recv_json(ws)
                assert saw_import_status
                assert routed_msg["type"] == "tool-calls"
                assert routed_msg["body"]["content"]["name"] == "importLines"
                assert routed_msg["body"]["content"]["arguments"] == {
                    "filePath": route_result["routingResult"],
                    "successPins": [],
                    "failedPins": [],
                }
                await ws.send_str(
                    _tool_result(
                        routed_msg["body"]["content"]["id"],
                        {"success": True, "message": "导入完成"},
                    )
                )

                routed_msg = await _recv_json(ws)
                assert routed_msg["type"] == "message"
                assert routed_msg["body"]["isFinal"] is True
                assert routed_msg["body"]["routingResult"] == route_result["routingResult"]
                assert routed_msg["body"]["report"] == route_result["report"]
                assert "导入完成" in routed_msg["body"]["content"]

    finally:
        await adapter.disconnect()

    assert len(observed_user_text) == 2
    assert "帮我进行BGA逃逸布线" in observed_user_text[0]
    assert "不要再次调用 getProjectData" in observed_user_text[0]
    assert "routerType 已确定为 arc" in observed_user_text[1]


def test_websocket_pcb_flow_round_trip(monkeypatch):
    asyncio.get_event_loop().run_until_complete(_run_websocket_pcb_flow_round_trip(monkeypatch))


def test_websocket_reroute_intent_loads_reroute_skill_without_bootstrap():
    adapter = _make_adapter()
    session_id = "sess-reroute-intent"

    decision = adapter._decide_route(
        session_id,
        "请帮我针对版图数据中的 BGA U2 的 net13、net17 拆线后重新布线",
    )

    assert decision.mode == "pcb"
    assert decision.intent == "pcb_reroute_selected"
    assert decision.bootstrap_get_project is False


@pytest.mark.parametrize(
    "text",
    [
        "删除我框选的线重新布线",
        "请对当前框选走线拆线重布",
        "把我选中的 traces 删除后重新走线",
    ],
)
def test_websocket_selected_trace_reroute_intent_variants(text):
    adapter = _make_adapter()
    decision = adapter._decide_route("sess-selected-trace-reroute", text)

    assert decision.mode == "pcb"
    assert decision.intent == "pcb_reroute_selected"
    assert decision.bootstrap_get_project is False


def test_websocket_reroute_short_command_works_in_pcb_context():
    adapter = _make_adapter()
    session_id = "sess-reroute-short"
    adapter._set_session_mode(session_id, "pcb")

    decision = adapter._decide_route(session_id, "拆线重布")

    assert decision.mode == "pcb"
    assert decision.intent == "pcb_reroute_selected"
    assert decision.bootstrap_get_project is False


def test_websocket_reroute_interrupts_pending_fanout_confirmation():
    adapter = _make_adapter()
    session_id = "sess-reroute-interrupt"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")
    adapter._session_fanout_params[session_id] = {
        "selectedBGA": "U22",
        "routerType": "135",
    }

    decision = adapter._decide_route(session_id, "拆线重布")

    assert decision.mode == "pcb"
    assert decision.intent == "pcb_reroute_selected"
    assert decision.reason == "pcb_reroute_selected"
    assert adapter._session_flow_states[session_id] == "idle"
    assert session_id not in adapter._session_fanout_params


def test_websocket_reroute_llm_intent_takes_priority_over_keyword_fallback():
    adapter = _make_adapter()

    chat_decision = adapter._decide_route(
        "sess-reroute-llm-chat",
        "请帮我针对版图数据中的 BGA U2 的 net13、net17 拆线后重新布线",
        llm_intent={
            "intent": "chat",
            "route_mode": "chat",
            "confidence": 0.91,
            "should_call_get_project_data": False,
        },
    )
    assert chat_decision.mode == "chat"
    assert chat_decision.intent == "chat"

    reroute_decision = adapter._decide_route(
        "sess-reroute-llm-direct",
        "把这些未布通网络重新整理一下",
        llm_intent={
            "intent": "pcb_reroute_selected",
            "route_mode": "pcb",
            "confidence": 0.88,
            "should_call_get_project_data": False,
        },
    )
    assert reroute_decision.mode == "pcb"
    assert reroute_decision.intent == "pcb_reroute_selected"
    assert reroute_decision.bootstrap_get_project is False


async def _run_websocket_reroute_fields_round_trip() -> None:
    port = _free_port()
    adapter = _make_adapter(port)
    session_id = "sess-reroute-fields"
    project_id = "proj-reroute-001"
    observed_auto_skill: list[str | None] = []

    reroute_fields = {
        "rerouteResult": {
            "type": "local_reroute",
            "selectedNets": ["net13", "net17"],
            "operations": [{"action": "reroute_net", "net": "net13"}],
            "routedBoardDataFilePath": r"F:\internal\routed.kicad_pcb",
            "routedLayoutTxtFilePath": r"F:\public\routed.txt",
        },
        "routedBoardDataFilePath": r"F:\internal\routed.kicad_pcb",
        "routedLayoutTxtFilePath": r"F:\public\routed.txt",
        "checkReport": {"passed": True, "checks": []},
        "explanation": "局部重布结果已生成",
    }

    async def handler(event):
        observed_auto_skill.append(event.auto_skill)
        assert "不要再次调用 getProjectData" not in event.text
        return (
            "已完成局部拆线重布。\n\n"
            "##PCB_FIELDS##\n"
            f"{json.dumps(reroute_fields, ensure_ascii=False)}\n"
            "##PCB_FIELDS_END##"
        )

    adapter.set_message_handler(handler)
    await adapter.connect()

    try:
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(
                    _user_message(
                        session_id,
                        project_id,
                        "请帮我针对版图数据中的 BGA U2 的 net13、net17 拆线后重新布线",
                    )
                )

                msg = await _recv_json(ws)
                assert msg["type"] == "message"
                assert msg["body"]["content"] == "正在导入版图，请稍候..."
                assert msg["body"]["isFinal"] is False

                msg = await _recv_json(ws)
                assert msg["type"] == "tool-calls"
                assert msg["body"]["content"]["name"] == "importLines"
                assert msg["body"]["content"]["arguments"] == {
                    "filePath": r"F:\public\routed.txt",
                    "successPins": [],
                    "failedPins": [],
                }
                await ws.send_str(
                    _tool_result(
                        msg["body"]["content"]["id"],
                        {"success": True, "message": "导入完成"},
                    )
                )

                msg = await _recv_json(ws)
                assert msg["type"] == "message"
                assert msg["body"]["rerouteResult"] == {
                    "type": "local_reroute",
                    "selectedNets": ["net13", "net17"],
                    "operations": [{"action": "reroute_net", "net": "net13"}],
                    "routedLayoutTxtFilePath": r"F:\public\routed.txt",
                }
                assert msg["body"]["routedLayoutTxtFilePath"] == r"F:\public\routed.txt"
                assert "routedBoardDataFilePath" not in msg["body"]
                assert msg["body"]["checkReport"] == reroute_fields["checkReport"]
                assert "导入完成" in msg["body"]["explanation"]
                assert ".kicad_pcb" not in json.dumps(msg["body"], ensure_ascii=False)
    finally:
        await adapter.disconnect()

    assert observed_auto_skill == ["hardware/pcb-reroute"]


def test_websocket_reroute_fields_round_trip():
    asyncio.get_event_loop().run_until_complete(_run_websocket_reroute_fields_round_trip())


async def _run_websocket_failed_reroute_does_not_import() -> None:
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-reroute-failed-no-import"
    adapter._connections[session_id] = (ws, "proj-reroute-failed")

    result = await adapter.send(
        chat_id=session_id,
        content=(
            "局部拆线重布未通过 DRC。\n\n"
            "##PCB_FIELDS##\n"
            + json.dumps(
                {
                    "rerouteResult": {
                        "type": "local_reroute",
                        "drcPassed": False,
                        "routedBoardDataFilePath": r"F:\internal\failed.kicad_pcb",
                    },
                    "checkReport": {"passed": False, "checks": []},
                    "explanation": r"DRC 未通过，内部路径 F:\internal\failed.kicad_pcb 不应给前端。",
                },
                ensure_ascii=False,
            )
            + "\n##PCB_FIELDS_END##"
        ),
        metadata={"stream_is_final": True},
    )

    assert result.success is True
    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "message"
    body = ws.sent[0]["body"]
    assert body["rerouteResult"]["drcPassed"] is False
    assert "routedLayoutTxtFilePath" not in body
    assert "routedBoardDataFilePath" not in body["rerouteResult"]
    assert ".kicad_pcb" not in json.dumps(body, ensure_ascii=False)


def test_websocket_failed_reroute_does_not_import():
    asyncio.get_event_loop().run_until_complete(_run_websocket_failed_reroute_does_not_import())


async def _run_websocket_reroute_sends_skill_status() -> None:
    port = _free_port()
    adapter = _make_adapter(port)
    session_id = "sess-reroute-status"
    project_id = "proj-reroute-status"
    observed_auto_skill: list[str | None] = []

    async def handler(event):
        observed_auto_skill.append(event.auto_skill)
        return "ok"

    adapter.set_message_handler(handler)
    await adapter.connect()

    try:
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(
                    _user_message(
                        session_id,
                        project_id,
                        "删除我框选的线重新布线",
                    )
                )

                received: list[dict[str, Any]] = []
                for _ in range(4):
                    msg = await asyncio.wait_for(ws.receive(), timeout=5.0)
                    assert msg.type == aiohttp.WSMsgType.TEXT
                    data = json.loads(msg.data)
                    received.append(data)
                    if data.get("body", {}).get("content") == "ok":
                        break

                status_messages = [
                    item
                    for item in received
                    if item.get("type") == "message"
                    and item.get("body", {}).get("content") == "已收到，进入拆线重布 skill，正在处理..."
                    and item.get("body", {}).get("isFinal") is False
                ]
                assert status_messages
    finally:
        await adapter.disconnect()

    assert observed_auto_skill == ["hardware/pcb-reroute"]


def test_websocket_reroute_sends_skill_status():
    asyncio.get_event_loop().run_until_complete(_run_websocket_reroute_sends_skill_status())


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
                assert "执行布线前必须先选择布线器" in second["body"]["content"]
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
                assert "已选择目标 BGA：FPGA1" in second["body"]["content"]
                assert "请回复 `arc` 或 `135`" in second["body"]["content"]
    finally:
        await adapter.disconnect()

    assert len(handled_turns) == 1
    assert "帮我进行BGA逃逸布线" in handled_turns[0]
    assert "不要再次调用 getProjectData" in handled_turns[0]


def test_websocket_selection_accepts_non_u_refdes():
    asyncio.get_event_loop().run_until_complete(_run_websocket_selection_accepts_non_u_refdes())


def test_router_choice_prompt_sets_wait_router_state():
    adapter = _make_adapter()
    session_id = "sess-router-choice-visible"
    adapter._set_session_mode(session_id, "pcb")

    content = "已完成 BGA 分析，请选择走线算法类型：arc 或 135。"
    guarded = adapter._guard_router_choice_before_confirm(session_id, content, {})

    assert guarded == content
    assert adapter._session_flow_states[session_id] == "wait_router_type"


def test_router_type_followup_recovers_when_flow_state_was_lost():
    adapter = _make_adapter()
    session_id = "sess-router-choice-lost-state"
    adapter._set_session_mode(session_id, "pcb", lock_seconds=0.0)
    adapter._set_flow_state(session_id, "idle")

    decision = adapter._decide_route(session_id, "135")

    assert decision.mode == "pcb"
    assert decision.reason == "router_type_step"
    assert adapter._session_router_types[session_id] == "135"
    assert adapter._session_flow_states[session_id] == "wait_router_type"


def test_fanout_params_visible_content_is_normalized():
    content = (
        "已生成扇出参数，请确认：\n"
        "- 目标 BGA：U22\n"
        "请已生成扇出参数，请确认：\n"
        "- 目标 BGA：U22请回复 **确认** 执行布线。"
    )
    fanout_params = {
        "selectedBGA": "U22",
        "routerType": "135",
        "orderLines": [
            {"net": "GND", "layer": "Top", "order": 1},
            {"net": "VCC", "layer": "Art03", "order": 2},
            {"net": "CLK", "layer": "Art03", "order": 3},
        ],
        "constraints": {"LineWidth": 4, "LineSpacing": 3},
    }

    normalized = WebSocketAdapter._fallback_visible_content_for_fields(
        content,
        {"fanoutParams": fanout_params},
    )

    assert normalized.count("已生成扇出参数，请确认") == 1
    assert "请已生成" not in normalized
    assert "目标 BGA：U22" in normalized
    assert "走线算法：135（135 度折角走线）" in normalized
    assert "逃逸层：Top、Art03" in normalized
    assert "请回复“确认”执行布线" in normalized


def test_direct_fanout_payload_is_sent_as_fanout_params():
    fields = WebSocketAdapter._collect_pcb_fields(
        {
            "selectedBGA": "U22",
            "routerType": "135",
            "orderLines": [{"net": "GND", "layer": "Top", "order": 1}],
            "constraints": {"LineWidth": 4, "LineSpacing": 3},
        }
    )

    assert fields["fanoutParams"] == {
        "selectedBGA": "U22",
        "routerType": "135",
        "orderLines": [{"net": "GND", "layer": "Top", "order": 1}],
        "constraints": {"LineWidth": 4, "LineSpacing": 3},
    }


def test_routing_result_report_visible_fallback():
    content = WebSocketAdapter._fallback_visible_content_for_fields(
        "",
        {
            "routingResult": r"F:\router_work\routing_input.txt",
            "report": "布线连通率：98.44%\n总线长：132977.590\n通孔数量：126",
        },
    )

    assert "布线连通率：98.44%" in content
    assert "通孔数量：126" in content


async def _run_plain_send_defaults_to_final() -> None:
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-plain-chat"
    adapter._connections[session_id] = (ws, "proj-plain-chat")

    result = await adapter.send(chat_id=session_id, content="你好，我在。")

    assert result.success is True
    assert ws.sent[-1]["body"]["content"] == "你好，我在。"
    assert ws.sent[-1]["body"]["isFinal"] is True


def test_plain_send_defaults_to_final():
    asyncio.get_event_loop().run_until_complete(_run_plain_send_defaults_to_final())


async def _run_plain_status_send_can_mark_non_final() -> None:
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-status-send"
    adapter._connections[session_id] = (ws, "proj-status-send")

    result = await adapter.send(
        chat_id=session_id,
        content="⚠️ Empty response from model — retrying (1/3)",
        metadata={"is_final": False},
    )

    assert result.success is True
    assert ws.sent[-1]["body"]["content"] == "⚠️ Empty response from model — retrying (1/3)"
    assert ws.sent[-1]["body"]["isFinal"] is False


def test_plain_status_send_can_mark_non_final():
    asyncio.get_event_loop().run_until_complete(_run_plain_status_send_can_mark_non_final())


async def _run_pcb_outbound_trace_log(tmp_path) -> None:
    adapter = _make_adapter(
        trace_pcb_messages=True,
        pcb_trace_log_path=str(tmp_path / "pcb_websocket_trace.jsonl"),
    )
    ws = _FakeWS()
    session_id = "sess-trace-routing"
    adapter._connections[session_id] = (ws, "proj-trace-001")

    result = await adapter.send(
        chat_id=session_id,
        content=(
            "布线完成。\n\n"
            "##PCB_FIELDS##\n"
            '{"routingResult":"F:\\\\router_work\\\\routing_input.txt"}\n'
            "##PCB_FIELDS_END##"
        ),
    )

    assert result.success is True
    trace_path = tmp_path / "pcb_websocket_trace.jsonl"
    event = json.loads(trace_path.read_text(encoding="utf-8").strip())
    assert event["delivered"] is True
    assert event["reason"] == "sent"
    assert event["sessionId"] == session_id
    assert event["projectid"] == "proj-trace-001"
    assert event["fieldKeys"] == ["routingResult"]
    assert event["routingResult"] == "F:\\router_work\\routing_input.txt"


def test_pcb_outbound_trace_log(tmp_path):
    asyncio.get_event_loop().run_until_complete(_run_pcb_outbound_trace_log(tmp_path))


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
    assert adapter._session_flow_states.get(session_id) == "wait_router_type"

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
async def test_plain_greeting_skips_route_intent_llm():
    adapter = _make_adapter(route_intent_llm_enabled=True)
    seen = {}

    async def fail_classify(**kwargs):
        raise AssertionError("plain greeting should not call route intent LLM")

    async def handler(event):
        seen["text"] = event.text
        seen["raw"] = event.raw_message
        return "你好，我在。"

    adapter._classify_route_intent_with_llm = fail_classify
    adapter.set_message_handler(handler)
    ws = _FakeWS()
    adapter._connections["sess-greeting"] = (ws, "proj-greeting")

    await adapter._handle_user_message(
        {"type": "message", "body": {"role": "user", "content": "你好"}},
        "sess-greeting",
        "proj-greeting",
    )

    assert seen["text"] == "你好"
    assert seen["raw"]["options"]["route_mode"] == "chat"
    assert ws.sent[-1]["body"]["content"] == "你好，我在。"
    assert ws.sent[-1]["body"]["isFinal"] is True


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


@pytest.mark.asyncio
async def test_import_fanout_result_calls_import_lines():
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-import-lines"
    adapter._connections[session_id] = (ws, "proj-import-lines")

    task = asyncio.create_task(
        adapter._import_fanout_result(
            session_id,
            {"successPins": ["U27.B13"], "failedPins": ["U27.B27"]},
            {"routingResult": r"F:\router_work\routing_input.txt"},
        )
    )
    for _ in range(5):
        await asyncio.sleep(0)
        if len(ws.sent) >= 2:
            break

    status_msg = next(item for item in ws.sent if item["type"] == "message")
    assert status_msg["type"] == "message"
    assert status_msg["body"]["content"] == "正在导入版图，请稍候..."
    assert status_msg["body"]["isFinal"] is False

    sent = next(item for item in ws.sent if item["type"] == "tool-calls")
    assert sent["type"] == "tool-calls"
    assert sent["body"]["content"]["name"] == "importLines"
    assert sent["body"]["content"]["arguments"] == {
        "filePath": r"F:\router_work\routing_input.txt",
        "successPins": ["U27.B13"],
        "failedPins": ["U27.B27"],
    }

    adapter._resolve_tool_result(
        json.loads(_tool_result(sent["body"]["content"]["id"], {"success": True, "message": "导入完成"}))
    )
    status = await task
    assert "导入完成" in status


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
    "text",
    [
        "BGA逃逸",
        "BGA扇出",
        "逃逸布线",
        "PCB布线",
        "fanout",
    ],
)
def test_short_pcb_commands_enter_fanout_flow(text):
    adapter = _make_adapter()

    decision = adapter._decide_route("sess-short-pcb-command", text)

    assert decision.mode == "pcb"
    assert decision.intent == "pcb_entry"
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
