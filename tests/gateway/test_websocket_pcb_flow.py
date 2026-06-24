"""End-to-end tests for the WebSocket PCB routing protocol."""

from __future__ import annotations

import asyncio
import json
import socket
import tempfile
from pathlib import Path
from typing import Any

import aiohttp
import pytest

from gateway.config import PlatformConfig
from gateway.platforms.websocket import WebSocketAdapter




def test_websocket_swsd_intent_model_uses_tool_planning_chat_when_enabled():
    from agent.swsd.pcb_intent_agent_loop import ToolPlanningChatIntentModel

    enabled = _make_adapter(route_intent_llm_enabled=True)
    disabled = _make_adapter(route_intent_llm_enabled=False)

    assert isinstance(enabled._swsd_intent_model, ToolPlanningChatIntentModel)
    assert disabled._swsd_intent_model is None

def _fanout_params_body(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value.get("fanoutParams") if isinstance(value.get("fanoutParams"), dict) else value
    if isinstance(value, str):
        parsed = json.loads(value)
        return parsed.get("fanoutParams") if isinstance(parsed.get("fanoutParams"), dict) else parsed
    raise AssertionError(f"unexpected fanoutParams type: {type(value).__name__}")


def _normalize_skill_ids(values: list[str] | tuple[str, ...] | None) -> list[str]:
    return [str(item).replace(chr(92), "/") for item in (values or [])]


def _assert_contains_skills(actual: list[str], expected: list[str]) -> None:
    normalized = _normalize_skill_ids(actual)
    assert all(item in normalized for item in expected), normalized


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


def _valid_reroute_import_text() -> str:
    return "TOP!LINE!0!net13!10.00!10.00!20.00!20.00!6.00\n"


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
        "fanout_param_llm_enabled": False,
        "trace_pcb_messages": False,
    }
    merged_extra.update(extra)
    adapter = WebSocketAdapter(PlatformConfig(enabled=True, extra=merged_extra))
    adapter._allow_legacy_route_decision = True
    return adapter


async def _run_route_intent_llm_uses_tool_planning_chat_stage(monkeypatch):
    from tools import pcb_model_runtime

    adapter = _make_adapter(route_intent_llm_enabled=True)
    captured: dict[str, Any] = {}

    def fake_chat_completion_text(**kwargs):
        captured.update(kwargs)
        return (
            '{"intent":"pcb_entry","route_mode":"pcb","confidence":0.92,'
            '"should_call_get_project_data":true}',
            {"stage": kwargs["stage"]},
        )

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)

    intent = await adapter._classify_route_intent_with_llm(
        session_id="sess-stage-route-intent",
        user_text="帮我做 BGA 逃逸布线",
        project_id="proj",
    )

    assert intent.intent == "pcb_entry"
    assert captured["stage"] == pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT


def test_route_intent_llm_uses_tool_planning_chat_stage(monkeypatch):
    asyncio.get_event_loop().run_until_complete(
        _run_route_intent_llm_uses_tool_planning_chat_stage(monkeypatch)
    )


async def _run_fanout_param_llm_uses_tool_planning_chat_stage(monkeypatch):
    from tools import pcb_model_runtime

    adapter = _make_adapter(fanout_param_llm_enabled=True)
    captured: dict[str, Any] = {}

    def fake_chat_completion_text(**kwargs):
        captured.update(kwargs)
        return (
            '{"fanoutParams":{"selectedBGA":"U22","routerType":"135",'
            '"orderLines":[{"net":"GND","layer":"Top","order":1}],'
            '"constraints":{"LineWidth":4,"LineSpacing":3}}}',
            {"stage": kwargs["stage"]},
        )

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)

    params = await adapter._generate_fanout_params_candidate(
        session_id="sess-stage-fanout",
        selected_bga="U22",
        router_type="135",
        board_summary={"netSummary": {"groundNets": ["GND"]}},
        fanout_context={"recommendedEscapeLayers": ["Top"]},
    )

    assert params["selectedBGA"] == "U22"
    assert captured["stage"] == pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT


def test_fanout_param_llm_uses_tool_planning_chat_stage(monkeypatch):
    asyncio.get_event_loop().run_until_complete(
        _run_fanout_param_llm_uses_tool_planning_chat_stage(monkeypatch)
    )


