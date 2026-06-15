import asyncio
import json
from pathlib import Path
from typing import Any


from gateway.config import PlatformConfig
from gateway.platforms.websocket import WebSocketAdapter


def _make_adapter(**extra: Any) -> WebSocketAdapter:
    merged_extra = {
        "host": "127.0.0.1",
        "port": 0,
        "route_intent_llm_enabled": False,
        "fanout_param_llm_enabled": False,
        "trace_pcb_messages": False,
    }
    merged_extra.update(extra)
    return WebSocketAdapter(PlatformConfig(enabled=True, extra=merged_extra))


def test_direct_fanout_step_applies_natural_language_order_and_autoroutes(monkeypatch, tmp_path):
    async def run():
        adapter = _make_adapter()
        session_id = "sess-nl-fanout-direct"
        user_text = "对 U22 开始布线，NET_A、NET_B 走 SIG03，NET_C 走 SIG04，线宽 5mil 间距 4mil"
        sent: list[str] = []
        routed_payloads: list[dict[str, Any]] = []

        async def fake_send(chat_id: str, content: str, **kwargs):
            sent.append(content)
            return None

        def fake_generate_fanout_params(**kwargs):
            assert kwargs["selected_bga"] == "U22"
            assert kwargs["router_type"] == "auto_arc"
            assert kwargs["constraints"] == {"LineWidth": 5, "LineSpacing": 4}
            return {
                "selectedBGA": "U22",
                "routerType": "ga_arc",
                "orderLines": [
                    {"net": "NET_A", "layer": "SIG01", "order": 1},
                    {"net": "NET_B", "layer": "SIG01", "order": 2},
                    {"net": "NET_C", "layer": "SIG02", "order": 3},
                ],
                "constraints": kwargs["constraints"],
            }

        def fake_route_bga(user_data: str, session_id: str = ""):
            routed_payloads.append(json.loads(user_data))
            return json.dumps({"routingResult": str(tmp_path / "routing_input.txt"), "report": "布线完成"}, ensure_ascii=False)

        monkeypatch.setattr(adapter, "send", fake_send)
        monkeypatch.setattr("tools.pcb_bjut_router.bjut_router_available", lambda router_type, work_dir=None: True)
        monkeypatch.setattr("tools.pcb_bjut_router.generate_fanout_params", fake_generate_fanout_params)
        monkeypatch.setattr("tools.pcb_tools.route_bga", fake_route_bga)
        from tools import pcb_tools
        pcb_tools._transport.set_session_mode(session_id, "pcb")
        pcb_tools._transport.cache_project_data("(pcb_data)", session_id=session_id)
        adapter._remember_board_analysis(
            session_id,
            {
                "selection": [{"label": "U22", "detail": "BGA"}],
                "boardSummary": {"netSummary": {"signalNets": ["NET_A", "NET_B", "NET_C"]}},
                "fanoutContext": {"recommendedEscapeLayers": ["SIG01", "SIG02"]},
            },
        )
        adapter._session_selected_targets[session_id] = "U22"
        adapter._remember_fanout_request_text(session_id, user_text)

        assert await adapter._run_direct_fanout_param_step(session_id, user_text) is True

        fanout_params = adapter._session_fanout_params[session_id]
        assert fanout_params["naturalLanguageOrderLines"] == [
            {"net": "NET_A", "layer": "SIG03", "order": 1},
            {"net": "NET_B", "layer": "SIG03", "order": 2},
            {"net": "NET_C", "layer": "SIG04", "order": 3},
        ]
        assert fanout_params["orderLines"] == fanout_params["naturalLanguageOrderLines"]
        assert fanout_params["constraints"] == {"LineWidth": 5, "LineSpacing": 4}
        assert fanout_params["routerType"] == "ga_arc"
        assert routed_payloads and routed_payloads[0]["orderLines"] == fanout_params["orderLines"]
        assert routed_payloads[0]["routerType"] == "ga_arc"
        assert any("已按你的自然语言要求开始布线" in item for item in sent)

    asyncio.run(run())
