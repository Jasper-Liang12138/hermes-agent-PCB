"""Mode guard tests for PCB tools."""

from __future__ import annotations

import json
import configparser
from pathlib import Path
from types import SimpleNamespace

import pytest

from model_tools import handle_function_call
from tools import pcb_model_runtime
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


def _router_call_executable_name(call: list[str]) -> str:
    if call and Path(call[0]).name.startswith("python") and len(call) > 1:
        return Path(call[1]).name
    return Path(call[0]).name


def test_read_router_report_uses_statistical_output_when_data_txt_missing(tmp_path):
    (tmp_path / "statistical.out").write_text(
        "布线失败的引脚个数:2个\n"
        "引脚名称:\n"
        "B19\n"
        "C19\n",
        encoding="utf-8",
    )

    report = pcb_tools._read_router_report(tmp_path)

    assert "失败引脚: B19、C19（共 2 个）" in report
    assert "\nB19\n" not in report


def test_read_router_report_summarizes_router_output_when_report_missing(tmp_path):
    (tmp_path / "line.out").write_text("line 1\nline 2\n", encoding="utf-8")

    report = pcb_tools._read_router_report(tmp_path)

    assert "布线器未输出详细报告" in report
    assert "line.out" in report
    assert "2 lines" in report


def test_reroute_report_preserves_markdown_newlines():
    payload = {
        "rerouteResult": {
            "drcPassed": False,
            "drcAttempts": [
                {
                    "drcResult": {
                        "details": {
                            "hard_issue_count": 2,
                            "hard_rule_counts": {"HR_CONNECT_PAD_NOT_ESCAPED": 1, "HR_DRC_SEGMENT_CROSSING": 1},
                        },
                        "issuesPreview": [
                            {
                                "rule": "HR_CONNECT_PAD_NOT_ESCAPED",
                                "message": "BGA pad U5.A5 on net N1 has no initial escape connection.",
                            }
                        ],
                    }
                }
            ],
        },
        "checkReport": {
            "passed": False,
            "checks": [{"name": "drc", "passed": False, "detail": "短路错误\n第二行"}],
        },
    }

    report = pcb_tools._compose_reroute_report_content(
        payload=payload,
        public_txt_path="",
        explain_report="可解释性分析报告\n================\n\n预测结果: 布线较差",
    )
    public_payload = pcb_tools._compact_public_reroute_payload({"content": report, "report": report})

    assert "DRC 分析\n========" in public_payload["report"]
    assert "可解释性分析报告\n================" in public_payload["report"]
    assert "- 硬 DRC 问题数：2" in public_payload["report"]
    assert "`HR_CONNECT_PAD_NOT_ESCAPED`" in public_payload["report"]
    assert "issues=[" not in public_payload["report"]
    assert "DRC 分析 ========" not in public_payload["report"]


def test_default_reroute_drc_iterations_is_three(monkeypatch):
    monkeypatch.delenv("PCB_REROUTE_MAX_DRC_ITERATIONS", raising=False)

    assert pcb_tools._get_max_drc_iterations({}) == 3


def test_extract_reroute_nets_ignores_bare_net_word():
    assert pcb_tools.extract_reroute_nets("请重布这个 net") == []
    assert pcb_tools.extract_reroute_nets("请重布 NET_A 和 net") == ["NET_A"]


def test_delete_payload_prefers_frontend_missing_route_nets():
    payload = pcb_tools._normalize_delete_for_rerouting_payload(
        {
            "missing_routes": [
                {
                    "net_name": "Z7_SPI0_SCK",
                    "start": {"layer": "Top", "x": 1, "y": 2},
                    "end": {"layer": "Top", "x": 3, "y": 4},
                }
            ],
            "projectData": "(pcb)",
        },
        user_text="请重布 NET_EXTRA 和 net",
        project_id="proj1",
    )

    assert payload["selectedNets"] == ["Z7_SPI0_SCK"]


def test_default_reroute_model_timeout_is_600(monkeypatch):
    monkeypatch.delenv("PCB_REROUTE_TIMEOUT", raising=False)
    monkeypatch.delenv("CTYUN_REROUTE_TIMEOUT", raising=False)

    assert pcb_tools._get_reroute_model_timeout_seconds() == 600.0


def test_reroute_model_max_tokens_reads_config_ini(monkeypatch):
    monkeypatch.delenv("PCB_REROUTE_MAX_TOKENS", raising=False)
    parser = configparser.ConfigParser()
    parser.read_dict({"reroute-model": {"max_tokens": "1024"}})
    monkeypatch.setattr(pcb_model_runtime, "_load_project_config_ini", lambda: parser)

    assert pcb_tools._get_reroute_model_max_tokens() == 1024


def test_reroute_model_max_tokens_env_overrides_config_and_is_capped(monkeypatch):
    monkeypatch.setenv("PCB_REROUTE_MAX_TOKENS", "999999")
    parser = configparser.ConfigParser()
    parser.read_dict({"reroute-model": {"max_tokens": "1024"}})
    monkeypatch.setattr(pcb_model_runtime, "_load_project_config_ini", lambda: parser)

    assert pcb_tools._get_reroute_model_max_tokens() == 8192