async def _run_websocket_pcb_flow_round_trip(monkeypatch) -> None:
    """Covers Agent-loop selection -> fanoutParams -> routingResult over real WebSocket I/O."""
    port = _free_port()
    adapter = _make_adapter(port, bootstrap_get_project=False)

    session_id = "sess-pcb-1"
    project_id = "proj-autotest-001"
    observed_auto_skill: list[Any] = []
    observed_text: list[str] = []

    fanout_params = {
        "selectedBGA": "U27",
        "routerType": "arc",
        "orderLines": [
            {"net": "GND", "layer": "SIG03", "order": 1},
            {"net": "VCC", "layer": "SIG04", "order": 2},
            {"net": "DDR_D0", "layer": "SIG03", "order": 3},
        ],
        "constraints": {"LineWidth": 4, "LineSpacing": 3},
    }
    route_result = {
        "routingResult": r"F:\router_work\routing_input.txt",
        "importLinesFilePath": r"F:\router_work\ARC_output.txt",
        "report": "布线连通率: 100%",
    }
    async def handler(event):
        observed_auto_skill.append(event.auto_skill)
        observed_text.append(event.text)
        text = event.text
        if "帮我进行BGA逃逸布线" in text:
            return (
                "请选择一个 BGA 进行布线。\n\n"
                "##PCB_FIELDS##\n"
                + json.dumps(
                    {
                        "selection": [
                            {"label": "U27", "detail": "BGA-256, 1.0mm pitch"},
                            {"label": "U35", "detail": "BGA-484, 0.8mm pitch"},
                        ]
                    },
                    ensure_ascii=False,
                )
                + "\n##PCB_FIELDS_END##"
            )
        if "选择 U27" in text:
            return "已选择目标 BGA：U27。\n\n请选择走线算法类型和层分配/逃逸顺序生成模块。"
        if "arc + 北科大" in text:
            return (
                "已生成扇出参数，请确认。\n\n"
                "##PCB_FIELDS##\n"
                + json.dumps({"fanoutParams": fanout_params}, ensure_ascii=False)
                + "\n##PCB_FIELDS_END##"
            )
        if "确认" in text:
            from tools import pcb_tools

            pcb_tools._transport.set_pending_pcb_fields(route_result, session_id=session_id)
            return "布线完成。"
        raise AssertionError(f"Unexpected Agent-loop event text: {text}")

    adapter.set_message_handler(handler)
    async def fake_direct_fanout_step(session_id: str, user_text: str) -> bool:
        return False

    async def fake_cached_fanout_route(session_id: str) -> bool:
        return False

    monkeypatch.setattr(adapter, "_run_direct_fanout_param_step", fake_direct_fanout_step)
    monkeypatch.setattr(adapter, "_run_cached_fanout_route", fake_cached_fanout_route)
    await adapter.connect()

    try:
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(_user_message(session_id, project_id, "帮我进行BGA逃逸布线"))

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

                await ws.send_str(_user_message(session_id, project_id, "arc + 北科大"))
                fanout_msg = await _recv_json(ws)
                assert fanout_msg["type"] == "message"
                normalized_fanout = _fanout_params_body(fanout_msg["body"]["fanoutParams"])
                assert normalized_fanout["selectedBGA"] == fanout_params["selectedBGA"]
                assert normalized_fanout["routerType"] == fanout_params["routerType"]
                assert normalized_fanout["constraints"] == fanout_params["constraints"]
                assert normalized_fanout.get("routeAlgorithm") in {None, "arc"}
                assert normalized_fanout.get("fanoutModule") in {None, "北科大"}

                await ws.send_str(_user_message(session_id, project_id, "确认"))
                routed_msg = await _recv_json(ws)
                saw_import_status = False
                while routed_msg["type"] == "message":
                    content = routed_msg["body"].get("content", "")
                    if content == "正在导入版图，请稍候...":
                        saw_import_status = True
                        assert routed_msg["body"]["isFinal"] is False
                    else:
                        raise AssertionError(f"Unexpected message before importLines: {routed_msg}")
                    routed_msg = await _recv_json(ws)
                assert saw_import_status
                assert routed_msg["type"] == "tool-calls"
                assert routed_msg["body"]["content"]["name"] == "importLines"
                assert routed_msg["body"]["content"]["arguments"] == {
                    "filePath": route_result["importLinesFilePath"],
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
                assert route_result["report"] in routed_msg["body"]["report"]
                assert "导入完成" in routed_msg["body"]["report"]

    finally:
        await adapter.disconnect()

    assert len(observed_auto_skill) == 3
    for skills in observed_auto_skill:
        _assert_contains_skills(skills, ["hardware/pcb-reroute", "hardware/pcb-intelligence"])
    assert observed_text[0].startswith("[SYSTEM: 当前消息来自启云方 WebSocket PCB 客户端。")
    assert "projectid: proj-autotest-001" in observed_text[0]


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


@pytest.mark.parametrize("text", ["#reroute", "＃reroute", "#reroute；", "#拆线重布", "＃拆线重布", "拆线重布"])
def test_websocket_reroute_can_start_without_existing_selection(text):
    adapter = _make_adapter()

    decision = adapter._decide_route("sess-reroute-no-selection", text)

    assert decision.mode == "pcb"
    assert decision.intent == "pcb_reroute_selected"
    assert decision.bootstrap_get_project is False


@pytest.mark.parametrize(
    "text",
    [
        "#逃逸布线",
        "＃逃逸布线",
        "#逃逸 布线；",
        "#全局fanout",
        "＃全局 fanout",
        "逃逸布线",
        "#逃逸布线，告诉我什么是BGA逃逸布线",
    ],
)
def test_websocket_hashtag_forces_global_fanout_skill(text):
    adapter = _make_adapter()

    decision = adapter._decide_route("sess-force-fanout", text)

    assert decision.mode == "pcb"
    assert decision.intent == "pcb_entry"
    assert decision.reason == "forced_global_fanout"
    assert decision.bootstrap_get_project is True


@pytest.mark.parametrize("text", ["#布线 是什么意思", "#逃逸布线abc", "#全局fanoutabc", "#rerouteabc", "#拆线重布abc"])
def test_unknown_or_partial_hashtag_commands_do_not_force_pcb_skill(text):
    adapter = _make_adapter()

    decision = adapter._decide_route("sess-unknown-hashtag", text)

    assert decision.reason != "forced_global_fanout"
    assert decision.intent != "pcb_reroute_selected"


def test_websocket_escape_routing_phrase_without_hash_forces_global_fanout():
    adapter = _make_adapter()

    decision = adapter._decide_route("sess-force-escape-routing", "逃逸布线")

    assert decision.mode == "pcb"
    assert decision.intent == "pcb_entry"
    assert decision.reason == "forced_global_fanout"
    assert decision.bootstrap_get_project is True


@pytest.mark.parametrize(
    "text",
    [
        "对U5逃逸布线",
        "对 U5 逃逸布线",
        "帮我对U5做BGA逃逸布线",
        "给 U5 做 fanout",
        "U5 扇出",
    ],
)
def test_targeted_bga_natural_language_forces_global_fanout(text):
    adapter = _make_adapter()

    decision = adapter._decide_route("sess-targeted-fanout", text)

    assert decision.mode == "pcb"
    assert decision.intent == "pcb_entry"
    assert decision.reason == "forced_global_fanout"
    assert decision.bootstrap_get_project is True
    assert adapter._session_requested_bga_targets["sess-targeted-fanout"] == "U5"


@pytest.mark.parametrize("text", ["什么是逃逸布线", "不要布线，只解释一下逃逸布线原理"])
def test_escape_routing_concept_or_noop_does_not_force_global_fanout(text):
    adapter = _make_adapter()

    decision = adapter._decide_route("sess-escape-chat", text)

    assert decision.mode == "chat"
    assert decision.reason != "forced_global_fanout"


def test_targeted_bga_concept_question_stays_chat():
    adapter = _make_adapter()

    decision = adapter._decide_route("sess-targeted-chat", "什么是 U5 逃逸布线")

    assert decision.mode == "chat"
    assert decision.reason != "forced_global_fanout"
    assert "sess-targeted-chat" not in adapter._session_requested_bga_targets


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
    assert adapter._session_flow_states[session_id] == "reroute"
    assert session_id not in adapter._session_fanout_params


def test_websocket_cancel_and_reroute_phrase_switches_task():
    adapter = _make_adapter()
    session_id = "sess-reroute-switch"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")
    adapter._session_fanout_params[session_id] = {
        "selectedBGA": "U22",
        "routerType": "135",
    }

    decision = adapter._decide_route(session_id, "取消当前，#reroute")

    assert decision.mode == "pcb"
    assert decision.intent == "pcb_reroute_selected"
    assert decision.reason == "pcb_reroute_selected"
    assert adapter._session_flow_states[session_id] == "reroute"
    assert session_id not in adapter._session_fanout_params


def test_websocket_natural_language_task_switch_resets_fanout_flow():
    adapter = _make_adapter()
    session_id = "sess-reroute-switch-natural"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")
    adapter._session_fanout_params[session_id] = {
        "selectedBGA": "U22",
        "routerType": "135",
    }

    decision = adapter._decide_route(session_id, "先不做 BGA 了，改成拆线重布")

    assert decision.mode == "pcb"
    assert decision.intent == "pcb_reroute_selected"
    assert decision.reason == "pcb_reroute_selected"
    assert adapter._session_flow_states[session_id] == "reroute"
    assert session_id not in adapter._session_fanout_params


def test_websocket_temporary_chat_preserves_pcb_flow_state():
    adapter = _make_adapter()
    session_id = "sess-temp-chat"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_router_type")

    chat_decision = adapter._decide_route(session_id, "解释一下 RL 是什么意思")

    assert chat_decision.mode == "chat"
    assert chat_decision.reason == "temporary_chat"
    assert adapter._session_flow_states[session_id] == "wait_router_type"

    followup_decision = adapter._decide_route(session_id, "135 + RL")

    assert followup_decision.mode == "pcb"
    assert followup_decision.reason == "router_type_step"
    assert followup_decision.intent == "pcb_followup"


def test_websocket_temporary_chat_preserves_wait_confirm_flow_state():
    adapter = _make_adapter()
    session_id = "sess-temp-chat-confirm"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")
    adapter._session_fanout_params[session_id] = {
        "selectedBGA": "U22",
        "routerType": "135",
    }

    chat_decision = adapter._decide_route(session_id, "这个 RL 是什么意思？")

    assert chat_decision.mode == "chat"
    assert chat_decision.reason == "temporary_chat"
    assert adapter._session_flow_states[session_id] == "wait_confirm"
    assert adapter._session_fanout_params[session_id]["selectedBGA"] == "U22"


def test_websocket_confirm_uses_cached_fanout_params_even_if_waiting_router_type():
    adapter = _make_adapter()
    session_id = "sess-confirm-cached-fanout"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_router_type")
    adapter._session_fanout_params[session_id] = {
        "selectedBGA": "U22",
        "routerType": "135",
        "orderLines": [{"net": "N1", "layer": "Top", "order": 1}],
    }

    decision = adapter._decide_route(session_id, "确认")

    assert decision.mode == "pcb"
    assert decision.reason == "confirm_route"
    assert decision.immediate_reply is None
    assert adapter._session_flow_states[session_id] == "routing"


def test_websocket_body_fanout_params_are_cached_before_confirm():
    adapter = _make_adapter()
    session_id = "sess-body-fanout-cache"

    adapter._remember_fanout_params_from_frontend(
        session_id,
        {
            "selectedBGA": "U22",
            "routerType": "rl",
            "orderLines": [{"net": "N1", "layer": "Art03", "order": 1}],
        },
    )
    decision = adapter._decide_route(session_id, "确认")

    assert decision.reason == "confirm_route"
    assert adapter._session_fanout_params[session_id]["selectedBGA"] == "U22"
    assert adapter._session_router_types[session_id] == "rl"
    assert adapter._session_flow_states[session_id] == "routing"


def test_websocket_cancel_requires_explicit_flow_cancel_phrase():
    adapter = _make_adapter()
    session_id = "sess-cancel-guard"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_router_type")

    chat_decision = adapter._decide_route(session_id, "这个停止条件是什么意思？")

    assert chat_decision.mode == "chat"
    assert chat_decision.reason == "temporary_chat"
    assert adapter._session_flow_states[session_id] == "wait_router_type"

    cancel_decision = adapter._decide_route(session_id, "取消当前布线流程")

    assert cancel_decision.mode == "chat"
    assert cancel_decision.reason == "cancel_flow"
    assert cancel_decision.intent == "cancel"
    assert adapter._session_flow_states[session_id] == "idle"


def test_websocket_natural_language_bga_reselect_invalidates_fanout_params():
    adapter = _make_adapter()
    session_id = "sess-bga-reselect"
    adapter._session_selection_labels[session_id] = ("U22", "U23")
    adapter._session_selected_targets[session_id] = "U22"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")
    adapter._session_fanout_params[session_id] = {
        "selectedBGA": "U22",
        "routerType": "135",
    }

    decision = adapter._decide_route(session_id, "目标 BGA 改成 U23")

    assert decision.mode == "pcb"
    assert decision.reason in {"reselect_before_confirm", "escape_change_target"}
    assert decision.immediate_reply
    assert adapter._session_selected_targets[session_id] == "U23"
    assert adapter._session_flow_states[session_id] == "wait_router_type"
    assert adapter._session_fanout_params[session_id]["selectedBGA"] == "U23"
    assert adapter._session_fanout_params[session_id]["routerType"] == "135"


def test_websocket_explicit_reroute_overrides_bad_llm_chat_intent():
    adapter = _make_adapter()

    decision = adapter._decide_route(
        "sess-reroute-llm-chat",
        "请帮我针对版图数据中的 BGA U2 的 net13、net17 拆线后重新布线",
        llm_intent={
            "intent": "chat",
            "route_mode": "chat",
            "confidence": 0.91,
            "should_call_get_project_data": False,
        },
    )
    assert decision.mode == "pcb"
    assert decision.intent == "pcb_reroute_selected"
    assert decision.bootstrap_get_project is False


def test_websocket_reroute_llm_intent_handles_ambiguous_followup():
    adapter = _make_adapter()

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


async def _run_websocket_reroute_fields_round_trip(monkeypatch) -> None:
    """Reroute is SWSD-controlled: delete result continues to reroute final without auto import."""
    from tools import pcb_tools

    adapter = _make_adapter(bootstrap_get_project=False)
    ws = _FakeWS()
    session_id = "sess-reroute-round-trip"
    project_id = "proj-reroute-round-trip"
    adapter._connections[session_id] = (ws, project_id)

    reroute_fields = {
        "rerouteResult": {
            "type": "local_reroute_completion",
            "status": "local_completion_passed",
            "selectedNets": ["net13", "net17"],
            "operations": [{"action": "complete_local_route_for_net", "net": "net13"}],
            "drcPassed": True,
            "importPending": True,
        },
        "routedLayoutTxtFilePath": r"F:\public\routed.txt",
        "importLinesFilePath": r"F:\public\reroute_line.out",
        "checkReport": {"passed": True, "checks": []},
        "explanation": "局部布线完善已完成。",
        "report": "局部布线完善已完成，DRC 通过，已生成可导入 txt。",
    }

    def fake_reroute(user_data="", session_id=None):
        return json.dumps(reroute_fields, ensure_ascii=False)

    monkeypatch.setattr(pcb_tools, "reroute", fake_reroute)

    task = asyncio.create_task(adapter._run_direct_reroute_delete_step(session_id))
    for _ in range(10):
        await asyncio.sleep(0)
        tool_calls = [item for item in ws.sent if item.get("type") == "tool-calls"]
        if tool_calls:
            break

    tool_calls = [item for item in ws.sent if item.get("type") == "tool-calls"]
    assert len(tool_calls) == 1
    assert tool_calls[0]["body"]["content"]["name"] == "deleteTracesForRerouting"
    call_id = tool_calls[0]["body"]["content"]["id"]

    result_payload = {
        "missing_routes": [
            {"net_name": "net13"},
            {"net_name": "net17"},
        ],
        "projectData": "(board after delete)",
        "localContext": {"source": "unit_test"},
    }
    adapter._resolve_tool_result(
        {
            "type": "tool-results",
            "sessionId": session_id,
            "projectid": project_id,
            "body": {
                "role": "tool",
                "sessionId": session_id,
                "projectid": project_id,
                "content": {"id": call_id, "result": result_payload},
            },
        }
    )
    assert await task is True

    await adapter._swsd_runtime_bridge.handle_reroute_delete_result(
        {
            "type": "tool-results",
            "sessionId": session_id,
            "projectid": project_id,
            "body": {
                "role": "tool",
                "sessionId": session_id,
                "projectid": project_id,
                "content": {"id": call_id, "result": result_payload},
            },
        },
        result_payload,
    )

    msg = {"body": adapter._last_direct_reroute_fields[session_id]}
    assert msg["body"]["rerouteResult"]["type"] == "local_reroute_completion"
    assert msg["body"]["rerouteResult"]["status"] == "local_completion_passed"
    assert msg["body"]["rerouteResult"]["selectedNets"] == ["net13", "net17"]
    assert msg["body"]["rerouteResult"]["operations"] == [
        {"action": "complete_local_route_for_net", "net": "net13"}
    ]
    assert msg["body"]["routedLayoutTxtFilePath"] == r"F:\public\routed.txt"
    assert msg["body"]["importLinesFilePath"] == r"F:\public\reroute_line.out"
    assert msg["body"]["checkReport"]["passed"] is True
    assert msg["body"]["checkReport"]["checks"] == []
    assert "局部布线完善" in msg["body"]["explanation"]
    assert msg["body"]["report"] == reroute_fields["report"]
    assert not msg["body"]["report"].lstrip().startswith("{")
    assert ".kicad_pcb" not in json.dumps(msg["body"], ensure_ascii=False)
    assert not any(
        item.get("type") == "tool-calls"
        and item.get("body", {}).get("content", {}).get("name") == "importLines"
        for item in ws.sent
    )

def test_websocket_reroute_fields_round_trip(monkeypatch):
    asyncio.get_event_loop().run_until_complete(_run_websocket_reroute_fields_round_trip(monkeypatch))


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


async def _run_websocket_reroute_flow_drops_fanout_fields() -> None:
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-reroute-drop-fanout-fields"
    adapter._connections[session_id] = (ws, "proj-reroute-drop-fanout")
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "reroute")

    result = await adapter.send(
        chat_id=session_id,
        content=(
            "已选择目标 BGA：U22。\n\n请选择走线算法类型。\n\n"
            "##PCB_FIELDS##\n"
            + json.dumps(
                {
                    "selection": [{"label": "U22", "detail": "BGA"}],
                    "fanoutParams": {"selectedBGA": "U22", "routerType": "135"},
                    "routingResult": r"F:\router_work\line.out",
                },
                ensure_ascii=False,
            )
            + "\n##PCB_FIELDS_END##"
        ),
        metadata={"stream_is_final": True},
    )

    assert result.success is True
    assert len(ws.sent) == 1
    body = ws.sent[0]["body"]
    assert "selection" not in body
    assert "fanoutParams" not in body
    assert "routingResult" not in body
    assert "拆线重布流程" in body["content"]


def test_websocket_reroute_flow_drops_fanout_fields():
    asyncio.get_event_loop().run_until_complete(_run_websocket_reroute_flow_drops_fanout_fields())


async def _run_websocket_reroute_failure_emits_error_message() -> None:
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-reroute-error-protocol"
    adapter._connections[session_id] = (ws, "proj-reroute-error-protocol")
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "reroute")

    result = await adapter.send(
        chat_id=session_id,
        content="拆线重布未能继续：未检测到框选走线\n请先在前端框选需要重布的走线后再试。",
        metadata={"stream_is_final": True},
    )

    assert result.success is True
    assert len(ws.sent) == 1
    assert ws.sent[0]["type"] == "message"
    body = ws.sent[0]["body"]
    assert body["role"] == "agent"
    assert body["rerouteResult"]["status"] == "blocked_missing_selection"
    assert body["rerouteResult"]["recoverable"] is True
    assert "未检测到框选走线" in body["rerouteResult"]["reason"]
    assert body["checkReport"]["passed"] is False
    assert "未检测到框选走线" in body["explanation"]
def test_websocket_reroute_failure_emits_error_message():
    asyncio.get_event_loop().run_until_complete(_run_websocket_reroute_failure_emits_error_message())


async def _run_websocket_reroute_auth_failure_is_normalized() -> None:
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-reroute-auth-failure"
    adapter._connections[session_id] = (ws, "proj-reroute-auth-failure")
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "reroute")

    result = await adapter.send(
        chat_id=session_id,
        content="❌ Non-retryable error (HTTP 401): HTTP 401: AppKey不存在，modelGenerationFailure",
        metadata={"stream_is_final": True},
    )

    assert result.success is True
    assert len(ws.sent) == 1
    body = ws.sent[0]["body"]
    assert body["rerouteResult"]["recoverable"] is True
    assert body["rerouteResult"]["status"] in {"reroute_finalize_failed", "drc_passed_import_pending"}
    assert "401" in body["rerouteResult"]["reason"]
    assert "checkReport" in body
    assert "explanation" in body


def test_websocket_reroute_auth_failure_is_normalized():
    asyncio.get_event_loop().run_until_complete(_run_websocket_reroute_auth_failure_is_normalized())

async def _run_websocket_reroute_txt_with_failed_drc_skips_import() -> None:
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-reroute-failed-txt-no-import"
    adapter._connections[session_id] = (ws, "proj-reroute-failed-txt")

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
                        "routedLayoutTxtFilePath": r"F:\public\failed.txt",
                    },
                    "routedLayoutTxtFilePath": r"F:\public\failed.txt",
                    "checkReport": {"passed": False, "checks": []},
                    "explanation": "DRC 未通过，不应调用 importLines。",
                },
                ensure_ascii=False,
            )
            + "\n##PCB_FIELDS_END##"
        ),
        metadata={"stream_is_final": True},
    )

    assert result.success is True
    assert len(ws.sent) == 1
    body = ws.sent[0]["body"]
    assert body["rerouteResult"]["drcPassed"] is False
    assert "routedLayoutTxtFilePath" not in body
    assert "routedLayoutTxtFilePath" not in body["rerouteResult"]
    assert body["explanation"] == "DRC 未通过，不应调用 importLines。"


def test_websocket_reroute_txt_with_failed_drc_skips_import():
    asyncio.get_event_loop().run_until_complete(_run_websocket_reroute_txt_with_failed_drc_skips_import())


async def _run_websocket_reroute_bypasses_agent_loop() -> None:
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

                msg = await _recv_json(ws)
                assert msg["type"] == "tool-calls"
                assert msg["body"]["content"]["name"] == "deleteTracesForRerouting"
                await ws.send_str(
                    _tool_result(
                        msg["body"]["content"]["id"],
                        {"missing_routes": [], "projectData": "", "localContext": {"source": "unit_test"}},
                    )
                )
    finally:
        await adapter.disconnect()

    assert observed_auto_skill == []


def test_websocket_reroute_bypasses_agent_loop():
    asyncio.get_event_loop().run_until_complete(_run_websocket_reroute_bypasses_agent_loop())


async def _run_websocket_chat_turn_uses_chat_mode_without_pcb_skills() -> None:
    """SWSD Controller chat branch short-circuits with immediate_reply."""
    port = _free_port()
    adapter = _make_adapter(port)

    session_id = "sess-chat-1"
    project_id = "proj-chat-001"
    handler_called = False

    def fake_chat_agent(event, plan):
        assert plan.phase == "chat"
        return "websocket-chat-reply"

    async def handler(event):
        nonlocal handler_called
        handler_called = True
        raise AssertionError("SWSD chat immediate_reply should not enter agent handler")

    adapter._swsd_workflow_controller._run_chat_agent = fake_chat_agent
    adapter.set_message_handler(handler)
    await adapter.connect()

    try:
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(_user_message(session_id, project_id, "?????"))
                msg = await _recv_json(ws)
                assert msg["type"] == "message"
                assert msg["body"]["content"] == "websocket-chat-reply"
                assert msg["body"]["isFinal"] is True
    finally:
        await adapter.disconnect()

    assert handler_called is False

def test_websocket_chat_turn_uses_chat_mode_without_pcb_skills():
    asyncio.get_event_loop().run_until_complete(_run_websocket_chat_turn_uses_chat_mode_without_pcb_skills())


async def _run_websocket_slash_command_passthrough() -> None:
    """Slash commands from the WebSocket client must reach gateway dispatch
    unwrapped so /skill-name commands can be resolved."""
    port = _free_port()
    adapter = _make_adapter(port)

    seen: dict[str, Any] = {}

    async def handler(event):
        seen["text"] = event.text
        seen["auto_skill"] = event.auto_skill
        seen["options"] = event.raw_message.get("options", {})
        return "slash-ok"

    adapter.set_message_handler(handler)
    await adapter.connect()

    try:
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(_user_message("sess-slash-1", "proj-slash-1", "/fresh-skill inspect this"))
                msg = await _recv_json(ws)
                assert msg["type"] == "message"
                assert msg["body"]["content"] == "slash-ok"
    finally:
        await adapter.disconnect()

    assert seen["text"] == "/fresh-skill inspect this"
    assert seen["auto_skill"] is None
    assert seen["options"]["route_mode"] == "chat"
    assert seen["options"]["pcb_agent_loop"] is False


def test_websocket_slash_command_passthrough():
    asyncio.get_event_loop().run_until_complete(_run_websocket_slash_command_passthrough())


async def _run_websocket_turn_options_passthrough() -> None:
    """WebSocket body.options 应透传到 MessageEvent.raw_message.options。"""
    port = _free_port()
    adapter = _make_adapter(port)

    captured = {}
    handler_called = False

    def fake_chat_agent(event, plan):
        captured["turn_options"] = dict(event.turn_options)
        return "ok"

    async def handler(event):
        nonlocal handler_called
        handler_called = True
        raise AssertionError("SWSD chat immediate_reply should not enter agent handler")

    adapter._swsd_workflow_controller._run_chat_agent = fake_chat_agent
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
                msg = await _recv_json(ws)
                assert msg["body"]["content"] == "ok"
    finally:
        await adapter.disconnect()

    assert handler_called is False
    assert captured["turn_options"] == {
        "streaming": False,
        "thinking": True,
        "reasoningEffort": "high",
    }


def test_websocket_turn_options_passthrough():
    asyncio.get_event_loop().run_until_complete(_run_websocket_turn_options_passthrough())


async def _run_websocket_selection_stage_fail_closed() -> None:
    """选择阶段收到“确认”应经 agent loop 返回纠偏提示。"""
    port = _free_port()
    adapter = _make_adapter(port, bootstrap_get_project=False)

    session_id = "sess-fsm-1"
    project_id = "proj-fsm-001"

    async def handler(event):
        _assert_contains_skills(event.auto_skill, ["hardware/pcb-reroute", "hardware/pcb-intelligence"])
        if "帮我进行BGA逃逸布线" in event.text:
            return (
                "已识别到目标 BGA：U27。\n\n"
                "##PCB_FIELDS##\n"
                + json.dumps({"selection": [{"label": "U27", "detail": "BGA-256"}]}, ensure_ascii=False)
                + "\n##PCB_FIELDS_END##"
            )
        if "确认" in event.text:
            return "执行布线前必须先选择走线算法和层分配/逃逸顺序生成模块。"
        raise AssertionError(f"unexpected event text: {event.text}")

    adapter.set_message_handler(handler)
    await adapter.connect()

    try:
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(_user_message(session_id, project_id, "帮我进行BGA逃逸布线"))
                first = await _recv_json(ws)
                assert first["type"] == "message"
                assert first["body"]["selection"] == [{"label": "U27", "detail": "BGA-256"}]

                await ws.send_str(_user_message(session_id, project_id, "确认"))
                second = await _recv_json(ws)
                assert second["type"] == "message"
                assert "当前还在选择阶段，请先回复器件" in second["body"]["content"]
    finally:
        await adapter.disconnect()


def test_websocket_selection_stage_fail_closed():
    asyncio.get_event_loop().run_until_complete(_run_websocket_selection_stage_fail_closed())


async def _run_websocket_selection_accepts_non_u_refdes() -> None:
    """Agent-loop 选择阶段应接受 selection 列表里的任意合法位号，而不只 U+数字。"""
    port = _free_port()
    adapter = _make_adapter(port, bootstrap_get_project=False)

    session_id = "sess-fsm-fpga"
    project_id = "proj-fsm-fpga"

    async def handler(event):
        _assert_contains_skills(event.auto_skill, ["hardware/pcb-reroute", "hardware/pcb-intelligence"])
        if "帮我进行BGA逃逸布线" in event.text:
            return (
                "已识别到目标 BGA：FPGA1。\n\n"
                "##PCB_FIELDS##\n"
                + json.dumps({"selection": [{"label": "FPGA1", "detail": "BGA-1156"}]}, ensure_ascii=False)
                + "\n##PCB_FIELDS_END##"
            )
        if "选择 FPGA1" in event.text:
            return "已选择目标 BGA：FPGA1。\n\n请回复例如：`135 + RL`、`arc + 北科大`。"
        raise AssertionError(f"unexpected event text: {event.text}")

    adapter.set_message_handler(handler)
    await adapter.connect()

    try:
        uri = f"http://127.0.0.1:{port}"
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(uri, heartbeat=None, autoping=False) as ws:
                await ws.send_str(_user_message(session_id, project_id, "帮我进行BGA逃逸布线"))
                first = await _recv_json(ws)
                assert first["type"] == "message"
                assert first["body"]["selection"] == [{"label": "FPGA1", "detail": "BGA-1156"}]

                await ws.send_str(_user_message(session_id, project_id, "选择 FPGA1"))
                second = await _recv_json(ws)
                assert second["type"] == "message"
                assert "已选择目标 BGA：FPGA1" in second["body"]["content"]
                assert "请回复例如：`135 + RL`、`arc + 北科大`" in second["body"]["content"]
    finally:
        await adapter.disconnect()


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

    decision = adapter._decide_route(session_id, "135 + 北科大")

    assert decision.mode == "pcb"
    assert decision.reason == "router_type_step"
    assert adapter._session_router_types[session_id] == "135"
    assert adapter._session_flow_states[session_id] == "wait_router_type"


@pytest.mark.asyncio
async def test_handle_user_message_router_choice_runs_direct_fanout_step(monkeypatch):
    adapter = _make_adapter()
    session_id = "sess-router-choice-direct"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_router_type")
    adapter._session_selection_labels[session_id] = ("U22",)
    adapter._session_selected_targets[session_id] = "U22"
    seen = {}

    async def fake_direct_fanout(sid, text):
        seen["session_id"] = sid
        seen["text"] = text
        return True

    async def handler(event):
        raise AssertionError("router choice should not be sent back to the model handler")

    monkeypatch.setattr(adapter, "_run_direct_fanout_param_step", fake_direct_fanout)
    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {
            "type": "message",
            "body": {"role": "user", "content": "135 + RL"},
        },
        session_id,
        "proj-router-choice-direct",
    )

    assert seen == {"session_id": session_id, "text": "135 + RL"}