@pytest.fixture(autouse=True)
def _restore_transport_state(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    prev_session = transport.current_session_id
    prev_modes = dict(transport._session_modes)
    prev_cache = dict(transport._cached_project_data)
    prev_reroute_cache = dict(transport._cached_reroute_context)
    prev_pending_fields = dict(transport._pending_pcb_fields)
    prev_adapter = transport._websocket_adapter
    prev_loop = transport._main_loop
    monkeypatch.setattr(
        pcb_tools,
        "generate_explain_report",
        lambda **kwargs: "测试可解释性报告：来自本地 explain 分类模型。",
    )
    monkeypatch.setattr(
        pcb_tools,
        "generate_drc_agent_report",
        lambda **kwargs: {
            "ok": True,
            "json_path": str(Path(kwargs["output_dir"]) / "drc_agent_report" / "mock_drc_agent.json"),
            "payload": {
                "schema_version": "drc_agent_v2",
                "language": "zh-CN",
                "message_zh": "DRC规则检查结果：mock hard 规则报告。",
                "result": {"hard_issue_count": 0},
                "issues": [],
            },
        },
    )
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


def test_generate_reroute_with_model_uses_reroute_runtime(monkeypatch):
    captured = {}

    def _fake_resolve_model_runtime(stage, **kwargs):
        captured["stage"] = stage
        captured["kwargs"] = kwargs
        return {
            "model": "reroute-only-model",
            "base_url": "https://reroute.example/v1",
            "api_key": "reroute-secret-value",
        }

    class _FakeAdapter:
        def __init__(self, **kwargs):
            captured["adapter_kwargs"] = kwargs

        def generate(self, prompt_bundle, generation_config):
            captured["prompt_bundle"] = prompt_bundle
            captured["generation_config"] = generation_config
            return (
                json.dumps(
                    {
                        "kicadPatch": (
                            "(segment (start 1 1) (end 2 2) "
                            "(width 0.1524) (layer Top) (net 1))"
                        )
                    }
                ),
                {"model": "reroute-only-model"},
            )

    from tools import pcb_chunking_tool

    monkeypatch.setattr(pcb_tools.pcb_model_runtime, "resolve_model_runtime", _fake_resolve_model_runtime)
    monkeypatch.setattr(
        pcb_chunking_tool,
        "_make_openai_compatible_chat_adapter",
        lambda **kwargs: _FakeAdapter(**kwargs),
    )

    payload = pcb_tools._generate_reroute_with_model(
        nets=["N1"],
        selected_trace_ids=["trace-1"],
        dropped_board_data=(
            '(segment (start 0 0) (end 1 1) (width 0.1524) (layer "Top") (net 1))'
        ),
        dropped_board_path="board-after-drop.txt",
        dropped_objects=[],
        local_context={},
        constraints={},
        check_report={},
        session_id="sess-reroute-model",
    )

    assert captured["stage"] == pcb_model_runtime.STAGE_REROUTE
    assert captured["kwargs"]["require_api_key"] is True
    assert captured["adapter_kwargs"]["model"] == "reroute-only-model"
    assert payload["kicadPatch"]


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


def test_generate_fanout_params_blocked_in_chat_mode():
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-chat-fanout"
    transport.set_session_mode("sess-chat-fanout", "chat")

    result = pcb_tools.generate_fanout_params_tool(selectedBGA="U27", routerType="arc")
    payload = json.loads(result)

    assert "error" in payload
    assert "被拒绝" in payload["error"]


def test_generate_fanout_params_uses_cached_project_data(monkeypatch, tmp_path):
    from tools import pcb_bjut_router

    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    session_id = "sess-pcb-fanout"
    transport.current_session_id = session_id
    transport.set_session_mode(session_id, "pcb")
    transport.cache_project_data(
        '(pcb_data (component (name "U27") (package "BGA-256")))',
        session_id=session_id,
    )
    monkeypatch.setenv("ROUTER_WORK_DIR", str(tmp_path))

    seen = {}

    def fake_generate_fanout_params(**kwargs):
        seen.update(kwargs)
        return {
            "selectedBGA": kwargs["selected_bga"],
            "routerType": kwargs["router_type"],
            "orderLines": [{"net": "GND", "layer": "Top", "order": 1}],
        }

    monkeypatch.setattr(pcb_bjut_router, "bjut_router_available", lambda router_type, work_dir: True)
    monkeypatch.setattr(pcb_bjut_router, "generate_fanout_params", fake_generate_fanout_params)

    result = pcb_tools.generate_fanout_params_tool(
        selectedBGA="U27",
        routerType="rl_135",
        constraints={"LineWidth": "5", "LineSpacing": 0},
        session_id=session_id,
    )
    payload = json.loads(result)

    assert payload["fanoutParams"] == {
        "selectedBGA": "U27",
        "routerType": "rl_135",
        "orderLines": [{"net": "GND", "layer": "Top", "order": 1}],
        "constraints": {"LineWidth": 5.0, "LineSpacing": 3},
    }
    assert seen["project_data"] == '(pcb_data (component (name "U27") (package "BGA-256")))'
    assert seen["selected_bga"] == "U27"
    assert seen["router_type"] == "rl_135"
    assert seen["work_dir"] == tmp_path.resolve()
    assert seen["constraints"] == {"LineWidth": 5.0, "LineSpacing": 3}


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
    (router_dir / "constrain.txt").write_text("PROFILE_CONSTRAINT\n", encoding="utf-8")
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
        "U35\n1\n2\nNET_A_P_SIG03 SIG03 1\nNET_A_N_SIG03 SIG03 2"
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
    (router_dir / "constrain.txt").write_text("PROFILE_CONSTRAINT\n", encoding="utf-8")
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
        "FPGA1\n1\n2\nNET_A_P_SIG03 SIG03 1\nNET_A_N_SIG03 SIG03 2"
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
    (router_dir / "constrain.txt").write_text("PROFILE_CONSTRAINT\n", encoding="utf-8")
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
        "U27\n1\n2\nNET_A_P_SIG03 SIG03 1\nNET_A_N_SIG03 SIG03 2"
    )
    assert (work_dir / "constrain.txt").read_text(encoding="utf-8") == "PROFILE_CONSTRAINT\n"
    assert [_router_call_executable_name(call) for call in calls] == ["c.out"]


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
    assert (work_dir / "order_input.txt").read_text(encoding="utf-8") == "U22\n1\n1\nVCC SIG04 2"
    assert [_router_call_executable_name(call) for call in calls] == [
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
        if tool_name == "deleteTracesForRerouting":
            return {
                "result": json.dumps(
                    {
                        "missing_routes": [
                            {
                                "net_name": "NET_U1_B7",
                                "start": {"component": "U1", "pad": "B7", "layer": "Top", "x": 47.3, "y": 62.3},
                                "end": {"layer": "Top", "x": 47.3, "y": 68.3},
                            }
                        ],
                        "projectData": "(pcb after delete)",
                    },
                    ensure_ascii=False,
                )
            }
        raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _fake_call_tool_sync)

    result = pcb_tools.drop_net("reroute selected traces", projectID="proj1")
    payload = json.loads(result)

    assert calls == [
        (
            "deleteTracesForRerouting",
            {},
            120.0,
            "sess-pcb-drop",
        ),
    ]
    assert payload["selectedNets"] == ["NET_U1_B7"]
    assert payload["selectedTraceIds"] == []
    assert payload["missingRoutes"][0]["net_name"] == "NET_U1_B7"
    assert "droppedBoardData" not in payload
    assert payload["droppedBoardDataChars"] == len("(pcb after delete)")
    cached = transport.get_cached_reroute_context("sess-pcb-drop")
    assert cached["selectedNets"] == ["NET_U1_B7"]
    assert cached["localContext"]["source"] == "deleteTracesForRerouting"
    assert cached["localContext"]["missingRoutes"][0]["net_name"] == "NET_U1_B7"