@pytest.mark.asyncio
async def test_frontend_confirmed_fanout_params_runs_cached_route(monkeypatch):
    adapter = _make_adapter()
    session_id = "sess-frontend-confirmed-fanout"
    seen = {}

    async def fake_route(sid):
        seen["session_id"] = sid
        return True

    async def handler(event):
        raise AssertionError("frontend-confirmed fanoutParams should not be sent to model handler")

    monkeypatch.setattr(adapter, "_run_cached_fanout_route", fake_route)
    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {
            "type": "message",
            "body": {
                "role": "user",
                "content": "配置已确认，逃逸布线参数已提交。",
                "fanoutParams": {
                    "selectedBGA": "U22",
                    "routerType": "rl",
                    "orderLines": [{"net": "NET_A", "layer": "Top", "order": 1}],
                },
            },
        },
        session_id,
        "proj-frontend-confirmed-fanout",
    )

    assert seen == {"session_id": session_id}
    assert adapter._session_fanout_params[session_id]["selectedBGA"] == "U22"
    assert adapter._session_flow_states[session_id] == "wait_confirm"


@pytest.mark.asyncio
async def test_frontend_confirmed_cached_fanout_without_body_params_runs_route(monkeypatch):
    adapter = _make_adapter()
    session_id = "sess-frontend-confirmed-cached-fanout"
    seen = {}

    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")
    adapter._session_fanout_params[session_id] = {
        "selectedBGA": "U22",
        "routerType": "rl",
        "orderLines": [{"net": "NET_A", "layer": "Top", "order": 1}],
    }

    async def fake_route(sid):
        seen["session_id"] = sid
        return True

    async def handler(event):
        raise AssertionError("frontend-confirmed cached fanout should not be sent to model handler")

    monkeypatch.setattr(adapter, "_run_cached_fanout_route", fake_route)
    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {
            "type": "message",
            "body": {
                "role": "user",
                "content": "配置已确认，逃逸布线参数已提交。",
            },
        },
        session_id,
        "proj-frontend-confirmed-cached-fanout",
    )

    assert seen == {"session_id": session_id}


@pytest.mark.asyncio
async def test_frontend_confirmed_fanout_params_json_content_runs_cached_route(monkeypatch):
    adapter = _make_adapter()
    session_id = "sess-frontend-confirmed-fanout-json-content"
    fanout_params = {
        "selectedBGA": "U5",
        "routerType": "rl",
        "orderLines": [{"net": "NET_A", "layer": "Top", "order": 1}],
        "constraints": {"LineWidth": 3.0, "LineSpacing": 4.0},
    }
    seen = {}

    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")

    async def fake_route(sid):
        seen["session_id"] = sid
        return True

    async def handler(event):
        raise AssertionError("fanoutParams JSON content should not be sent to model handler")

    monkeypatch.setattr(adapter, "_run_cached_fanout_route", fake_route)
    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {
            "type": "message",
            "body": {
                "role": "user",
                "content": json.dumps(fanout_params, ensure_ascii=False),
            },
        },
        session_id,
        "proj-frontend-confirmed-fanout-json-content",
    )

    assert seen == {"session_id": session_id}
    assert adapter._session_fanout_params[session_id]["selectedBGA"] == fanout_params["selectedBGA"]
    assert adapter._session_fanout_params[session_id]["constraints"] == fanout_params["constraints"]


@pytest.mark.asyncio
async def test_injected_fanout_target_change_updates_cached_draft(monkeypatch):
    adapter = _make_adapter()
    session_id = "sess-injected-fanout-change-target"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")
    adapter._session_selection_labels[session_id] = ("U27", "U55")
    sent: list[str] = []

    async def fake_send(chat_id, content, metadata=None):
        sent.append(content)
        return None

    async def handler(event):
        raise AssertionError("injected target change should be handled locally")

    monkeypatch.setattr(adapter, "send", fake_send)
    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {
            "type": "message",
            "body": {
                "role": "user",
                "content": "换成 U55",
                "fanoutParams": {
                    "selectedBGA": "U27",
                    "routerType": "135+RL",
                    "orderLines": [{"net": "GND", "layer": "Top", "order": 1}],
                    "constraints": {"LineWidth": 4, "LineSpacing": 3},
                },
            },
        },
        session_id,
        "proj-injected-fanout-change-target",
    )

    assert adapter._session_selected_targets[session_id] == "U55"
    assert adapter._session_fanout_params[session_id]["selectedBGA"] == "U55"
    assert adapter._session_flow_states[session_id] == "wait_router_type"
    assert sent and "请选择走线算法类型" in sent[0]


@pytest.mark.asyncio
async def test_injected_fanout_router_change_updates_cached_draft(monkeypatch):
    adapter = _make_adapter()
    session_id = "sess-injected-fanout-change-router"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")
    adapter._session_selection_labels[session_id] = ("U27",)
    sent: list[str] = []

    async def fake_send(chat_id, content, metadata=None):
        sent.append(content)
        return None

    async def handler(event):
        raise AssertionError("injected router change should be handled locally")

    monkeypatch.setattr(adapter, "send", fake_send)
    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {
            "type": "message",
            "body": {
                "role": "user",
                "content": "改成 arc",
                "fanoutParams": {
                    "selectedBGA": "U27",
                    "routerType": "135+RL",
                    "orderLines": [{"net": "GND", "layer": "Top", "order": 1}],
                    "constraints": {"LineWidth": 4, "LineSpacing": 3},
                },
            },
        },
        session_id,
        "proj-injected-fanout-change-router",
    )

    assert adapter._session_fanout_params[session_id]["routeAlgorithm"] == "arc"
    assert adapter._session_fanout_params[session_id]["routerType"] == "rl_arc"
    assert adapter._session_flow_states[session_id] == "wait_confirm"
    assert sent and "已更新当前 fanout 配置" in sent[0]


@pytest.mark.asyncio
async def test_injected_fanout_constraint_change_updates_cached_draft(monkeypatch):
    adapter = _make_adapter()
    session_id = "sess-injected-fanout-change-constraint"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")
    adapter._session_selection_labels[session_id] = ("U27",)
    sent: list[str] = []

    async def fake_send(chat_id, content, metadata=None):
        sent.append(content)
        return None

    async def handler(event):
        raise AssertionError("injected constraint change should be handled locally")

    monkeypatch.setattr(adapter, "send", fake_send)
    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {
            "type": "message",
            "body": {
                "role": "user",
                "content": "线宽改成 5",
                "fanoutParams": {
                    "selectedBGA": "U27",
                    "routerType": "135+RL",
                    "orderLines": [{"net": "GND", "layer": "Top", "order": 1}],
                    "constraints": {"LineWidth": 4, "LineSpacing": 3},
                },
            },
        },
        session_id,
        "proj-injected-fanout-change-constraint",
    )

    assert adapter._session_fanout_params[session_id]["constraints"]["LineWidth"] == 5
    assert adapter._session_flow_states[session_id] == "wait_confirm"
    assert sent and "已更新当前 fanout 配置" in sent[0]


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

    assert normalized == "已完成逃逸参数配置，请确认"


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


def test_fanout_candidate_is_validated_and_never_routes_directly():
    adapter = _make_adapter()
    session_id = "sess-validate-fanout"
    adapter._session_selection_labels[session_id] = ("U22",)

    params = adapter._validate_or_build_fanout_params(
        session_id=session_id,
        candidate={
            "selectedBGA": "U1",
            "routerType": "pcb_fanout",
            "routingResult": r"F:\fake\routing_input.txt",
            "orderLines": [{"net": "GND", "layer": "Top", "order": "9"}],
            "constraints": {"LineWidth": 4, "LineSpacing": 3},
        },
        selected_bga="U22",
        router_type="135",
        board_summary={"netSummary": {"groundNets": ["GND"], "powerNets": ["VCC"], "clockNets": []}},
        fanout_context={"recommendedEscapeLayers": ["Top", "Art03"], "recommendedLineWidth": 4, "recommendedLineSpacing": 3},
    )

    assert params == {
        "selectedBGA": "U22",
        "routerType": "135",
        "orderLines": [{"net": "GND", "layer": "Top", "order": 1}],
        "constraints": {"LineWidth": 4, "LineSpacing": 3},
    }
    assert "routingResult" not in params


def test_fanout_candidate_rejects_model_invented_nets():
    adapter = _make_adapter()
    session_id = "sess-validate-fanout-net-whitelist"
    adapter._session_selection_labels[session_id] = ("U22",)

    params = adapter._validate_or_build_fanout_params(
        session_id=session_id,
        candidate={
            "orderLines": [
                {"net": "NET_U1_A10", "layer": "Top", "order": 1},
                {"net": "GND", "layer": "Top", "order": 2},
                {"net": "VCC", "layer": "Art03", "order": 3},
            ],
            "constraints": {"LineWidth": 4, "LineSpacing": 3},
        },
        selected_bga="U22",
        router_type="135",
        board_summary={"netSummary": {"groundNets": ["GND"], "powerNets": ["VCC"], "clockNets": []}},
        fanout_context={"recommendedEscapeLayers": ["Top", "Art03"], "recommendedLineWidth": 4, "recommendedLineSpacing": 3},
    )

    assert params["orderLines"] == [
        {"net": "GND", "layer": "Top", "order": 1},
        {"net": "VCC", "layer": "Art03", "order": 2},
    ]


def test_fanout_candidate_falls_back_when_all_model_nets_are_invented():
    adapter = _make_adapter()
    session_id = "sess-validate-fanout-net-fallback"
    adapter._session_selection_labels[session_id] = ("U22",)

    params = adapter._validate_or_build_fanout_params(
        session_id=session_id,
        candidate={
            "orderLines": [{"net": "NET_U1_A10", "layer": "Top", "order": 1}],
            "constraints": {"LineWidth": 4, "LineSpacing": 3},
        },
        selected_bga="U22",
        router_type="135",
        board_summary={"netSummary": {"groundNets": ["GND"], "powerNets": ["VCC"], "clockNets": []}},
        fanout_context={"recommendedEscapeLayers": ["Top", "Art03"], "recommendedLineWidth": 4, "recommendedLineSpacing": 3},
    )

    assert params["orderLines"] == [
        {"net": "GND", "layer": "Top", "order": 1},
        {"net": "VCC", "layer": "Art03", "order": 2},
    ]


def test_unconfirmed_routing_result_fields_are_dropped():
    adapter = _make_adapter()
    session_id = "sess-drop-model-route"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")

    filtered = adapter._filter_unconfirmed_routing_fields(
        session_id,
        {
            "routingResult": r"F:\fake\routing_input.txt",
            "importLinesFilePath": r"F:\fake\line.out",
            "report": "模型声称布线完成",
            "fanoutParams": {"selectedBGA": "U22", "routerType": "135"},
        },
    )

    assert "routingResult" not in filtered
    assert "importLinesFilePath" not in filtered
    assert "report" not in filtered
    assert "fanoutParams" in filtered

    adapter._set_flow_state(session_id, "routing")
    routed_fields = {"routingResult": r"F:\router_work\routing_input.txt", "report": "布线完成"}
    assert adapter._filter_unconfirmed_routing_fields(session_id, routed_fields) == routed_fields


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


def test_reroute_report_visible_fallback_keeps_content_human_readable():
    content = WebSocketAdapter._fallback_visible_content_for_fields(
        "局部拆线重布已完成。",
        {
            "rerouteResult": {"type": "local_reroute", "drcPassed": True},
            "checkReport": {"passed": True, "checks": []},
            "explanation": "DRC 通过，已生成 txt。",
            "report": "局部拆线重布已完成，DRC 通过，已生成可导入 txt。",
        },
    )

    assert content == "局部拆线重布已完成，DRC 通过，已生成可导入 txt。"
    assert not content.lstrip().startswith("{")


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
    adapter._set_flow_state(session_id, "routing")

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
    assert event["fieldKeys"] == ["routingResult", "report"]
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


def test_raw_board_sketch_extension_text_is_not_visible():
    content = (
        "global sketches extension\n"
        "sketch DOC4QM_5 item EXT26\n"
        "extensions extension file=sketches/DOC4QM5-view.sch\n"
        "doc=DOC4QM5 net=NET47\n"
    )

    clean, fields = WebSocketAdapter._sanitize_pcb_visible_content(content)

    assert clean == ""
    assert fields == {}


def test_partial_raw_board_stream_prefix_is_suppressed():
    assert WebSocketAdapter._looks_like_partial_raw_board_leak("global")
    assert WebSocketAdapter._looks_like_partial_raw_board_leak("global sketches extension")
    assert WebSocketAdapter._looks_like_partial_raw_board_leak("sketch DOC4QM_5 item")
    assert not WebSocketAdapter._looks_like_partial_raw_board_leak("已完成局部拆线重布。")


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
    assert seen["raw"]["options"]["route_mode"] == "pcb"
    assert seen["raw"]["options"]["pcb_agent_loop"] is True
    assert seen["text"].startswith("[SYSTEM: 当前消息来自启云方 WebSocket PCB 客户端。")
    assert "projectid: proj-camel-001" in seen["text"]


@pytest.mark.asyncio
async def test_handle_user_message_chat_uses_chat_mode():
    adapter = _make_adapter(route_intent_llm_enabled=True)
    ws = _FakeWS()
    adapter._connections["sess-chat-no-project"] = (ws, "proj-chat-001")
    handler_called = False

    async def handler(event):
        nonlocal handler_called
        handler_called = True
        raise AssertionError("SWSD chat immediate_reply should not enter agent handler")

    adapter._swsd_workflow_controller._run_chat_agent = lambda event, plan: "chat-short-circuit"
    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {"type": "message", "body": {"role": "user", "content": "BGA ? QFP ????????????"}},
        "sess-chat-no-project",
        "proj-chat-001",
    )

    assert handler_called is False
    assert ws.sent[-1]["type"] == "message"
    assert ws.sent[-1]["body"]["content"] == "chat-short-circuit"
    assert ws.sent[-1]["body"]["isFinal"] is True

@pytest.mark.asyncio
async def test_handle_user_message_sends_immediate_reply_without_agent_handler():
    adapter = _make_adapter()
    ws = _FakeWS()
    adapter._connections["sess-immediate"] = (ws, "proj-immediate")
    adapter._session_flow_states["sess-immediate"] = "wait_router_type"
    called = False

    async def handler(event):
        nonlocal called
        called = True
        raise AssertionError("immediate_reply should not enter agent handler")

    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {"type": "message", "body": {"role": "user", "content": "确认"}},
        "sess-immediate",
        "proj-immediate",
    )

    assert called is False
    assert ws.sent[-1]["type"] == "message"
    assert "必须先选择走线算法" in ws.sent[-1]["body"]["content"]
    assert ws.sent[-1]["body"]["isFinal"] is True


@pytest.mark.asyncio
async def test_handle_user_message_uses_swsd_controller_entry_not_adapter_decide_route(monkeypatch):
    adapter = _make_adapter()
    ws = _FakeWS()
    adapter._connections["sess-swsd-entry"] = (ws, "proj-swsd-entry")
    handler_called = False

    def fail_decide_route(*args, **kwargs):
        raise AssertionError("WebSocket _handle_user_message must not call _decide_route directly")

    async def handler(event):
        nonlocal handler_called
        handler_called = True
        raise AssertionError("SWSD chat immediate_reply should not enter agent handler")

    monkeypatch.setattr(adapter, "_decide_route", fail_decide_route)
    adapter._swsd_workflow_controller._run_chat_agent = lambda event, plan: "controller-chat"
    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {"type": "message", "body": {"role": "user", "content": "BGA ? QFP ??????"}},
        "sess-swsd-entry",
        "proj-swsd-entry",
    )

    assert handler_called is False
    assert ws.sent[-1]["type"] == "message"
    assert ws.sent[-1]["body"]["content"] == "controller-chat"

@pytest.mark.asyncio
async def test_plain_greeting_skips_route_intent_llm():
    adapter = _make_adapter(route_intent_llm_enabled=True)
    handler_called = False

    async def fail_classify(**kwargs):
        raise AssertionError("plain greeting should not call route intent LLM")

    async def handler(event):
        nonlocal handler_called
        handler_called = True
        raise AssertionError("SWSD chat immediate_reply should not enter agent handler")

    adapter._classify_route_intent_with_llm = fail_classify
    adapter._swsd_workflow_controller._run_chat_agent = lambda event, plan: "hello"
    adapter.set_message_handler(handler)
    ws = _FakeWS()
    adapter._connections["sess-greeting"] = (ws, "proj-greeting")

    await adapter._handle_user_message(
        {"type": "message", "body": {"role": "user", "content": "hello"}},
        "sess-greeting",
        "proj-greeting",
    )

    assert handler_called is False
    assert ws.sent[-1]["body"]["content"] == "hello"
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
    assert sent["projectID"] == "proj-tool-1"
    assert sent["type"] == "tool-calls"
    assert sent["body"]["content"]["name"] == "getProjectData"
    assert sent["body"]["content"]["arguments"] == {}

    adapter._resolve_tool_result(json.loads(_tool_result("call_tool_1", "(pcb_data)")))
    result = await task
    assert result == "(pcb_data)"


@pytest.mark.asyncio
async def test_send_delete_traces_for_rerouting_omits_empty_arguments():
    adapter = _make_adapter()
    ws = _FakeWS()
    adapter._connections["sess-reroute-tool"] = (ws, "proj-reroute-tool")

    task = asyncio.create_task(
        adapter.send_tool_call(
            session_id="sess-reroute-tool",
            call_id="call_delete_reroute",
            tool_name="deleteTracesForRerouting",
            arguments={},
            timeout=1.0,
        )
    )
    await asyncio.sleep(0)
    sent = ws.sent[-1]
    assert sent["sessionId"] == "sess-reroute-tool"
    assert sent["projectid"] == "proj-reroute-tool"
    assert sent["projectID"] == "proj-reroute-tool"
    assert sent["type"] == "tool-calls"
    assert sent["body"]["content"] == {
        "id": "call_delete_reroute",
        "name": "deleteTracesForRerouting",
    }

    adapter._resolve_tool_result(json.loads(_tool_result("call_delete_reroute", "{}")))
    result = await task
    assert result == "{}"


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
            {
                "routingResult": r"F:\router_work\routing_input.txt",
                "importLinesFilePath": r"F:\router_work\ARC_output.txt",
            },
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
        "filePath": r"F:\router_work\ARC_output.txt",
        "successPins": ["U27.B13"],
        "failedPins": ["U27.B27"],
    }

    adapter._resolve_tool_result(
        json.loads(_tool_result(sent["body"]["content"]["id"], {"success": True, "message": "导入完成"}))
    )
    status = await task
    assert "导入完成" in status


@pytest.mark.asyncio
async def test_import_fanout_result_dedupes_same_file(tmp_path):
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-import-lines-dedupe"
    adapter._connections[session_id] = (ws, "proj-import-lines-dedupe")
    import_file = tmp_path / "line.out"
    import_file.write_text("line records\n", encoding="utf-8")

    fields = {
        "routingResult": str(tmp_path / "routing_input.txt"),
        "importLinesFilePath": str(import_file),
    }
    route_params = {"successPins": ["U27.B13"], "failedPins": []}

    first_task = asyncio.create_task(adapter._import_fanout_result(session_id, route_params, fields))
    for _ in range(5):
        await asyncio.sleep(0)
        if any(item.get("type") == "tool-calls" for item in ws.sent):
            break

    first_call = next(item for item in ws.sent if item["type"] == "tool-calls")
    adapter._resolve_tool_result(
        json.loads(_tool_result(first_call["body"]["content"]["id"], "Imported 1 / 1 path units from line.out"))
    )
    first_status = await first_task

    second_status = await adapter._import_fanout_result(session_id, route_params, fields)

    import_calls = [item for item in ws.sent if item.get("type") == "tool-calls"]
    assert len(import_calls) == 1
    assert second_status == first_status


@pytest.mark.asyncio
async def test_import_reroute_prefers_incremental_import_file(tmp_path):
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-reroute-import-incremental"
    adapter._connections[session_id] = (ws, "proj-reroute-import-incremental")
    layout_file = tmp_path / "routed_layout.txt"
    layout_file.write_text("(layout routed)\n", encoding="utf-8")
    import_file = tmp_path / "reroute_import.txt"
    import_file.write_text(_valid_reroute_import_text(), encoding="utf-8")

    fields = {
        "rerouteResult": {
            "type": "local_reroute",
            "drcPassed": True,
            "routedLayoutTxtFilePath": str(layout_file),
            "importLinesFilePath": str(import_file),
        },
        "routedLayoutTxtFilePath": str(layout_file),
        "importLinesFilePath": str(import_file),
        "checkReport": {"passed": True},
    }

    task = asyncio.create_task(adapter._import_reroute_result(session_id, fields))
    for _ in range(5):
        await asyncio.sleep(0)
        if any(item.get("type") == "tool-calls" for item in ws.sent):
            break

    sent = next(item for item in ws.sent if item["type"] == "tool-calls")
    assert sent["body"]["content"]["name"] == "importLines"
    assert sent["body"]["content"]["arguments"]["filePath"] == str(import_file)

    adapter._resolve_tool_result(
        json.loads(_tool_result(sent["body"]["content"]["id"], {"success": True, "message": "导入完成"}))
    )
    status = await task
    assert "导入完成" in status


@pytest.mark.asyncio
async def test_import_reroute_skips_full_layout_file(tmp_path):
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-reroute-import-layout-skip"
    adapter._connections[session_id] = (ws, "proj-reroute-import-layout-skip")
    layout_file = tmp_path / "routed_layout.txt"
    layout_file.write_text("(layout\n  (wires)\n)\n", encoding="utf-8")
    fields = {
        "rerouteResult": {"type": "local_reroute", "drcPassed": True},
        "routedLayoutTxtFilePath": str(layout_file),
        "checkReport": {"passed": True},
    }

    status = await adapter._import_reroute_result(session_id, fields)

    assert "不适合 importLines" in status
    assert not any(item.get("type") == "tool-calls" for item in ws.sent)


@pytest.mark.asyncio
async def test_cached_fanout_route_suppresses_report_when_import_is_rejected(monkeypatch):
    adapter = _make_adapter()
    ws = _FakeWS()
    session_id = "sess-import-rejected"
    adapter._connections[session_id] = (ws, "proj-import-rejected")
    adapter._session_fanout_params[session_id] = {
        "selectedBGA": "U22",
        "routerType": "135",
        "orderLines": [{"net": "net13", "layer": "Top", "order": 1}],
    }
    route_result = {
        "importLinesFilePath": r"F:\router_work\line.out",
        "report": "布线连通率: 100%",
        "routingResult": r"F:\router_work\line.out",
    }

    from tools import pcb_tools

    monkeypatch.setattr(
        pcb_tools,
        "route_bga",
        lambda userData, session_id=None: json.dumps(route_result, ensure_ascii=False),
    )

    task = asyncio.create_task(adapter._run_cached_fanout_route(session_id))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if any(item.get("type") == "tool-calls" for item in ws.sent):
            break

    sent = next(item for item in ws.sent if item["type"] == "tool-calls")
    assert sent["body"]["content"]["name"] == "importLines"

    adapter._resolve_tool_result(
        json.loads(_tool_result(sent["body"]["content"]["id"], {"cancelled": True, "message": "用户取消导入"}))
    )
    assert await task is True

    final = ws.sent[-1]
    assert final["type"] == "message"
    assert final["body"]["content"] == "已取消导入布线。"
    assert "布线连通率" not in final["body"]["content"]
    assert "##PCB_FIELDS##" not in final["body"]["content"]