def test_delete_traces_for_rerouting_prefers_frontend_routes_over_user_text_nets(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-drop-text-nets"
    transport.set_session_mode("sess-pcb-drop-text-nets", "pcb")
    calls = []

    def _fake_call_tool_sync(tool_name, arguments, timeout=30.0, session_id=None):
        calls.append((tool_name, arguments, timeout, session_id))
        if tool_name == "deleteTracesForRerouting":
            return {
                "missing_routes": [
                    {"net_name": "net13", "start": {"layer": "Top", "x": 1, "y": 2}, "end": {"layer": "Top", "x": 3, "y": 4}}
                ],
                "projectData": "(pcb before text-net reroute)",
            }
        raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _fake_call_tool_sync)

    result = pcb_tools.delete_traces_for_rerouting("请把 net13、net17 拆线后重布", projectID="proj1")
    payload = json.loads(result)

    assert calls == [
        (
            "deleteTracesForRerouting",
            {},
            120.0,
            "sess-pcb-drop-text-nets",
        ),
    ]
    assert payload["selectedNets"] == ["net13"]
    assert payload["selectedTraceIds"] == []
    assert "droppedBoardData" not in payload
    assert payload["droppedBoardDataChars"] == len("(pcb before text-net reroute)")
    cached = transport.get_cached_reroute_context("sess-pcb-drop-text-nets")
    assert cached["selectedNets"] == ["net13"]
    assert cached["localContext"]["source"] == "deleteTracesForRerouting"


def test_delete_traces_for_rerouting_surfaces_frontend_error(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-drop-many"
    transport.set_session_mode("sess-pcb-drop-many", "pcb")
    calls = []

    def _fake_call_tool_sync(tool_name, arguments, timeout=30.0, session_id=None):
        calls.append((tool_name, arguments, timeout, session_id))
        if tool_name == "deleteTracesForRerouting":
            return {"error": "Selected trace count exceeds 40."}
        raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _fake_call_tool_sync)

    result = pcb_tools.delete_traces_for_rerouting("reroute selected traces", projectID="proj1")
    payload = json.loads(result)

    assert calls == [
        (
            "deleteTracesForRerouting",
            {},
            120.0,
            "sess-pcb-drop-many",
        )
    ]
    assert payload["error"] == "Selected trace count exceeds 40."
    assert payload["frontendError"]["message"] == "Tool execution failed"
    assert transport.get_cached_reroute_context("sess-pcb-drop-many") is None