@pytest.mark.asyncio
async def test_agent_loop_fanout_fields_suppress_report_when_import_is_rejected():
    adapter = _make_adapter()
    session_id = "sess-agent-loop-import-rejected"

    async def fake_import(*args, **kwargs):
        return "__pcb_import_lines_rejected__"

    adapter._import_fanout_result = fake_import
    fields = await adapter._prepare_final_pcb_fields_for_frontend(
        session_id,
        {
            "routingResult": r"F:\router_work\line.out",
            "importLinesFilePath": r"F:\router_work\line.out",
            "report": "布线连通率: 100%",
        },
    )

    assert fields == {"_importRejected": True}


def test_rule_validation_rejects_llm_chat_for_strong_pcb_request():
    adapter = _make_adapter()

    decision = adapter._decide_route(
        "sess-llm-guard-1",
        "帮我对U27做BGA逃逸布线",
        llm_intent="chat",
    )

    assert decision.mode == "pcb"
    assert decision.reason == "forced_global_fanout"
    assert adapter._session_requested_bga_targets["sess-llm-guard-1"] == "U27"
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
    assert decision.reason in {"pcb_entry", "forced_global_fanout"}
    assert decision.bootstrap_get_project is True


@pytest.mark.parametrize(
    ("text", "expected_mode"),
    [
        ("不要解释，直接开始PCB BGA逃逸布线", "pcb"),
        ("开始 PCB 布线", "pcb"),
        ("这个板子跑一下 BGA 扇出", "pcb"),
        ("对 U27 做 BGA fanout", "pcb"),
        ("获取当前版图并找出可布线 BGA", "pcb"),
        ("我想做BGA逃逸布线，告诉我什么是BGA逃逸布线", "chat"),
        ("告诉我什么是BGA逃逸布线", "chat"),
        ("什么是逃逸布线", "chat"),
        ("介绍一下 BGA 逃逸布线原理", "chat"),
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


def test_parse_route_intent_output_recovers_final_json_after_reasoning():
    adapter = _make_adapter()
    raw = (
        "Thinking Process:\n"
        "1. analyze the request\n"
        "2. compare candidate intents\n\n"
        '{"intent":"pcb_entry","route_mode":"pcb","confidence":0.93,'
        '"should_call_get_project_data":true}'
    )

    intent = adapter._parse_route_intent_output(raw)

    assert intent is not None
    assert intent.intent == "pcb_entry"
    assert intent.route_mode == "pcb"


def test_parse_route_intent_output_recovers_final_kv_after_reasoning():
    adapter = _make_adapter()
    raw = (
        "Thinking Process:\n"
        "1. analyze the request\n"
        "2. compare candidate intents\n\n"
        "intent=cancel\n"
        "route_mode=chat\n"
        "confidence=0.99\n"
    )

    intent = adapter._parse_route_intent_output(raw)

    assert intent is not None
    assert intent.intent == "cancel"
    assert intent.route_mode == "chat"


def test_route_intent_prompt_includes_intention_memory(tmp_path, monkeypatch):
    hermes_home = tmp_path / ".hermes"
    memory_dir = hermes_home / "memories"
    memory_dir.mkdir(parents=True)
    (memory_dir / "MEMORY.md").write_text(
        "PCB 意图识别经验：#全局fanout 必须进入全局 fanout skill。",
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    adapter = _make_adapter()

    messages = adapter._build_route_intent_prompt(
        session_id="sess-memory-intent",
        user_text="#全局fanout",
        project_id="proj-memory-intent",
    )

    assert "意图识别经验 memory" in messages[0]["content"]
    assert "#全局fanout 必须进入全局 fanout skill" in messages[0]["content"]


def test_rule_validation_rejects_followup_without_pcb_context():
    adapter = _make_adapter()

    decision = adapter._decide_route(
        "sess-llm-guard-2",
        "继续",
        llm_intent="pcb_followup",
    )

    assert decision.mode == "chat"
    assert decision.reason == "default_chat"


def test_route_decision_supports_reroute_reentry_from_report_state():
    adapter = _make_adapter()
    session_id = "sess-reroute-reentry"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "reroute")
    adapter._swsd_update(
        session_id,
        "pcb_reroute_flow",
        "report",
        {"finalFields": {"rerouteResult": {"drcPassed": True}}},
        event_type="checkpoint",
        intent="reroute_result",
        checkpoint_label="reroute result",
    )

    decision = adapter._decide_route(session_id, "再 reroute 一次")

    assert decision.mode == "pcb"
    assert decision.reason == "reroute_reentry"
    assert decision.intent == "pcb_reroute_selected"


def test_route_decision_supports_reroute_checkpoint_rollback():
    adapter = _make_adapter()
    session_id = "sess-reroute-rollback"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "reroute")
    adapter._swsd_update(
        session_id,
        "pcb_reroute_flow",
        "rip_up",
        {"routerType": "arc"},
        event_type="checkpoint",
        intent="ripup_complete",
        checkpoint_label="ripup",
    )
    adapter._swsd_update(
        session_id,
        "pcb_reroute_flow",
        "report",
        {"routerType": "arc", "finalFields": {"rerouteResult": {"drcPassed": True}}},
        event_type="checkpoint",
        intent="reroute_result",
        checkpoint_label="report",
    )

    decision = adapter._decide_route(session_id, "回到上一步")

    assert decision.mode == "pcb"
    assert decision.reason == "reroute_rollback"
    assert "恢复" in (decision.immediate_reply or "")


def test_route_decision_supports_escape_target_change_from_review_state():
    adapter = _make_adapter()
    session_id = "sess-fanout-change-target"
    adapter._set_session_mode(session_id, "pcb")
    adapter._session_bga_selection[session_id] = ({"label": "U23"}, {"label": "U55"})
    adapter._session_selected_targets[session_id] = "U23"
    adapter._session_router_types[session_id] = "135"
    adapter._session_fanout_params[session_id] = {"routerType": "135+RL"}
    adapter._swsd_update(
        session_id,
        "pcb_escape_flow",
        "review",
        {
            "selection": [{"label": "U23"}, {"label": "U55"}],
            "selectedBGA": "U23",
            "routerType": "135",
            "fanoutParams": {"routerType": "135+RL"},
        },
        event_type="checkpoint",
        intent="route_complete",
        checkpoint_label="review",
    )

    decision = adapter._decide_route(session_id, "换成 U55")

    assert decision.mode == "pcb"
    assert decision.reason == "escape_change_target"
    assert decision.intent == "pcb_select_target"
    assert adapter._session_selected_targets[session_id] == "U55"
    assert adapter._session_flow_states[session_id] == "wait_router_type"


def test_route_decision_supports_escape_param_modify_from_review_state():
    adapter = _make_adapter()
    session_id = "sess-fanout-modify-params"
    adapter._set_session_mode(session_id, "pcb")
    adapter._session_selected_targets[session_id] = "U23"
    adapter._session_fanout_params[session_id] = {
        "routerType": "135+RL",
        "constraints": {"LineWidth": 4, "LineSpacing": 3},
    }
    adapter._swsd_update(
        session_id,
        "pcb_escape_flow",
        "review",
        {
            "selectedBGA": "U23",
            "routerType": "135",
            "fanoutParams": {"routerType": "135+RL"},
        },
        event_type="checkpoint",
        intent="route_complete",
        checkpoint_label="review",
    )

    decision = adapter._decide_route(session_id, "改成 arc")

    assert decision.mode == "pcb"
    assert decision.reason == "escape_modify_params"
    assert adapter._session_flow_states[session_id] == "wait_confirm"
    assert adapter._session_route_algorithms[session_id] == "arc"
    assert adapter._session_fanout_params[session_id]["routerType"] == "rl_arc"


def test_route_decision_supports_escape_constraint_modify_from_review_state():
    adapter = _make_adapter()
    session_id = "sess-fanout-modify-constraints"
    adapter._set_session_mode(session_id, "pcb")
    adapter._session_selected_targets[session_id] = "U23"
    adapter._session_fanout_params[session_id] = {
        "selectedBGA": "U23",
        "routerType": "135+RL",
        "constraints": {"LineWidth": 4, "LineSpacing": 3},
    }
    adapter._swsd_update(
        session_id,
        "pcb_escape_flow",
        "review",
        {
            "selectedBGA": "U23",
            "routerType": "135",
            "fanoutParams": {"routerType": "135+RL"},
        },
        event_type="checkpoint",
        intent="route_complete",
        checkpoint_label="review",
    )

    decision = adapter._decide_route(session_id, "线宽改成 5")

    assert decision.mode == "pcb"
    assert decision.reason == "escape_modify_params"
    assert adapter._session_flow_states[session_id] == "wait_confirm"
    assert adapter._session_fanout_params[session_id]["constraints"]["LineWidth"] == 5


def test_route_decision_supports_pinyin_confirm_and_reject():
    adapter = _make_adapter()
    confirm_session = "sess-pinyin-confirm"
    adapter._set_session_mode(confirm_session, "pcb")
    adapter._set_flow_state(confirm_session, "wait_confirm")
    adapter._session_fanout_params[confirm_session] = {"selectedBGA": "U23", "routerType": "rl"}

    confirm_decision = adapter._decide_route(confirm_session, "queren")

    assert confirm_decision.reason == "confirm_route"

    reject_session = "sess-pinyin-reject"
    adapter._set_session_mode(reject_session, "pcb")
    adapter._session_fanout_params[reject_session] = {"selectedBGA": "U23", "routerType": "rl"}
    adapter._swsd_update(
        reject_session,
        "pcb_escape_flow",
        "review",
        {"selectedBGA": "U23", "fanoutParams": {"routerType": "rl"}},
        event_type="checkpoint",
        intent="route_complete",
        checkpoint_label="review",
    )

    reject_decision = adapter._decide_route(reject_session, "jujue")

    assert reject_decision.reason == "reject_route"

def test_route_decision_supports_escape_checkpoint_rollback():
    adapter = _make_adapter()
    session_id = "sess-fanout-rollback"
    adapter._set_session_mode(session_id, "pcb")
    adapter._swsd_update(
        session_id,
        "pcb_escape_flow",
        "layer_assign",
        {
            "selection": [{"label": "U23"}],
            "selectedBGA": "U23",
            "routerType": "135",
        },
        event_type="checkpoint",
        intent="router_type_step",
        checkpoint_label="layer assign",
    )
    adapter._swsd_update(
        session_id,
        "pcb_escape_flow",
        "review",
        {
            "selection": [{"label": "U23"}],
            "selectedBGA": "U23",
            "routerType": "135",
            "fanoutParams": {"routerType": "135+RL"},
        },
        event_type="checkpoint",
        intent="route_complete",
        checkpoint_label="review",
    )

    decision = adapter._decide_route(session_id, "回到上一步")

    assert decision.mode == "pcb"
    assert decision.reason == "escape_rollback"
    assert adapter._session_flow_states[session_id] == "wait_router_type"
    assert adapter._session_selected_targets[session_id] == "U23"


@pytest.mark.asyncio
async def test_handle_user_message_skips_adapter_intent_and_loads_pcb_skills(monkeypatch):
    adapter = _make_adapter(route_intent_llm_enabled=True)
    seen = {}

    async def fake_classify(*, session_id, user_text, project_id):
        raise AssertionError("WebSocket adapter should not classify PCB business intent")

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

    _assert_contains_skills(seen["auto_skill"], ["hardware/pcb-intelligence"])
    assert seen["text"].startswith("[SYSTEM: 当前消息来自启云方 WebSocket PCB 客户端。")
    assert "forced_skill: global_fanout" in seen["text"]
    assert "projectid: proj-llm-1" in seen["text"]
    assert "帮我对U27做BGA逃逸布线" in seen["text"]


@pytest.mark.parametrize("content", ["#逃逸布线", "#全局fanout", "逃逸布线", "对U5逃逸布线"])
@pytest.mark.asyncio
async def test_forced_fanout_tag_enters_agent_loop_with_global_fanout_guard(monkeypatch, content):
    adapter = _make_adapter(route_intent_llm_enabled=True)
    seen = {}
    sent = []

    async def handler(event):
        seen["auto_skill"] = event.auto_skill
        seen["text"] = event.text
        seen["options"] = event.raw_message["options"]
        return None

    async def fake_send(*, chat_id, content, **kwargs):
        sent.append((chat_id, content))

    adapter.set_message_handler(handler)
    monkeypatch.setattr(adapter, "send", fake_send)

    await adapter._handle_user_message(
        {
            "type": "message",
            "body": {"role": "user", "content": content},
        },
        "sess-forced-fanout",
        "proj-forced-fanout",
    )

    _assert_contains_skills(seen["auto_skill"], ["hardware/pcb-intelligence"])
    assert seen["options"]["route_mode"] == "pcb"
    assert seen["options"]["pcb_agent_loop"] is True
    assert "forced_skill: global_fanout" in seen["text"]
    assert "禁止调用 deleteTracesForRerouting、getSelectedElements、drop_net 或 reroute" in seen["text"]
    assert content in seen["text"]
    assert "projectid: proj-forced-fanout" in seen["text"]
    assert adapter._session_modes["sess-forced-fanout"] == "pcb"
    assert sent == [("sess-forced-fanout", "已进入全局 BGA fanout/逃逸布线流程，正在获取版图信息。")]


@pytest.mark.asyncio
async def test_direct_bga_analysis_uses_requested_target(monkeypatch):
    adapter = _make_adapter()
    session_id = "sess-requested-bga"
    sent = []

    async def fake_send(*, chat_id, content, **kwargs):
        sent.append((chat_id, content))

    monkeypatch.setattr(adapter, "send", fake_send)
    from tools import pcb_chunking_tool

    monkeypatch.setattr(
        pcb_chunking_tool,
        "_extract_bga",
        lambda *args, **kwargs: json.dumps(
            {
                "selection": [
                    {"label": "U5", "detail": "BGA candidate"},
                    {"label": "U7", "detail": "BGA candidate"},
                ],
                "boardSummary": {"components": [{"refdes": "U5"}, {"refdes": "U7"}]},
                "fanoutContext": {},
            },
            ensure_ascii=False,
        ),
    )
    adapter._session_requested_bga_targets[session_id] = "U5"

    handled = await adapter._run_direct_bga_analysis(session_id, {"projectData": "cached"})

    assert handled is True
    assert adapter._session_selected_targets[session_id] == "U5"
    assert adapter._session_flow_states[session_id] == "wait_router_type"
    assert sent
    assert "已选择目标 BGA：U5" in sent[-1][1]
    assert "请选择走线算法类型" in sent[-1][1]


@pytest.mark.asyncio
async def test_direct_bga_analysis_reports_missing_requested_target(monkeypatch):
    adapter = _make_adapter()
    session_id = "sess-missing-requested-bga"
    sent = []

    async def fake_send(*, chat_id, content, **kwargs):
        sent.append((chat_id, content))

    monkeypatch.setattr(adapter, "send", fake_send)
    from tools import pcb_chunking_tool

    monkeypatch.setattr(
        pcb_chunking_tool,
        "_extract_bga",
        lambda *args, **kwargs: json.dumps(
            {
                "selection": [{"label": "U7", "detail": "BGA candidate"}],
                "boardSummary": {},
                "fanoutContext": {},
            },
            ensure_ascii=False,
        ),
    )
    adapter._session_requested_bga_targets[session_id] = "U5"

    handled = await adapter._run_direct_bga_analysis(session_id, {"projectData": "cached"})

    assert handled is True
    assert session_id not in adapter._session_selected_targets
    assert adapter._session_flow_states[session_id] == "wait_selection"
    assert "未在 BGA 候选中找到 U5" in sent[-1][1]
    assert "候选 BGA：U7" in sent[-1][1]


@pytest.mark.asyncio
async def test_forced_fanout_empty_agent_response_sends_status_fallback(monkeypatch):
    adapter = _make_adapter(route_intent_llm_enabled=True)
    sent = []

    async def handler(event):
        return ""

    async def fake_send(*, chat_id, content, **kwargs):
        sent.append((chat_id, content))

    adapter.set_message_handler(handler)
    monkeypatch.setattr(adapter, "send", fake_send)

    await adapter._handle_user_message(
        {
            "type": "message",
            "body": {"role": "user", "content": "#逃逸布线"},
        },
        "sess-forced-fanout-empty",
        "proj-forced-fanout-empty",
    )

    assert sent == [
        (
            "sess-forced-fanout-empty",
            "已进入全局 BGA fanout/逃逸布线流程，正在获取版图信息。",
        )
    ]


@pytest.mark.parametrize("content", ["#reroute", "#拆线重布"])
@pytest.mark.asyncio
async def test_forced_reroute_tag_uses_swsd_direct_tool_call(monkeypatch, content):
    adapter = _make_adapter(route_intent_llm_enabled=True)
    seen = {"handler_called": False, "tool_calls": []}

    async def handler(event):
        seen["handler_called"] = True
        return None

    async def fake_send_tool_call(*, session_id, call_id, tool_name, arguments, timeout=360.0):
        seen["tool_calls"].append({
            "session_id": session_id,
            "call_id": call_id,
            "tool_name": tool_name,
            "arguments": arguments,
        })
        return {"missing_routes": [], "projectData": "", "localContext": {"source": "unit_test"}}

    adapter.set_message_handler(handler)
    monkeypatch.setattr(adapter, "send_tool_call", fake_send_tool_call)

    await adapter._handle_user_message(
        {
            "type": "message",
            "body": {"role": "user", "content": content},
        },
        "sess-forced-reroute",
        "proj-forced-reroute",
    )

    assert seen["handler_called"] is False
    assert len(seen["tool_calls"]) == 1
    assert seen["tool_calls"][0]["tool_name"] == "deleteTracesForRerouting"
    assert seen["tool_calls"][0]["arguments"] == {}
    assert adapter._session_modes["sess-forced-reroute"] == "pcb"
    assert adapter._session_flow_states["sess-forced-reroute"] == "reroute"


def test_get_project_data_relative_file_path_is_resolved_and_cached(monkeypatch, tmp_path):
    monkeypatch.setenv("BOARD_DATA_USE_FILE_PATH", "1")
    monkeypatch.chdir(tmp_path)
    adapter = _make_adapter()
    session_id = "sess-relative-board-path"
    call_id = "call_relative_board"
    board_text = '(component "U22"\n  (part "CG400")\n  (propname "DFA_DEV_CLASS" propvalue "BGA")\n)\n'
    board_file = tmp_path / "board.txt"
    board_file.write_text(board_text, encoding="utf-8")

    from tools.pcb_tools import WebSocketTransportSingleton

    transport = WebSocketTransportSingleton.get_instance()
    transport.clear_session(session_id)
    adapter._pending_tool_names[call_id] = "getProjectData"
    adapter._pending_tool_sessions[call_id] = session_id

    result = adapter._maybe_read_file_result(call_id, "board.txt")

    assert result == board_text
    assert transport.get_cached_project_data_path(session_id) == str(board_file.resolve())
    transport.clear_session(session_id)


def test_pcb_extract_bga_uses_cached_project_data_file_path(tmp_path):
    session_id = "sess-extract-bga-path"
    board_file = tmp_path / "board.txt"
    board_file.write_text(
        '(component "U22"\n'
        '  (part "CG400")\n'
        '  (footprint "BGA400")\n'
        '  (propname "DFA_DEV_CLASS" propvalue "BGA")\n'
        ')\n',
        encoding="utf-8",
    )

    from tools import pcb_chunking_tool
    from tools.pcb_tools import WebSocketTransportSingleton

    transport = WebSocketTransportSingleton.get_instance()
    transport.clear_session(session_id)
    transport.cache_project_data_path(str(board_file), session_id=session_id)

    result = json.loads(
        pcb_chunking_tool._extract_bga("__CACHED_PROJECT_DATA__", session_id=session_id)
    )

    assert result["selection"][0]["label"] == "U22"
    assert result["source"] == "rule_script"
    transport.clear_session(session_id)


@pytest.mark.asyncio
async def test_pcb_entry_file_path_mode_bootstraps_from_file_path(monkeypatch, tmp_path):
    monkeypatch.setenv("BOARD_DATA_USE_FILE_PATH", "1")
    port = _free_port()
    adapter = _make_adapter(port)
    board_file = tmp_path / "board.txt"
    board_file.write_text(
        '(component "FPGA1"\n'
        '(part "BGA-1156")\n'
        '(propname "DFA_DEV_CLASS" propvalue "BGA")\n'
        ')\n',
        encoding="utf-8",
    )

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
                msg = await _recv_json(ws)
                assert msg["type"] == "tool-calls"
                assert msg["body"]["content"]["name"] == "getProjectData"
                await ws.send_str(_tool_result(msg["body"]["content"]["id"], str(board_file)))

                msg = await _recv_json(ws)
                assert msg["type"] == "message"
                assert msg["body"]["selection"] == [{"label": "FPGA1", "detail": "BGA-1156"}]
                assert "FPGA1" in msg["body"]["content"]
    finally:
        await adapter.disconnect()


def test_bga_question_with_polite_phrase_stays_chat():
    adapter = _make_adapter()

    decision = adapter._decide_route(
        "sess-chat-question-1",
        "BGA 和 QFP 有什么区别？请简短回答。",
        llm_intent="chat",
    )

    assert decision.mode == "chat"
    assert decision.reason in {"chat_only", "default_chat"}


def test_pcb_visible_content_strips_raw_board_leak():
    raw = (
        "**BGA 选择列表**：\n"
        "- 请在列表中选择一个 BGA。?: 请问箭头指向的是什么？"
        'T", ceramic_add_min_total_cavity_width: 0.1195, '
        "ceramic_add_total_cavity_width_min_poly: 0.0024, "
        'generation_options: "donut_rounded", '
        "gerber_output_quality_strength: 100.0, "
        "pad_to_pad_clearance: 0.051811"
    )

    clean, fields = WebSocketAdapter._sanitize_pcb_visible_content(raw)

    assert clean == ""
    assert fields == {}


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

def test_router_type_prompt_includes_ga_and_auto_options():
    adapter = _make_adapter()
    session_id = "sess-router-options"
    adapter._session_selected_targets[session_id] = "U22"

    prompt = adapter._router_type_prompt(session_id)
    followup = adapter._router_choice_followup_prompt(session_id)

    assert "GA" in prompt
    assert "Auto" in prompt
    assert "135 + GA" in prompt
    assert "arc + Auto" in prompt
    assert "GA" in followup or "Auto" in followup


def test_router_type_prompt_no_longer_mentions_digit_menu():
    adapter = _make_adapter()
    session_id = "sess-router-no-digit"
    adapter._session_selected_targets[session_id] = "U22"
    adapter._session_route_algorithms[session_id] = "135"

    prompt = adapter._router_type_prompt(session_id)
    followup = adapter._router_choice_followup_prompt(session_id)

    assert "1=" not in prompt
    assert "2=" not in prompt
    assert "1=" not in followup
    assert "2=" not in followup


def test_wait_router_type_pure_digit_falls_back_to_chat():
    adapter = _make_adapter()
    session_id = "sess-router-digit-chat"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_router_type")
    adapter._session_selected_targets[session_id] = "U22"

    decision = adapter._decide_route(session_id, "1")

    assert decision.mode == "chat"
    assert decision.reason == "default_chat"
    assert adapter._session_flow_states[session_id] == "wait_router_type"


def test_route_decision_supports_fanout_rerun_in_review_state():
    adapter = _make_adapter()
    session_id = "sess-fanout-rerun"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")
    adapter._session_selected_targets[session_id] = "U23"
    adapter._session_fanout_params[session_id] = {
        "selectedBGA": "U23",
        "routerType": "rl",
        "routeAlgorithm": "135",
        "fanoutModule": "RL",
        "orderLines": [{"net": "GND", "layer": "Top", "order": 1}],
        "constraints": {"LineWidth": 4, "LineSpacing": 3},
    }
    adapter._swsd_update(
        session_id,
        "pcb_escape_flow",
        "review",
        {"selectedBGA": "U23", "fanoutParams": {"routerType": "rl"}},
        event_type="checkpoint",
        intent="route_complete",
        checkpoint_label="review",
    )

    decision = adapter._decide_route(session_id, "rerun fanout")

    assert decision.mode == "pcb"
    assert decision.reason == "rerun_fanout"
    assert adapter._session_flow_states[session_id] == "wait_router_type"
    assert adapter._session_fanout_params[session_id]["selectedBGA"] == "U23"
    assert "orderLines" not in adapter._session_fanout_params[session_id]


def test_targeted_single_utterance_fanout_enters_pcb_entry():
    adapter = _make_adapter()

    decision = adapter._decide_route("sess-single-utterance", "for U5 fanout, line width 30")

    assert decision.mode == "pcb"
    assert decision.reason == "forced_global_fanout"
    assert adapter._session_requested_bga_targets["sess-single-utterance"] == "U5"

def test_active_workflow_state_keeps_review_active_and_legacy_not_idle():
    adapter = _make_adapter()
    session_id = "sess-active-review"
    adapter._set_session_mode(session_id, "pcb")
    adapter._swsd_update(
        session_id,
        "pcb_escape_flow",
        "review",
        {"selectedBGA": "U22", "fanoutParams": {"routerType": "rl"}},
        event_type="checkpoint",
        intent="route_complete",
        checkpoint_label="review",
    )

    workflow_id, workflow_state = adapter._active_workflow_state(session_id)

    assert workflow_id == "pcb_escape_flow"
    assert workflow_state == "review"
    assert adapter._swsd_runtime_bridge.legacy_flow_for_workflow_state(workflow_id, workflow_state) == "wait_confirm"


def test_route_decision_supports_escape_restore_params_version():
    adapter = _make_adapter()
    session_id = "sess-restore-params"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")

    from tools import pcb_tools

    pcb_tools._transport.bind_project(session_id, "proj-restore-params")
    pcb_tools._transport.cache_project_data('(board "demo")', session_id=session_id)
    pcb_tools._transport.record_fanout_route_version(
        session_id,
        fanout_params={
            "selectedBGA": "U22",
            "routerType": "rl",
            "orderLines": [{"net": "GND", "layer": "Top", "order": 1}],
        },
        user_text="first run",
        report="ok",
    )

    decision = adapter._decide_route(session_id, "恢复第 1 版参数")

    assert decision.mode == "pcb"
    assert decision.reason == "restore_params_version"
    assert "restoredKind" in (decision.immediate_reply or "")
    assert adapter._session_active_params_versions[session_id] == 1
    assert adapter._session_flow_states[session_id] == "wait_confirm"


def test_route_decision_supports_escape_restore_layout_checkpoint():
    adapter = _make_adapter()
    session_id = "sess-restore-layout"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "wait_confirm")

    from tools import pcb_tools

    pcb_tools._transport.bind_project(session_id, "proj-restore-layout")
    pcb_tools._transport.cache_project_data('(board "demo")', session_id=session_id)
    layout_file = Path(tempfile.gettempdir()) / "hermes_test_restore_layout_v001.txt"
    layout_file.write_text('(layout "routed-v1")', encoding="utf-8")
    pcb_tools._transport.record_fanout_route_version(
        session_id,
        fanout_params={
            "selectedBGA": "U22",
            "routerType": "rl",
            "orderLines": [{"net": "GND", "layer": "Top", "order": 1}],
        },
        user_text="first run",
        routed_layout_path=str(layout_file),
        report="ok",
    )
    pcb_tools._transport.mark_fanout_import_status(session_id, 1, "success", "imported")

    decision = adapter._decide_route(session_id, "恢复第 1 版版图")

    assert decision.mode == "pcb"
    assert decision.reason == "restore_layout_checkpoint"
    assert "restoredKind" in (decision.immediate_reply or "")
    assert "requiresReimport" in (decision.immediate_reply or "")
    assert adapter._session_layout_versions[session_id] == 1
    assert adapter._session_flow_states[session_id] == "wait_confirm"



def test_swsd_update_write_failure_is_observable(caplog):
    adapter = _make_adapter()

    def fail_update(*args, **kwargs):
        raise RuntimeError("state db down")

    adapter._swsd_state.update = fail_update

    with caplog.at_level("WARNING"):
        ok = adapter._swsd_update(
            "sess-health",
            "pcb_escape_flow",
            "review",
            {"x": 1},
            event_type="state_sync",
            intent="",
            action_type="state_sync",
        )

    assert ok is False
    assert "SWSD update failed" in caplog.text
    assert adapter._swsd_health["sess-health"]["lastWriteError"] == "state db down"


def test_swsd_transition_guard_warn_allows_and_logs(caplog):
    adapter = _make_adapter(swsd_transition_guard_mode="warn")
    adapter._swsd_update("sess-guard-warn", "pcb_escape_flow", "review", {}, event_type="state_sync")

    with caplog.at_level("WARNING"):
        ok = adapter._swsd_update(
            "sess-guard-warn",
            "pcb_escape_flow",
            "select_bga",
            {},
            event_type="workflow_action",
            intent="confirm_route",
            action_type="normal",
        )

    assert ok is True
    assert "Illegal SWSD transition" in caplog.text
    assert adapter._swsd_state.load("sess-guard-warn", "pcb_escape_flow")["current_state"] == "select_bga"