def test_delete_traces_for_rerouting_rejects_too_many_missing_routes(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-drop-too-many-routes"
    transport.set_session_mode("sess-pcb-drop-too-many-routes", "pcb")
    routes = [
        {
            "net_name": f"NET_{idx}",
            "start": {"layer": "Top", "x": idx, "y": idx + 1},
            "end": {"layer": "Top", "x": idx + 2, "y": idx + 3},
        }
        for idx in range(41)
    ]

    monkeypatch.setattr(
        pcb_tools._transport,
        "call_tool_sync",
        lambda *args, **kwargs: {"missing_routes": routes, "projectData": "(pcb after delete)"},
    )

    payload = json.loads(pcb_tools.delete_traces_for_rerouting("reroute selected traces", projectID="proj1"))

    assert "超过 40Pin" in payload["error"]
    assert payload["frontendError"]["code"] == 50001
    assert transport.get_cached_reroute_context("sess-pcb-drop-too-many-routes") is None


def test_delete_traces_for_rerouting_rejects_invalid_missing_route_shape(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-drop-invalid-route"
    transport.set_session_mode("sess-pcb-drop-invalid-route", "pcb")

    monkeypatch.setattr(
        pcb_tools._transport,
        "call_tool_sync",
        lambda *args, **kwargs: {
            "missing_routes": [
                {
                    "net_name": "NET_U1_B7",
                    "start": {"layer": "Top", "x": 47.3},
                    "end": {"layer": "Top", "x": 47.3, "y": 68.3},
                }
            ],
            "projectData": "(pcb after delete)",
        },
    )

    payload = json.loads(pcb_tools.delete_traces_for_rerouting("reroute selected traces", projectID="proj1"))

    assert "start 缺少有效 y 坐标" in payload["error"]
    assert transport.get_cached_reroute_context("sess-pcb-drop-invalid-route") is None


def test_delete_traces_for_rerouting_accepts_non_bga_selection(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-drop-non-bga"
    transport.set_session_mode("sess-pcb-drop-non-bga", "pcb")

    monkeypatch.setattr(
        pcb_tools._transport,
        "call_tool_sync",
        lambda *args, **kwargs: {
            "isBgaEscape": False,
            "missing_routes": [
                {
                    "net_name": "NET_U1_B7",
                    "start": {"layer": "Top", "x": 47.3, "y": 62.3},
                    "end": {"layer": "Top", "x": 47.3, "y": 68.3},
                }
            ],
            "projectData": "(pcb after delete)",
        },
    )

    payload = json.loads(pcb_tools.delete_traces_for_rerouting("reroute selected traces", projectID="proj1"))

    assert "error" not in payload
    assert payload["selectedNets"] == ["NET_U1_B7"]
    assert transport.get_cached_reroute_context("sess-pcb-drop-non-bga")["selectedNets"] == ["NET_U1_B7"]


def test_delete_traces_for_rerouting_rejects_unreadable_project_data(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-drop-bad-project-data"
    transport.set_session_mode("sess-pcb-drop-bad-project-data", "pcb")

    monkeypatch.setattr(
        pcb_tools._transport,
        "call_tool_sync",
        lambda *args, **kwargs: {
            "missing_routes": [
                {
                    "net_name": "NET_U1_B7",
                    "start": {"layer": "Top", "x": 47.3, "y": 62.3},
                    "end": {"layer": "Top", "x": 47.3, "y": 68.3},
                }
            ],
            "projectData": r"F:\does_not_exist\missing_board.txt",
        },
    )

    payload = json.loads(pcb_tools.delete_traces_for_rerouting("reroute selected traces", projectID="proj1"))

    assert "projectData 不可读" in payload["error"]
    assert transport.get_cached_reroute_context("sess-pcb-drop-bad-project-data") is None


def test_delete_traces_for_rerouting_rejects_non_json_result(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-drop-strict"
    transport.set_session_mode("sess-pcb-drop-strict", "pcb")
    calls = []

    def _fake_call_tool_sync(tool_name, arguments, timeout=30.0, session_id=None):
        calls.append((tool_name, arguments, timeout, session_id))
        if tool_name == "deleteTracesForRerouting":
            return "['2386476278', '3424247826']"
        raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(pcb_tools._transport, "call_tool_sync", _fake_call_tool_sync)

    result = pcb_tools.delete_traces_for_rerouting("reroute selected traces", projectID="proj1")
    payload = json.loads(result)

    assert calls == [
        (
            "deleteTracesForRerouting",
            {},
            120.0,
            "sess-pcb-drop-strict",
        )
    ]
    assert payload["selectedNets"] == []
    assert "未检测到框选走线" in payload["error"]
    assert payload["frontendError"]["code"] == 50001


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
    model_failure = (
        "模型输出中未找到合法的 `(segment ...)` 或 `(via ...)` 走线对象，"
        "因此无法回填版图，DRC 未执行，也不会生成 txt 或调用 importLines。"
    )

    def _fake_generate(**kwargs):
        payload = pcb_tools._build_fallback_reroute_payload(**kwargs)
        payload["rerouteResult"]["modelGenerationFailure"] = model_failure
        return payload

    monkeypatch.setattr(pcb_tools, "_generate_reroute_with_model", _fake_generate)

    result = pcb_tools.reroute(session_id="sess-pcb-reroute")
    payload = json.loads(result)

    assert payload["rerouteResult"]["type"] == "local_reroute"
    assert payload["rerouteResult"]["selectedNets"] == ["net13", "net17"]
    assert payload["rerouteResult"]["operations"][0]["action"] == "reroute_net"
    assert payload["checkReport"]["passed"] is False
    assert "routedLayoutTxtFilePath" not in payload
    assert model_failure in payload["explanation"]
    assert "局部重布" in payload["explanation"]
    assert "DRC 分析" in payload["content"]
    assert "DRC 状态: 未执行" in payload["content"]
    assert model_failure in payload["content"]
    assert "Explain 模型可解释性报告" in payload["content"]
    assert "测试可解释性报告：来自本地 explain 分类模型。" in payload["content"]
    assert "布线较好概率: 0.984707" not in payload["content"]


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
    model_failure = (
        "模型输出中未找到合法的 `(segment ...)` 或 `(via ...)` 走线对象，"
        "因此无法回填版图，DRC 未执行，也不会生成 txt 或调用 importLines。"
    )

    def _fake_generate(**kwargs):
        payload = pcb_tools._build_fallback_reroute_payload(**kwargs)
        payload["rerouteResult"]["modelGenerationFailure"] = model_failure
        return payload

    monkeypatch.setattr(pcb_tools, "_generate_reroute_with_model", _fake_generate)

    result = pcb_tools.reroute(session_id="sess-pcb-reroute-traces")
    payload = json.loads(result)

    assert payload["rerouteResult"]["mode"] == "selected_traces_after_delete"
    assert payload["rerouteResult"]["selectedTraceIds"] == ["2386476278", "3424247826"]
    assert payload["rerouteResult"]["operations"][0]["action"] == "reroute_selected_traces"
    assert payload["checkReport"]["passed"] is False
    assert "routedLayoutTxtFilePath" not in payload
    assert model_failure in payload["explanation"]


def test_extract_kicad_patch_from_non_json_model_text():
    text = """
    下面是重布结果：
    (segment (start 1 1) (end 2 2) (width 0.2) (layer Top) (net 73))
    (via (at 2 2) (size 0.45) (drill 0.2) (layers Top Bottom) (net 73))
    (module SHOULD_NOT_BE_PATCH (layer Top))
    """

    patch = pcb_tools._extract_kicad_patch_from_model_text(text)

    assert "(segment" in patch
    assert "(via" in patch
    assert "(module" not in patch


def test_drc_feedback_for_prompt_includes_issue_details():
    attempt = SimpleNamespace(
        failure_summary="hard_issue_count=2",
        fill_detail={"segments_count": 1, "vias_count": 0},
        drc_result={
            "details": {
                "hard_issue_count": 2,
                "hard_rule_counts": {"clearance": 1, "short": 1},
            },
            "artifacts": {
                "issues": [
                    {"rule": "clearance", "message": "too close", "severity": "error"},
                    {"rule": "short", "description": "net short", "severity": "error"},
                ]
            },
        },
    )

    feedback = pcb_tools._drc_feedback_for_prompt(attempt)

    assert "硬 DRC 问题数量：2" in feedback
    assert "clearance" in feedback
    assert "too close" in feedback
    assert "patch 回填统计" in feedback
    assert "不能原样重复上一轮 patch" in feedback


def test_reroute_model_payload_ignores_model_report_fields():
    fallback = pcb_tools._build_fallback_reroute_payload(
        nets=["net13"],
        dropped_board_data="(kicad_pcb)",
        dropped_board_path="/tmp/dropped.kicad_pcb",
        dropped_objects=[],
        local_context={},
        constraints={},
        check_report={"passed": True, "checks": []},
        original_board_path="/tmp/original.kicad_pcb",
    )

    payload = pcb_tools._normalize_reroute_model_payload(
        {
            "kicadPatch": "(segment (start 1 1) (end 2 2) (width 0.2) (layer F.Cu) (net 13))",
            "content": "模型不应该生成报告",
            "report": "模型不应该生成报告",
            "explanation": "模型不应该覆盖系统 explanation",
            "checkReport": {"passed": False},
        },
        fallback_payload=fallback,
        context_stats={"chunkCount": 1},
    )

    assert payload["kicadPatch"].startswith("(segment")
    assert "content" not in payload
    assert "report" not in payload
    assert payload["explanation"] == fallback["explanation"]
    assert payload["checkReport"] == fallback["checkReport"]


def test_explain_prompt_uses_board_content_without_internal_path(tmp_path):
    board_path = tmp_path / "internal_board.kicad_pcb"
    board_path.write_text("(kicad_pcb (segment (start 1 1) (end 2 2)))", encoding="utf-8")

    messages = pcb_tools._build_explain_prompt(
        internal_board_path=str(board_path),
        payload={
            "rerouteResult": {
                "routedBoardDataFilePath": str(board_path),
                "drcPassed": True,
            },
            "checkReport": {"passed": True, "checks": []},
        },
        public_txt_path=str(tmp_path / "routed.txt"),
    )
    serialized = json.dumps(messages, ensure_ascii=False)

    assert "(kicad_pcb" in serialized
    assert str(board_path) not in serialized


def test_reroute_without_model_patch_reports_no_txt_reason(monkeypatch):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-reroute-no-patch"
    transport.set_session_mode("sess-pcb-reroute-no-patch", "pcb")
    transport.cache_reroute_context(
        {
            "selectedNets": ["net13"],
            "droppedBoardData": "(kicad_pcb\n)\n",
            "droppedObjects": [],
            "localContext": {},
        },
        session_id="sess-pcb-reroute-no-patch",
    )

    model_failure = (
        "模型输出中未找到合法的 `(segment ...)` 或 `(via ...)` 走线对象，"
        "因此无法回填版图，DRC 未执行，也不会生成 txt 或调用 importLines。"
    )

    def _fake_generate(**kwargs):
        payload = pcb_tools._build_fallback_reroute_payload(**kwargs)
        payload["rerouteResult"]["modelGenerationFailure"] = model_failure
        return payload

    monkeypatch.setattr(pcb_tools, "_generate_reroute_with_model", _fake_generate)

    result = pcb_tools.reroute(session_id="sess-pcb-reroute-no-patch")
    payload = json.loads(result)

    assert payload["checkReport"]["passed"] is False
    assert any(
        check["name"] == "model_patch" and check["passed"] is False
        for check in payload["checkReport"]["checks"]
    )
    assert "routedLayoutTxtFilePath" not in payload
    assert model_failure in payload["explanation"]
    pending = pcb_tools._transport.pop_pending_pcb_fields("sess-pcb-reroute-no-patch")
    assert pending["rerouteResult"]["type"] == "local_reroute"
    assert pending["checkReport"]["passed"] is False
    assert pending["rerouteResult"]["modelGenerationFailure"] == model_failure
    assert model_failure in pending["explanation"]
    assert "DRC 分析" in pending["report"]
    assert "DRC 状态: 未执行" in pending["report"]
    assert model_failure in pending["report"]
    assert "txt 输出: 未生成" in pending["report"]
    assert "importLines: 不允许" in pending["report"]
    assert "Explain 模型可解释性报告" in pending["report"]
    assert "测试可解释性报告：来自本地 explain 分类模型。" in pending["report"]


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


def test_reroute_model_prompt_hides_internal_kicad_paths():
    prompts = pcb_tools._build_reroute_generation_prompts(
        nets=["net13"],
        selected_trace_ids=["2386476278"],
        dropped_board_path="/private/tmp/secret/after_drop.kicad_pcb",
        original_board_path="/private/tmp/secret/original.kicad_pcb",
        dropped_objects=[
            {
                "id": "2386476278",
                "debugPath": "/private/tmp/secret/trace.kicad_pcb",
            }
        ],
        local_context={
            "originalBoardDataFilePath": "/private/tmp/secret/original.kicad_pcb",
            "source": "getSelectedElements/deleteTracesById/getProjectData",
        },
        constraints={},
        context_text="(kicad_pcb (segment (start 1 1) (end 2 2)))",
        context_stats={"chunkCount": 1},
    )

    combined = json.dumps(prompts, ensure_ascii=False)

    assert ".kicad_pcb" not in combined
    assert "/private/tmp/secret" not in combined
    assert "originalBoardDataFilePath" not in combined
    assert "droppedBoardDataFilePath" not in combined
    assert "internalBoardPathHidden" not in prompts["user"]


def test_reroute_prompt_uses_single_shot_answer_contract_and_endpoint_summary():
    board_text = """
    (kicad_pcb
      (net 73 "NET_U1_B7")
      (module BGA_BENCHMARK (layer Top)
        (at 50.800000 50.800000)
        (fp_text reference U1 (at 0 16 0))
        (pad B7 smd circle (at -3.5 11.5) (layers Top F.Paste F.Mask) (net 73 "NET_U1_B7"))
      )
      (module TP (layer Top)
        (at 47.300000 68.300000)
        (fp_text reference TP27 (at 0 -1 0))
        (pad 1 smd circle (at 0 0) (layers Top F.Paste F.Mask) (net 73 "NET_U1_B7"))
      )
    )
    """
    task_description, task_stats = pcb_tools._build_missing_route_description(
        board_text=board_text,
        nets=[],
        dropped_objects=[{"id": "2386476278", "net": "NET_U1_B7"}],
        local_context={},
        selected_trace_ids=["2386476278"],
    )
    context = pcb_tools._build_single_shot_reroute_context(
        board_text=board_text,
        task_description=task_description,
        selected_trace_ids=["2386476278"],
        nets=[],
    )
    prompts = pcb_tools._build_reroute_generation_prompts(
        nets=[],
        selected_trace_ids=["2386476278"],
        dropped_board_path="/private/tmp/secret/after_drop.kicad_pcb",
        original_board_path="/private/tmp/secret/original.kicad_pcb",
        dropped_objects=[{"id": "2386476278", "net": "NET_U1_B7"}],
        local_context={},
        constraints={},
        context_text=context["contextText"],
        context_stats={**context["stats"], **task_stats},
        task_description=task_description,
    )

    assert "只输出合法 KiCad 走线对象，不要输出推理过程" in prompts["system"]
    assert prompts["user"].startswith("/no_think\n")
    assert "只允许输出纯文本形式的 (segment ...)" in prompts["user"]
    assert "最终只输出缺失走线对象，不要输出其它内容" in prompts["user"]
    assert "47.30, 62.30" in prompts["user"]
    assert "47.30, 68.30" in prompts["user"]
    assert "只输出 JSON" not in prompts["system"]
    assert "最终答案必须放在 <answer>" not in prompts["user"]
    assert "<answer>" not in prompts["user"]


def test_reroute_prompt_includes_frontend_missing_route_start_and_end():
    board_text = """
    (kicad_pcb
      (net 91 "Z7_SPI0_SCK")
    )
    """
    local_context = {
        "source": "deleteTracesForRerouting",
        "missingRoutes": [
            {
                "net_name": "Z7_SPI0_SCK",
                "start": {"component": "U5", "pad": "A5", "layer": "Top", "x": 558.23, "y": 132.79},
                "end": {"layer": "Top", "x": 558.23, "y": -6.76},
            }
        ],
    }
    task_description, task_stats = pcb_tools._build_missing_route_description(
        board_text=board_text,
        nets=["Z7_SPI0_SCK"],
        dropped_objects=[],
        local_context=local_context,
        selected_trace_ids=[],
    )
    context = pcb_tools._build_single_shot_reroute_context(
        board_text=board_text,
        task_description=task_description,
        selected_trace_ids=[],
        nets=["Z7_SPI0_SCK"],
    )
    prompts = pcb_tools._build_reroute_generation_prompts(
        nets=["Z7_SPI0_SCK"],
        selected_trace_ids=[],
        dropped_board_path="after_drop.txt",
        original_board_path="before_drop.txt",
        dropped_objects=[],
        local_context=local_context,
        constraints={},
        context_text=context["contextText"],
        context_stats={**context["stats"], **task_stats},
        task_description=task_description,
    )

    assert task_stats["frontendMissingRouteCount"] == 1
    assert "前端删除线段" in prompts["user"]
    assert "Z7_SPI0_SCK" in prompts["user"]
    assert "KiCad net id 必须使用 91" in prompts["user"]
    assert "U5.A5" in prompts["user"]
    assert "558.23, 132.79" in prompts["user"]
    assert "558.23, -6.76" in prompts["user"]
    assert "不得改变坐标正负号" in prompts["user"]
    assert "segment/via 必须精确连接这些端点" in prompts["user"]


def test_bind_reroute_patch_nets_rewrites_single_selected_net_id():
    board_text = """
    (kicad_pcb
      (net 58 Z7_SPI0_SCK)
      (net 145 PHY1_RXD1)
    )
    """
    patch, warnings, errors = pcb_tools._bind_reroute_patch_nets(
        "(segment (start 558.23 132.79) (end 558.23 -6.76) (width 0.1524) (layer Top) (net 145))",
        board_text=board_text,
        selected_nets=["Z7_SPI0_SCK"],
        local_context={},
    )

    assert errors == []
    assert warnings
    assert "(net 58)" in patch
    assert "(net 145)" not in patch


def test_bind_reroute_patch_nets_assigns_multiple_nets_by_missing_route_endpoint():
    board_text = """
    (kicad_pcb
      (net 58 Z7_SPI0_SCK)
      (net 77 Z7_SPI0_IO3)
    )
    """
    local_context = {
        "missingRoutes": [
            {
                "net_name": "Z7_SPI0_SCK",
                "start": {"layer": "Top", "x": 558.23, "y": 132.79},
                "end": {"layer": "Top", "x": 558.23, "y": -6.76},
            },
            {
                "net_name": "Z7_SPI0_IO3",
                "start": {"layer": "Top", "x": 526.73, "y": 132.79},
                "end": {"layer": "Top", "x": 526.73, "y": -6.76},
            },
        ]
    }
    patch, _warnings, errors = pcb_tools._bind_reroute_patch_nets(
        "\n".join(
            [
                "(segment (start 558.23 132.79) (end 558.23 -6.76) (width 0.1524) (layer Top) (net 145))",
                "(segment (start 526.73 132.79) (end 526.73 -6.76) (width 0.1524) (layer Top) (net 145))",
            ]
        ),
        board_text=board_text,
        selected_nets=["Z7_SPI0_SCK", "Z7_SPI0_IO3"],
        local_context=local_context,
    )

    assert errors == []
    assert patch.count("(net 58)") == 1
    assert patch.count("(net 77)") == 1
    assert "(net 145)" not in patch


def test_bind_reroute_patch_nets_rejects_unknown_selected_net_id():
    patch, _warnings, errors = pcb_tools._bind_reroute_patch_nets(
        "(segment (start 1 1) (end 2 2) (width 0.1524) (layer Top) (net 145))",
        board_text="(kicad_pcb (net 58 Z7_SPI0_SCK))",
        selected_nets=["MISSING_NET"],
        local_context={},
    )

    assert "(net 145)" in patch
    assert errors
    assert "找不到 selected net" in errors[0]


def test_endpoint_guard_corrects_single_missing_route_endpoint():
    local_context = {
        "missingRoutes": [
            {
                "net_name": "Z7_SPI0_SCK",
                "start": {"layer": "Top", "x": 558.23, "y": 132.79},
                "end": {"layer": "Top", "x": 558.23, "y": -6.76},
            }
        ]
    }
    bad_patch = "(segment (start 558.230000 132.790000) (end 558.230000 126.760000) (width 0.304800) (layer Top) (net 58))"

    messages = pcb_tools._endpoint_guard_messages(
        bad_patch,
        selected_nets=["Z7_SPI0_SCK"],
        local_context=local_context,
    )
    fixed, warnings = pcb_tools._correct_single_missing_route_segment_endpoints(
        bad_patch,
        selected_nets=["Z7_SPI0_SCK"],
        local_context=local_context,
    )

    assert messages
    assert "558.230000, -6.760000" in messages[0]
    assert warnings
    assert "(end 558.230000 -6.760000)" in fixed
    assert "126.760000" not in fixed


def test_reroute_drc_loop_validates_endpoint_corrected_patch(monkeypatch, tmp_path):
    base_payload = {
        "kicadPatch": "(segment (start 558.230000 132.790000) (end 558.230000 126.760000) (width 0.304800) (layer Top) (net 145))",
        "rerouteResult": {},
        "checkReport": {"passed": True, "checks": []},
        "explanation": "",
    }
    original_board_data = "(kicad_pcb\n  (net 58 Z7_SPI0_SCK)\n  (net 145 PHY1_RXD1)\n)\n"
    seen_patches: list[str] = []

    def _fake_validate(**kwargs):
        seen_patches.append(kwargs["model_output_text"])
        return pcb_reroute_drc.RerouteDrcAttempt(
            iteration=kwargs["iteration"],
            passed=True,
            filled_board_data_file_path=str(tmp_path / "routed.kicad_pcb"),
            drc_result={"ok": True, "pass": True},
        )

    monkeypatch.setattr(pcb_reroute_drc, "validate_kicad_patch_with_drc", _fake_validate)

    payload = pcb_tools._run_reroute_drc_iterations(
        base_payload=base_payload,
        original_board_data=original_board_data,
        original_board_path=str(tmp_path / "original.kicad_pcb"),
        output_dir=str(tmp_path),
        sample_id="sample",
        max_iterations=1,
        selected_nets=["Z7_SPI0_SCK"],
        local_context={
            "missingRoutes": [
                {
                    "net_name": "Z7_SPI0_SCK",
                    "start": {"layer": "Top", "x": 558.23, "y": 132.79},
                    "end": {"layer": "Top", "x": 558.23, "y": -6.76},
                }
            ]
        },
        regenerate=lambda feedback, history: base_payload,
    )

    assert payload["rerouteResult"]["drcPassed"] is True
    assert seen_patches == [
        "(segment (start 558.230000 132.790000) (end 558.230000 -6.760000) (width 0.304800) (layer Top) (net 58))"
    ]
    assert payload["rerouteResult"]["patchBindingWarnings"]


def test_reroute_model_outputs_not_written_by_default(monkeypatch, tmp_path):
    monkeypatch.delenv("PCB_REROUTE_WRITE_MODEL_OUTPUTS", raising=False)

    output = pcb_tools._write_reroute_debug_artifact(
        output_dir=str(tmp_path),
        session_id="sess-secret",
        label="model_raw",
        content="sensitive model output",
    )

    assert output == ""
    assert not (tmp_path / "model_outputs").exists()


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
    assert payload["checkReport"]["passed"] is False
    assert any(
        check["name"] == "drc_validation" and check["passed"] is False
        for check in payload["checkReport"]["checks"]
    )


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
    assert payload["importLinesFilePath"].endswith("_reroute_import.txt")
    assert payload["rerouteResult"]["importLinesFilePath"] == payload["importLinesFilePath"]
    import_path = Path(payload["importLinesFilePath"])
    assert import_path.is_file()
    assert import_path.read_text(encoding="utf-8").lstrip().startswith("(wires")
    assert not import_path.read_text(encoding="utf-8").lstrip().startswith("(layout")
    assert "routedBoardDataFilePath" not in payload
    assert "originalBoardDataFilePath" not in payload["rerouteResult"]
    assert ".kicad_pcb" not in json.dumps(payload, ensure_ascii=False)
    assert payload["checkReport"]["passed"] is True
    assert "DRC 分析" in payload["content"]
    assert "DRC 状态: 通过" in payload["content"]
    assert "DRC规则检查结果：mock hard 规则报告。" in payload["content"]
    assert "txt 输出: 已生成" in payload["content"]
    assert str(tmp_path / "routed.txt") in payload["content"]
    assert "importLines: 允许" in payload["content"]
    assert "Explain 模型可解释性报告" in payload["content"]
    assert "测试可解释性报告：来自本地 explain 分类模型。" in payload["content"]
    assert payload["rerouteResult"]["drcAgentReport"]["ok"] is True


def test_reroute_drc_pass_without_txt_marks_failure(monkeypatch, tmp_path):
    transport = pcb_tools.WebSocketTransportSingleton.get_instance()
    transport.current_session_id = "sess-pcb-reroute-no-txt"
    transport.set_session_mode("sess-pcb-reroute-no-txt", "pcb")
    original_path = tmp_path / "original.kicad_pcb"
    original_path.write_text("(kicad_pcb\n)\n", encoding="utf-8")
    transport.cache_reroute_context(
        {
            "selectedNets": ["net13"],
            "droppedBoardData": "(kicad_pcb\n)\n",
            "originalBoardDataFilePath": str(original_path),
        },
        session_id="sess-pcb-reroute-no-txt",
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

    monkeypatch.setattr(pcb_tools, "_generate_reroute_with_model", _fake_generate)
    monkeypatch.setattr(pcb_reroute_drc, "validate_kicad_patch_with_drc", _fake_validate)
    monkeypatch.setattr(pcb_tools, "_convert_internal_kicad_to_public_txt", lambda **kwargs: ("", []))
    monkeypatch.setattr(pcb_tools, "_write_reroute_incremental_import_file", lambda **kwargs: ("", []))

    result = pcb_tools.reroute(session_id="sess-pcb-reroute-no-txt")
    payload = json.loads(result)

    assert payload["rerouteResult"]["drcPassed"] is True
    assert "routedLayoutTxtFilePath" not in payload
    assert payload["checkReport"]["passed"] is False
    assert any(
        check["name"] == "txt_output_conversion" and check["passed"] is False
        for check in payload["checkReport"]["checks"]
    )
    pending = pcb_tools._transport.pop_pending_pcb_fields("sess-pcb-reroute-no-txt")
    assert pending["rerouteResult"]["drcPassed"] is True
    assert "routedLayoutTxtFilePath" not in pending
    assert "importLinesFilePath" not in pending
    assert pending["checkReport"]["passed"] is False
    assert "DRC 已通过，但未生成轻量增量导入文件" in pending["explanation"]


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
    assert len(generate_feedback[1]) == 1
    assert 'hard_rule_counts={"HR_DRC_SEGMENT_CROSSING":1}' in generate_feedback[1][0]
    assert "不能原样重复上一轮 patch" in generate_feedback[1][0]
    assert generate_history[0] == []
    assert generate_history[1][0]["iteration"] == 1
    assert generate_history[1][0]["kicadPatch"].startswith("(segment")
    assert generate_history[1][0]["failureSummary"] == 'hard_rule_counts={"HR_DRC_SEGMENT_CROSSING":1}'


def test_reroute_normalizes_frontend_endpoint_to_internal_pad_center():
    board_text = """
(kicad_pcb
 (net 58 Z7_SPI0_SCK)
 (module BGA400 (layer Top)
  (at 102.079044 146.772884 90)
  (fp_text reference U5 (at 0 -1))
  (pad A5 smd circle (at 7.599934 4.400042) (size 0.4064 0.4064) (layers Top F.Paste F.Mask) (net 58 Z7_SPI0_SCK))
 )
)
"""
    local_context = {
        "missingRoutes": [
            {
                "net_name": "Z7_SPI0_SCK",
                "start": {"component": "U5", "pad": "A5", "layer": "Top", "x": 558.23, "y": 132.79},
                "end": {"layer": "Top", "x": 558.23, "y": -6.76},
            }
        ]
    }

    normalized, stats = pcb_tools._normalize_reroute_local_context_coordinates(
        local_context,
        board_text=board_text,
        nets=["Z7_SPI0_SCK"],
        dropped_objects=[],
    )

    route = normalized["missingRoutes"][0]
    assert stats["normalized"] is True
    assert route["start"]["coordinateSource"] == "internal_pad_center"
    assert route["start"]["x"] == pytest.approx(106.479086, abs=1e-6)
    assert route["start"]["y"] == pytest.approx(139.17295, abs=1e-6)
    assert route["frontendStart"]["x"] == 558.23
    assert route["end"]["coordinateSource"] == "frontend_txt_dbu_to_internal_kicad"
    assert route["end"]["x"] == pytest.approx(106.479086, abs=1e-6)
    assert route["end"]["y"] == pytest.approx(135.62838, abs=1e-6)


def test_reroute_local_drc_policy_ignores_unrelated_global_pad_issues():
    attempt = pcb_reroute_drc.RerouteDrcAttempt(
        iteration=1,
        passed=False,
        drc_result={
            "ok": True,
            "pass": False,
            "artifacts": {
                "issues": [
                    {
                        "rule": "HR_CONNECT_PAD_NOT_ESCAPED",
                        "message": "BGA pad U5.B13 on net PS_MIO50_501 has no initial escape connection.",
                        "severity": "ERROR",
                    }
                ]
            },
        },
    )

    passed, summary, detail = pcb_tools._local_reroute_drc_passes(attempt, selected_nets=["Z7_SPI0_SCK"])

    assert passed is True
    assert "ignored 1 inherited" in summary
    assert detail["blockingIssueCount"] == 0
    assert detail["inheritedIssueCount"] == 1


def test_reroute_local_drc_policy_blocks_selected_net_issue():
    attempt = pcb_reroute_drc.RerouteDrcAttempt(
        iteration=1,
        passed=False,
        drc_result={
            "ok": True,
            "pass": False,
            "artifacts": {
                "issues": [
                    {
                        "rule": "HR_CONNECT_PAD_NOT_ESCAPED",
                        "message": "BGA pad U5.A5 on net Z7_SPI0_SCK has no initial escape connection.",
                        "severity": "ERROR",
                    }
                ]
            },
        },
    )

    passed, summary, detail = pcb_tools._local_reroute_drc_passes(attempt, selected_nets=["Z7_SPI0_SCK"])

    assert passed is False
    assert "selected net local DRC failed" in summary
    assert detail["blockingIssueCount"] == 1


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
        filled_path = tmp_path / f"filled_iter{kwargs['iteration']}.kicad_pcb"
        filled_path.write_text("(kicad_pcb\n)\n", encoding="utf-8")
        return pcb_reroute_drc.RerouteDrcAttempt(
            iteration=kwargs["iteration"],
            passed=False,
            filled_board_data_file_path=str(filled_path),
            failure_summary=f"iteration {kwargs['iteration']} failed",
        )

    def _fake_convert(**kwargs):
        assert kwargs.get("output_subdir") == "failed_txt"
        txt_dir = tmp_path / "failed_txt"
        txt_dir.mkdir(parents=True, exist_ok=True)
        txt_path = txt_dir / "filled_iter2.txt"
        txt_path.write_text("(layout failed)", encoding="utf-8")
        return str(txt_path), []

    monkeypatch.setattr(pcb_tools, "_generate_reroute_with_model", _fake_generate)
    monkeypatch.setattr(pcb_reroute_drc, "validate_kicad_patch_with_drc", _fake_validate)
    monkeypatch.setattr(pcb_tools, "_convert_internal_kicad_to_public_txt", _fake_convert)

    result = pcb_tools.reroute(json.dumps({"maxDrcIterations": 2}, ensure_ascii=False), session_id="sess-pcb-reroute-drc-fail")
    payload = json.loads(result)

    assert payload["rerouteResult"]["drcPassed"] is False
    assert "routedLayoutTxtFilePath" not in payload["rerouteResult"]
    assert "routedLayoutTxtFilePath" not in payload
    assert "importLinesFilePath" not in payload
    assert Path(payload["rerouteResult"]["drcFailedLayoutTxtFilePath"]).parent.name == "failed_txt"
    assert "routedBoardDataFilePath" not in payload["rerouteResult"]
    assert payload["rerouteResult"]["drcFailureReasons"] == ["iteration 1 failed", "iteration 2 failed"]
    assert ".kicad_pcb" not in json.dumps(payload, ensure_ascii=False)
    assert payload["checkReport"]["passed"] is False
    assert "DRC 分析" in payload["content"]
    assert "DRC 状态: 未通过" in payload["content"]
    assert "iteration 2 failed" in payload["content"]
    assert "txt 输出: 未生成" in payload["content"]
    assert "失败回填txt: 已保存" in payload["content"]
    assert "importLines: 不允许" in payload["content"]
    assert "Explain 模型可解释性报告" in payload["content"]