def test_swsd_transition_guard_strict_rejects_invalid_transition(caplog):
    adapter = _make_adapter(swsd_transition_guard_mode="strict")
    adapter._swsd_update("sess-guard-strict", "pcb_escape_flow", "review", {}, event_type="state_sync")

    with caplog.at_level("WARNING"):
        ok = adapter._swsd_update(
            "sess-guard-strict",
            "pcb_escape_flow",
            "select_bga",
            {},
            event_type="workflow_action",
            intent="confirm_route",
            action_type="normal",
        )

    assert ok is False
    assert "Illegal SWSD transition" in caplog.text
    assert adapter._swsd_state.load("sess-guard-strict", "pcb_escape_flow")["current_state"] == "review"
    assert "illegal transition review->select_bga" in adapter._swsd_health["sess-guard-strict"]["lastWriteError"]


def test_swsd_transition_guard_bypasses_state_sync_reset_observation():
    adapter = _make_adapter(swsd_transition_guard_mode="strict")
    adapter._swsd_update("sess-bypass", "pcb_escape_flow", "review", {}, event_type="state_sync")

    assert adapter._swsd_update(
        "sess-bypass",
        "pcb_escape_flow",
        "select_bga",
        {},
        event_type="observation",
        intent="not_a_graph_transition",
        action_type="observation",
    ) is True
    assert adapter._swsd_update(
        "sess-bypass",
        "pcb_escape_flow",
        "idle",
        {},
        event_type="reset",
        intent="not_a_graph_transition",
        action_type="reset",
    ) is True


def test_stale_fanout_body_does_not_pull_reroute_report_back_to_escape_review():
    adapter = _make_adapter()
    session_id = "sess-stale-body"
    adapter._set_session_mode(session_id, "pcb")
    adapter._set_flow_state(session_id, "reroute")
    adapter._swsd_update(session_id, "pcb_reroute_flow", "report", {}, event_type="state_sync")

    adapter._recover_experience_from_inbound_body(
        session_id,
        "project",
        {"fanoutParams": {"selectedBGA": "U5", "routerType": "rl"}},
    )

    assert adapter._session_flow_states[session_id] == "reroute"
    assert adapter._swsd_state.load(session_id, "pcb_reroute_flow")["current_state"] == "report"
    assert adapter._session_fanout_params[session_id]["selectedBGA"] == "U5"

@pytest.mark.asyncio
async def test_swsd_fanout_execute_chain_bootstraps_get_project_data(monkeypatch):
    adapter = _make_adapter(route_intent_llm_enabled=True)
    adapter._allow_legacy_route_decision = False
    ws = _FakeWS()
    session_id = "sess-swsd-fanout-e2e"
    project_id = "proj-swsd-fanout-e2e"
    adapter._connections[session_id] = (ws, project_id)
    seen = {"tool_calls": [], "analysis_context": None, "handler_called": False}

    def fail_decide_route(*args, **kwargs):
        raise AssertionError("new SWSD execute chain must not call legacy _decide_route")

    async def handler(event):
        seen["handler_called"] = True
        raise AssertionError("fanout execute bootstrap should not enter Hermes agent handler")

    async def fake_send_tool_call(*, session_id, call_id, tool_name, arguments, timeout=360.0):
        seen["tool_calls"].append(
            {
                "session_id": session_id,
                "call_id": call_id,
                "tool_name": tool_name,
                "arguments": arguments,
                "timeout": timeout,
            }
        )
        return {
            "projectData": "(board U5 U7)",
            "relativePath": "boards/demo.kicad_pcb",
            "absolutePath": "F:/boards/demo.kicad_pcb",
        }

    async def fake_run_direct_bga_analysis(session_id, bootstrap_context):
        seen["analysis_context"] = dict(bootstrap_context)
        return True

    monkeypatch.setattr(adapter, "_decide_route", fail_decide_route)
    monkeypatch.setattr(adapter, "send_tool_call", fake_send_tool_call)
    monkeypatch.setattr(adapter, "_run_direct_bga_analysis", fake_run_direct_bga_analysis)
    adapter.set_message_handler(handler)

    await adapter._handle_user_message(
        {"type": "message", "body": {"role": "user", "content": "给 U5 做 fanout，U7 也一起做，线宽3mil，线距3mil"}},
        session_id,
        project_id,
    )

    assert seen["handler_called"] is False
    assert len(seen["tool_calls"]) == 1
    assert seen["tool_calls"][0]["tool_name"] == "getProjectData"
    assert seen["tool_calls"][0]["session_id"] == session_id
    assert seen["analysis_context"]["source"] == "bootstrap_getProjectData"

    state = adapter._swsd_state.load(session_id, "pcb_escape_flow")
    assert state["current_state"] == "select_bga"
    payload = state["state_payload"]
    assert payload["step_id"] == "get_project_data"
    assert payload["projectData"]['status'] == "loaded"
    assert payload["projectData"]['relative_path'] == "boards/demo.kicad_pcb"
    assert payload["projectData"]['absolute_path'] == "F:/boards/demo.kicad_pcb"
    assert payload["targetBGAs"] == ["U5", "U7"]
    assert payload["fanoutParamPlan"]["jump_to"] == "layer_assign_escape_order"
    assert payload["fanoutParamPlan"]["constraints"]["normalized"] == {"LineWidth": 3, "LineSpacing": 3}
    assert adapter._session_requested_bga_targets[session_id] == "U5"
    assert adapter._session_fanout_params[session_id]["constraints"] == {"LineWidth": 3, "LineSpacing": 3}