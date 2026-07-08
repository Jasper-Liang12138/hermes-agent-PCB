import asyncio
from pathlib import Path

from pcb_agent_langgraph.agent import PCBLangGraphAgent
from pcb_agent_langgraph.planner.planner import PCBPlanner
from pcb_agent_langgraph.tools.external import AnalysisTool, ExternalProgramTool
from pcb_agent_langgraph.utils.config import load_config
from pcb_agent_langgraph.websocket.protocol import agent_message


# ====== 功能：验证 rule planner fanout 场景。 ======
def test_rule_planner_fanout():
    plan = PCBPlanner(use_model=False).plan({"user_input": "帮我重新逃逸", "workflow_state": "idle", "intermediate_cache": {}})
    assert plan["intent"] == "global_fanout"
    assert plan["workflow"] == "pcb_escape_flow"
    assert plan["tool_calls"][0]["name"] == "getProjectData"


# ====== 功能：验证 rule planner reroute 场景。 ======
def test_rule_planner_reroute():
    plan = PCBPlanner(use_model=False).plan({"user_input": "把 DDR 那几根线拆线重布", "workflow_state": "idle", "intermediate_cache": {}})
    assert plan["intent"] == "reroute"
    assert plan["workflow"] == "pcb_reroute_flow"
    assert plan["tool_calls"][0]["name"] == "deleteTracesForRerouting"


# ====== 功能：验证 fanout entities u5 width spacing 场景。 ======
def test_fanout_entities_u5_width_spacing():
    plan = PCBPlanner(use_model=False).plan({"user_input": "对 U5 进行逃逸布线，线宽3mil，线距4.5mil，用135规则", "workflow_state": "idle", "intermediate_cache": {"projectData": "board.txt"}})
    args = plan["tool_calls"][0]["arguments"]
    assert plan["tool_calls"][0]["name"] == "layer_assign"
    assert args["selectedBGA"] == "U5"
    assert args["constraints"]["LineWidth"] == 3
    assert args["constraints"]["LineSpacing"] == 4.5
    assert args["routerType"] == "rule_135"


# ====== 功能：验证 fanout entities multiple bgas arc 场景。 ======
def test_fanout_entities_multiple_bgas_arc():
    plan = PCBPlanner(use_model=False).plan({"user_input": "给 U5 和 U7 做 arc 逃逸，width=3.5 spacing=4", "workflow_state": "idle", "intermediate_cache": {"projectData": "board.txt"}})
    args = plan["tool_calls"][0]["arguments"]
    assert plan["tool_calls"][0]["name"] == "layer_assign"
    assert args["selectedBGA"] == "U5"
    assert args["targetBGAs"] == ["U5", "U7"]
    assert args["constraints"] == {"LineWidth": 3.5, "LineSpacing": 4}
    assert args["routerType"] == "rule_arc"


# ====== 功能：验证 config explicit path precedence 场景。 ======
def test_config_explicit_path_precedence(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text("[drc]\nenabled = 1\n", encoding="utf-8")
    config = load_config(cfg)
    assert config.source_config == cfg
    assert config.drc.enabled is True


# ====== 功能：验证 drc disabled fails clearly 场景。 ======
def test_drc_disabled_fails_clearly():
    config = load_config("missing-config.ini")
    result = asyncio.run(AnalysisTool("drc_check", config).ainvoke({}, {}))
    assert result["status"] == "failed"
    assert "DRC is disabled" in result["reason"]


# ====== 功能：验证 drc configured missing input fails after tool resolution 场景。 ======
def test_drc_configured_missing_input_fails_after_tool_resolution(tmp_path):
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[drc]\n"
        "enabled = 1\n"
        f"tool_path = {Path('tools/pcb_reroute_drc.py')}\n"
        f"eval_root = {Path('vendor/AI-PCB-Eval')}\n",
        encoding="utf-8",
    )
    config = load_config(cfg)
    result = asyncio.run(AnalysisTool("drc_check", config).ainvoke({}, {}))
    assert result["status"] == "failed"
    assert result["reason"] == "missing original board data for DRC"


# ====== 功能：验证 explain and help config defaults present 场景。 ======
def test_explain_and_help_config_defaults_present():
    config = load_config("missing-config.ini")
    assert config.explain_model.code_dir.endswith("explain_model\\explain_code")
    assert config.explain_model.checkpoint_path.endswith("explain_model\\model\\best.pt")
    assert config.router.pcbrouter_bin.endswith("tools\\reroute_helper\\pcbrouter.exe")
    assert config.reroute_help.max_drc_failures == 3


# ====== 功能：验证 reroute drc failure triggers help planner 场景。 ======
def test_reroute_drc_failure_triggers_help_planner():
    cache = {
        "deleteTracesResult": {"status": "ok", "selectedNets": ["DDR_DQ0"]},
        "rerouteInput": {"status": "ok"},
        "rerouteContext": {"status": "ok"},
        "rerouteResult": {"status": "ok", "importLinesFilePath": "missing.kicad_pcb"},
        "importLinesResult": {"status": "ok"},
        "drcResult": {"status": "failed", "reason": "violation"},
        "rerouteDrcFailureCount": 3,
    }
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续", "workflow_state": "error", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["tool_calls"][0]["name"] == "help_planner"
# ====== 功能：验证真实模型只返回参数时仍由 LangGraph 修复为可执行 fanout 计划。 ======
def test_model_parameter_only_output_repairs_to_fanout_plan():
    from pcb_agent_langgraph.models.pcb_model import ModelResult

    class FakeModel:
        # ====== 功能：模拟 [reroute-model] 只返回线宽线距参数的旧格式响应。 ======
        def complete(self, messages, temperature=0.0):
            return ModelResult(content='{"LineWidth":4,"LineSpacing":3}', raw={}, elapsed_ms=1.0, usage={})

    state = {"user_input": "对 U22 进行135逃逸布线，线宽4，线距3", "workflow_state": "idle", "intermediate_cache": {}}
    plan = PCBPlanner(model=FakeModel(), use_model=True, require_model=True).plan(state)
    assert plan["planner_source"] == "model"
    assert plan["intent"] == "global_fanout"
    assert plan["tool_calls"][0]["name"] == "getProjectData"
    assert plan["tool_calls"][0]["arguments"]["selectedBGA"] == "U22"
    assert plan["tool_calls"][0]["arguments"]["constraints"] == {"LineWidth": 4, "LineSpacing": 3}
# ====== 功能：验证 reroute 删除走线后先准备统一 KiCad/CSV 输入。 ======
def test_reroute_after_delete_prepares_inputs_before_context():
    cache = {"deleteTracesResult": {"status": "ok", "selectedNets": ["DDR_DQ0"], "projectData": {"boardData": "(kicad_pcb (segment (net 1)))"}}}
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续 reroute", "workflow_state": "report", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["tool_calls"][0]["name"] == "prepare_reroute_inputs"


# ====== 功能：验证 KiCad reroute 上下文压缩会优先检索目标网络片段。 ======
def test_reroute_context_retrieves_target_net_chunk():
    from pcb_agent_langgraph.tools.reroute_context import build_reroute_context

    board_text = "NET USB_DP unrelated\n" * 200 + "(segment (start 1 1) (end 2 2) (layer Top) (net DDR_DQ0))\n" + "NET HDMI unrelated\n" * 200
    result = build_reroute_context(board_text=board_text, task_description="repair DDR_DQ0", nets=["DDR_DQ0"], chunk_chars=512, overlap_chars=64, retrieve_k=1)
    assert result["status"] == "ok"
    assert "DDR_DQ0" in result["contextText"]
    assert result["stats"]["retrievedSegmentCount"] == 1


# ====== 功能：验证 reroute 输入准备可直接使用 KiCad 并生成 net-only CSV。 ======
def test_prepare_reroute_inputs_accepts_kicad_and_writes_net_csv(tmp_path):
    config = load_config("missing-config.ini")
    tool = ExternalProgramTool("prepare_reroute_inputs", config)
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("(kicad_pcb (version 20240108) (layers (0 \"F.Cu\" signal)))\n", encoding="utf-8")
    context = {
        "session_id": "s1",
        "deleteTracesResult": {"projectData": str(board_path), "missing_routes": [{"net_name": "DDR_DQ0"}]},
    }
    result = asyncio.run(tool.ainvoke({}, context))
    assert result["status"] == "ok"
    assert result["kicadBoardPath"].endswith(".kicad_pcb")
    assert result["selectedNets"] == ["DDR_DQ0"]
    assert result["csvMode"] == "net_only"
    assert Path(result["localRouteCsvPath"]).read_text(encoding="utf-8").splitlines() == ["net", "DDR_DQ0"]


# ====== 功能：验证 reroute 输入准备在存在固定层时生成 route_layer CSV。 ======
def test_prepare_reroute_inputs_writes_fixed_route_layer_csv(tmp_path):
    config = load_config("missing-config.ini")
    tool = ExternalProgramTool("prepare_reroute_inputs", config)
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("(kicad_pcb (version 20240108) (layers (0 \"F.Cu\" signal) (31 \"B.Cu\" signal)))\n", encoding="utf-8")
    context = {
        "session_id": "s2",
        "deleteTracesResult": {"projectData": str(board_path), "missing_routes": [{"net_name": "DDR_DQ1", "route_layer": "Top"}]},
    }
    result = asyncio.run(tool.ainvoke({}, context))
    assert result["status"] == "ok"
    assert result["csvMode"] == "fixed_route_layer"
    assert Path(result["localRouteCsvPath"]).read_text(encoding="utf-8").splitlines() == ["net,route_layer", "DDR_DQ1,F.Cu"]


# ====== 功能：验证 reroute 输入转换失败时返回前端友好的格式转换错误。
def test_prepare_reroute_inputs_conversion_error_hides_internal_format_name(monkeypatch, tmp_path):
    config = load_config("missing-config.ini")
    tool = ExternalProgramTool("prepare_reroute_inputs", config)

    def fail_convert(*_args, **_kwargs):
        raise RuntimeError("converter exploded")

    monkeypatch.setattr("pcb_agent_langgraph.tools.external._ensure_reroute_kicad_input", fail_convert)
    result = asyncio.run(tool.ainvoke({}, {"session_id": "s3", "deleteTracesResult": {"projectData": {"boardData": "(layout broken)"}}}))

    assert result["status"] == "failed"
    assert "格式转换错误" in result["reason"]
    assert "KiCad" not in result["reason"]
    assert "kicad" not in result["reason"].lower()
# ====== 功能：验证 DRC 和可解释性结果会被组装成 Markdown 报告。 ======
def test_markdown_report_contains_drc_and_explainability():
    from pcb_agent_langgraph.reports.markdown import build_markdown_report

    cache = {
        "drcResult": {"status": "ok", "passed": True, "score": 1.0, "tool_path": "tools/pcb_reroute_drc.py"},
        "explainabilityReport": {"status": "ok", "report": "模型判断布线结果可接受。"},
        "fanout_routeResult": {"routedLayoutTxtFilePath": "line.out"},
        "importLinesResult": {"status": "ok"},
    }
    report = build_markdown_report("global_fanout", cache)
    assert report["drcPassed"] is True
    assert "# PCB Fanout Report" in report["markdown"]
    assert "tools/pcb_reroute_drc.py" in report["markdown"]
    assert "模型判断布线结果可接受" in report["markdown"]


# ====== 功能：验证发给前端的 Agent 消息带 Markdown 报告字段。 ======
def test_agent_message_carries_markdown_report():
    from pcb_agent_langgraph.websocket.protocol import agent_message

    message = agent_message("s1", "p1", "正文", markdownReport="# Report", reportPayload={"drcPassed": True})
    assert message["body"]["markdownReport"] == "# Report"
    assert message["body"]["reportPayload"] == {"drcPassed": True}


# ====== 功能：验证 fanout 获取项目后未指定 BGA 时先调用脚本提取。 ======
def test_fanout_without_selected_bga_extracts_bga_after_project_data():
    state = {
        "user_input": "帮我做逃逸布线",
        "workflow_state": "idle",
        "workflow_id": "pcb_escape_flow",
        "task_type": "global_fanout",
        "intermediate_cache": {"projectData": "F:/demo/export.txt"},
    }
    plan = PCBPlanner(use_model=False).plan(state)
    assert plan["tool_calls"][0]["name"] == "pcb_extra_bga"


# ====== 功能：验证 BGA 脚本唯一候选也必须先展示 selection。 ======
def test_fanout_single_bga_candidate_returns_selection():
    cache = {"projectData": "F:/demo/export.txt", "bgaCandidates": [{"refdes": "U5", "pincount": 450}]}
    state = {"user_input": "帮我做逃逸布线", "workflow_state": "idle", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": cache}
    plan = PCBPlanner(use_model=False).plan(state)
    assert plan["tool_calls"] == []
    assert plan["action"] == "select_bga"
    assert plan["selection"][0]["label"] == "U5"

# ====== 功能：验证 reroute 上下文完成后直接进入 VSEA 主循环。 ======
def test_reroute_after_context_calls_vsea_reroute_loop_directly():
    cache = {"deleteTracesResult": {"status": "ok"}, "rerouteInput": {"status": "ok", "selectedNets": ["GND"]}, "rerouteContext": {"status": "ok", "selectedNets": ["GND"]}}
    state = {"user_input": "继续", "workflow_state": "report", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache}
    plan = PCBPlanner(use_model=False).plan(state)
    assert plan["action"] == "reroute_loop"
    assert plan["tool_calls"][0]["name"] == "reroute_loop"
# ====== 功能：验证唯一 BGA 自动进入参数生成后停在 fanoutParams 确认。 ======
def test_fanout_single_bga_stops_for_fanout_params_review():
    cache = {
        "projectData": "board.txt",
        "bgaCandidates": [{"refdes": "U5", "pincount": 400}],
        "layerAssignResult": {"status": "ok"},
        "escapeOrderResult": {"status": "ok"},
        "fanoutParams": {"selectedBGA": "U5", "routerType": "135"},
    }
    plan = PCBPlanner(use_model=False).plan({"user_input": "帮我做逃逸布线", "workflow_state": "layer_assign_escape_order", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": cache})
    assert plan["tool_calls"] == []
    assert plan["action"] == "fanout_params_review"


# ====== 功能：验证 fanout 参数确认后才调用真实布线器。 ======
def test_fanout_confirm_params_calls_router():
    cache = {
        "projectData": "board.txt",
        "fanoutEntities": {"selectedBGA": "U5", "routerType": "rule_135"},
        "layerAssignResult": {"status": "ok"},
        "escapeOrderResult": {"status": "ok"},
        "fanoutParams": {"selectedBGA": "U5", "routerType": "135"},
    }
    plan = PCBPlanner(use_model=False).plan({"user_input": "确认", "workflow_state": "param_review", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": cache})
    assert plan["tool_calls"][0]["name"] == "fanout_route"


# ====== 功能：验证 fanout 路由完成后直接调用 importLines，由前端审批确认/拒绝。 ======
def test_fanout_route_result_calls_import_lines_directly():
    cache = {
        "projectData": "board.txt",
        "fanoutEntities": {"selectedBGA": "U5", "routerType": "rule_135"},
        "layerAssignResult": {"status": "ok"},
        "escapeOrderResult": {"status": "ok"},
        "fanout_routeResult": {"status": "ok", "importLinesFilePath": "line.out"},
    }
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续", "workflow_state": "routing", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": cache})
    assert plan["action"] == "import"
    assert plan["tool_calls"][0]["name"] == "importLines"
    assert plan["tool_calls"][0]["arguments"]["filePath"] == "line.out"

# ====== 功能：验证 reroute 上下文压缩后不再等待确认。 ======
def test_reroute_context_calls_reroute_without_confirm():
    cache = {"deleteTracesResult": {"status": "ok"}, "rerouteInput": {"status": "ok", "selectedNets": ["GND"]}, "rerouteContext": {"status": "ok", "selectedNets": ["GND"]}}
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续", "workflow_state": "rip_up", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["action"] == "reroute_loop"
    assert plan["tool_calls"][0]["name"] == "reroute_loop"


# ====== 功能：验证 reroute 成功写缓存时不会因日志摘要缺少 json 导入而中断。 ======
def test_reroute_cache_update_summarizes_result_without_name_error():
    from pcb_agent_langgraph.graph.nodes import _update_cache_from_tool

    cache = {"selectedNets": ["PHY1_TX_CLK"]}
    result = {"status": "ok", "attempt": 1, "report": {"message": "done"}, "workDir": "work/reroute/attempt_1"}
    _update_cache_from_tool(cache, "reroute", result)
    assert cache["rerouteResult"] == result
    assert cache["rerouteAttemptCount"] == 1


# ====== 功能：验证 reroute 进度消息携带模型调用证据，避免“已完成”过于含糊。 ======
def test_reroute_progress_suffix_includes_model_evidence():
    from pcb_agent_langgraph.graph.nodes import _tool_progress_suffix

    suffix = _tool_progress_suffix(
        {
            "ok": True,
            "elapsed_ms": 1300.0,
            "result": {"status": "ok", "tool": "reroute", "elapsedMs": 900.0, "modelOutputChars": 42, "workDir": "work/reroute/attempt_1"},
        }
    )
    assert "工具耗时 1.3s" in suffix
    assert "模型耗时 0.9s" in suffix
    assert "模型输出 42 字符" in suffix
    assert "workDir=work/reroute/attempt_1" in suffix


# ====== 功能：验证 DRC 通过后传给 importLines 的是轻量增量 line.out，不是完整 KiCad 文件。 ======
def test_drc_pass_generates_reroute_incremental_import_file(tmp_path):
    from pcb_agent_langgraph.graph.nodes import _update_cache_from_tool

    work_dir = tmp_path / "attempt_1"
    work_dir.mkdir()
    routed_kicad = tmp_path / "routed.kicad_pcb"
    routed_kicad.write_text("(kicad_pcb routed)", encoding="utf-8")
    cache = {
        "rerouteInput": {
            "kicadBoardText": "(kicad_pcb (net 58 Z7_SPI0_SCK))",
            "missingRoutes": [{"net_name": "Z7_SPI0_SCK", "start": {"layer": "Top", "x": 106.479086, "y": 139.17295}, "end": {"layer": "Top", "x": 106.479086, "y": 135.62838}}],
        },
        "missingRoutes": [{"net_name": "Z7_SPI0_SCK", "start": {"layer": "Top", "x": 106.479086, "y": 139.17295}, "end": {"layer": "Top", "x": 106.479086, "y": 135.62838}}],
        "rerouteResult": {
            "status": "ok",
            "workDir": str(work_dir),
            "modelOutputText": "\n".join(
                [
                    "(segment (start 106.480000 139.170000) (end 106.480000 135.630000) (width 0.152400) (layer Top) (net 58))",
                    "(segment (start 106.480000 135.630000) (end 106.480000 132.790000) (width 0.152400) (layer Top) (net 58))",
                ]
            ),
        },
    }
    _update_cache_from_tool(cache, "drc_check", {"status": "ok", "passed": True, "detail": {"filled_board_data_file_path": str(routed_kicad)}})
    import_path = Path(cache["rerouteResult"]["importLinesFilePath"])
    assert import_path.name.endswith("_reroute_line.out")
    assert import_path.suffix == ".out"
    assert cache["rerouteResult"]["routedKicadFilePath"] == str(routed_kicad)
    assert cache["rerouteResult"]["importLinesFilePath"] != str(routed_kicad)
    assert import_path.read_text(encoding="utf-8").startswith("TOP!LINE!0!Z7_SPI0_SCK!")


# ====== 功能：验证 helper-router 的完整板输出会 diff 成前端可导入的 line.out。
def test_helper_router_routed_board_diff_generates_incremental_line_out(tmp_path):
    from pcb_agent_langgraph.tools.external import _write_helper_router_incremental_import_file

    original = """
    (kicad_pcb
      (net 1 DDR_DQ0)
      (net 2 DDR_DQ1)
      (segment (start 10 20) (end 11 20) (width 0.1524) (layer F.Cu) (net 1))
    )
    """
    routed_path = tmp_path / "routed.kicad_pcb"
    routed_path.write_text(
        """
        (kicad_pcb
          (net 1 DDR_DQ0)
          (net 2 DDR_DQ1)
          (segment (start 10 20) (end 11 20) (width 0.1524) (layer F.Cu) (net 1))
          (segment (start 11 20) (end 12 20) (width 0.1524) (layer F.Cu) (net 1))
          (segment (start 30 40) (end 31 40) (width 0.1524) (layer F.Cu) (net 2))
        )
        """,
        encoding="utf-8",
    )

    import_path, notes = _write_helper_router_incremental_import_file(
        original_board_text=original,
        routed_board_path=routed_path,
        selected_nets=["DDR_DQ0"],
        work_dir=tmp_path,
    )

    assert import_path
    assert Path(import_path).name == "helper_router_reroute_line.out"
    text = Path(import_path).read_text(encoding="utf-8")
    assert text.count("!LINE!") == 1
    assert "DDR_DQ0" in text
    assert "DDR_DQ1" not in text
    assert any("generated_helper_router_incremental_line_out" in note for note in notes)


# ====== 功能：验证 VSEA reroute_loop 成功后生成可导入增量 line.out 并信任 hard DRC。 ======
def test_reroute_loop_vsea_success_generates_incremental_import(monkeypatch, tmp_path):
    from pcb_agent_langgraph.tools import external

    class FakeSchemas:
        class RerouteInput:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

    class FakeResult:
        def __init__(self, routed_path: Path):
            self.routed_path = routed_path

        def to_dict(self):
            return {
                "success": True,
                "status": "passed",
                "routing_patch": "(segment (start 11 20) (end 12 20) (width 0.1524) (layer F.Cu) (net 1))",
                "completed_kicad_path": str(self.routed_path),
                "drc_report": {"violations": 0},
            }

    class FakeAgent:
        @classmethod
        def from_env(cls):
            return cls()

        def run(self, request):
            assert request.kwargs["routing_task_prompt"]
            routed_path = tmp_path / "completed.kicad_pcb"
            routed_path.write_text(
                """
                (kicad_pcb
                  (net 1 DDR_DQ0)
                  (segment (start 10 20) (end 11 20) (width 0.1524) (layer F.Cu) (net 1))
                  (segment (start 11 20) (end 12 20) (width 0.1524) (layer F.Cu) (net 1))
                )
                """,
                encoding="utf-8",
            )
            return FakeResult(routed_path)

    class FakeAgentModule:
        RerouteAgent = FakeAgent

    def fake_import(_pipeline_root, module_name):
        return FakeSchemas if module_name.endswith(".schemas") else FakeAgentModule

    pipeline_root = tmp_path / "VSEA-PCB"
    pipeline_root.mkdir()
    config = load_config("missing-config.ini")
    config.reroute_loop.pipeline_root = str(pipeline_root)
    monkeypatch.setattr(external, "_import_vsea_module", fake_import)
    tool = ExternalProgramTool("reroute_loop", config)
    context = {
        "session_id": "s1",
        "rerouteInput": {
            "kicadBoardText": """
            (kicad_pcb
              (net 1 DDR_DQ0)
              (segment (start 10 20) (end 11 20) (width 0.1524) (layer F.Cu) (net 1))
            )
            """,
            "kicadBoardPath": str(tmp_path / "input.kicad_pcb"),
            "selectedNets": ["DDR_DQ0"],
            "missingRoutes": [{"net_name": "DDR_DQ0"}],
        },
        "rerouteContext": {"contextText": "DDR_DQ0 local area"},
    }

    result = asyncio.run(tool.ainvoke({}, context))

    assert result["status"] == "ok"
    assert result["source"] == "vsea_reroute_pipeline"
    assert result["drcResult"]["passed"] is True
    import_path = Path(result["importLinesFilePath"])
    assert import_path.suffix == ".out"
    assert "DDR_DQ0" in import_path.read_text(encoding="utf-8")


# ====== 功能：验证 VSEA reroute_loop 失败不会给前端导入路径。 ======
def test_reroute_loop_vsea_failure_has_no_import_path(monkeypatch, tmp_path):
    from pcb_agent_langgraph.tools import external

    class FakeSchemas:
        class RerouteInput:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

    class FakeResult:
        def to_dict(self):
            return {"success": False, "status": "drc_failed", "error": "violation"}

    class FakeAgent:
        @classmethod
        def from_env(cls):
            return cls()

        def run(self, _request):
            return FakeResult()

    class FakeAgentModule:
        RerouteAgent = FakeAgent

    pipeline_root = tmp_path / "VSEA-PCB"
    pipeline_root.mkdir()
    config = load_config("missing-config.ini")
    config.reroute_loop.pipeline_root = str(pipeline_root)
    monkeypatch.setattr(external, "_import_vsea_module", lambda _root, name: FakeSchemas if name.endswith(".schemas") else FakeAgentModule)
    result = asyncio.run(
        ExternalProgramTool("reroute_loop", config).ainvoke(
            {},
            {
                "session_id": "s2",
                "rerouteInput": {"kicadBoardText": "(kicad_pcb (version 20240108) (net 1 DDR_DQ0))", "selectedNets": ["DDR_DQ0"]},
                "rerouteContext": {"contextText": "DDR_DQ0"},
            },
        )
    )

    assert result["status"] == "failed"
    assert result["failureType"] == "drc_failed"
    assert "importLinesFilePath" not in result


# ====== 功能：验证 VSEA 只返回 completed_kicad 文本时会落盘后生成增量导入文件。 ======
def test_reroute_loop_vsea_completed_text_generates_import(monkeypatch, tmp_path):
    from pcb_agent_langgraph.tools import external

    class FakeSchemas:
        class RerouteInput:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

    class FakeResult:
        def to_dict(self):
            return {
                "success": True,
                "status": "passed",
                "routing_patch": "(segment (start 11 20) (end 12 20) (width 0.1524) (layer F.Cu) (net 1))",
                "completed_kicad": """
                (kicad_pcb
                  (net 1 DDR_DQ0)
                  (segment (start 10 20) (end 11 20) (width 0.1524) (layer F.Cu) (net 1))
                  (segment (start 11 20) (end 12 20) (width 0.1524) (layer F.Cu) (net 1))
                )
                """,
                "drc_report": {"violations": 0},
            }

    class FakeAgent:
        @classmethod
        def from_env(cls):
            return cls()

        def run(self, _request):
            return FakeResult()

    class FakeAgentModule:
        RerouteAgent = FakeAgent

    pipeline_root = tmp_path / "VSEA-PCB"
    pipeline_root.mkdir()
    config = load_config("missing-config.ini")
    config.reroute_loop.pipeline_root = str(pipeline_root)
    monkeypatch.setattr(external, "_import_vsea_module", lambda _root, name: FakeSchemas if name.endswith(".schemas") else FakeAgentModule)
    result = asyncio.run(
        ExternalProgramTool("reroute_loop", config).ainvoke(
            {},
            {
                "session_id": "s4",
                "rerouteInput": {
                    "kicadBoardText": """
                    (kicad_pcb
                      (net 1 DDR_DQ0)
                      (segment (start 10 20) (end 11 20) (width 0.1524) (layer F.Cu) (net 1))
                    )
                    """,
                    "selectedNets": ["DDR_DQ0"],
                },
                "rerouteContext": {"contextText": "DDR_DQ0"},
            },
        )
    )
    assert result["status"] == "ok"
    assert Path(result["routedKicadFilePath"]).name == "completed_from_vsea.kicad_pcb"
    assert Path(result["importLinesFilePath"]).suffix == ".out"


# ====== 功能：验证 VSEA pipeline 路径缺失时明确失败。 ======
def test_reroute_loop_missing_pipeline_root_fails(tmp_path):
    config = load_config("missing-config.ini")
    config.reroute_loop.pipeline_root = str(tmp_path / "missing-vsea")
    result = asyncio.run(
        ExternalProgramTool("reroute_loop", config).ainvoke(
            {},
            {
                "session_id": "s3",
                "rerouteInput": {"kicadBoardText": "(kicad_pcb (version 20240108) (net 1 DDR_DQ0))", "selectedNets": ["DDR_DQ0"]},
                "rerouteContext": {"contextText": "DDR_DQ0"},
            },
        )
    )
    assert result["status"] == "failed"
    assert result["failureType"] == "pipeline_unavailable"
# ====== 功能：验证普通消息不携带空 selection。 ======
def test_agent_message_omits_none_selection():
    message = agent_message("s1", "p1", "正文", selection=None)
    assert "selection" not in message["body"]

# ====== 功能：验证 BGA 选择后先停在 router 选择，而不是直接生成层分配。 ======
def test_fanout_bga_selection_waits_for_router_choice():
    cache = PCBLangGraphAgent._cache_for_turn({"projectData": "board.txt", "bgaCandidates": [{"refdes": "U5"}]}, "U5", "select_bga")
    plan = PCBPlanner(use_model=False).plan({"user_input": "U5", "workflow_state": "select_bga", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": cache})
    assert plan["tool_calls"] == []
    assert plan["action"] == "router_type_prompt"


# ====== 功能：验证选择 135 后才调用 layer_assign，并携带 rule_135。 ======
def test_fanout_router_choice_135_calls_layer_assign():
    cache = {"projectData": "board.txt", "fanoutEntities": {"selectedBGA": "U5", "targetBGAs": ["U5"]}}
    cache = PCBLangGraphAgent._cache_for_turn(cache, "135", "wait_router_type")
    plan = PCBPlanner(use_model=False).plan({"user_input": "135", "workflow_state": "wait_router_type", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": cache})
    assert plan["tool_calls"][0]["name"] == "layer_assign"
    assert plan["tool_calls"][0]["arguments"]["routerType"] == "rule_135"


# ====== 功能：验证选择 arc 后调用 layer_assign，并携带 rule_arc。 ======
def test_fanout_router_choice_arc_calls_layer_assign():
    cache = {"projectData": "board.txt", "fanoutEntities": {"selectedBGA": "U5", "targetBGAs": ["U5"]}}
    cache = PCBLangGraphAgent._cache_for_turn(cache, "arc", "wait_router_type")
    plan = PCBPlanner(use_model=False).plan({"user_input": "arc", "workflow_state": "wait_router_type", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": cache})
    assert plan["tool_calls"][0]["name"] == "layer_assign"
    assert plan["tool_calls"][0]["arguments"]["routerType"] == "rule_arc"


# ====== 功能：验证前端参数 JSON 会确认 fanoutParams 并进入布线。 ======
def test_fanout_param_review_json_calls_router_and_keeps_real_order_lines():
    cache = {
        "projectData": "board.txt",
        "fanoutEntities": {"selectedBGA": "U5", "routerType": "rule_135"},
        "layerAssignResult": {"status": "ok"},
        "escapeOrderResult": {"status": "ok"},
        "fanoutParams": {"selectedBGA": "U5", "routerType": "rule_135", "constraints": {"LineWidth": 4}, "orderLines": ["DDR_D0 Top 1"]},
    }
    payload = '{"orderLines":[{"net":"","layer":"","order":1}],"constraints":{"LineSpacing":3,"LineWidth":5}}'
    cache = PCBLangGraphAgent._cache_for_turn(cache, payload, "param_review")
    plan = PCBPlanner(use_model=False).plan({"user_input": payload, "workflow_state": "param_review", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": cache})
    assert plan["tool_calls"][0]["name"] == "fanout_route"
    assert cache["fanoutParams"]["orderLines"] == ["DDR_D0 Top 1"]
    assert cache["fanoutParams"]["constraints"] == {"LineWidth": 5, "LineSpacing": 3}
# ====== 功能：验证模型越级 layer_assign 会被 LangGraph 合法转移改写为 BGA 提取。 ======
def test_model_illegal_layer_assign_rewritten_to_extract_bga():
    from pcb_agent_langgraph.models.pcb_model import ModelResult

    class FakeModel:
        def complete(self, messages, temperature=0.0):
            return ModelResult(content='{"intent":"global_fanout","workflow":"pcb_escape_flow","tool_calls":[{"name":"layer_assign","arguments":{"selectedBGA":"U5","routerType":"rule_135"}}]}', raw={}, elapsed_ms=1.0, usage={})

    state = {"user_input": "请帮我做逃逸布线", "workflow_state": "idle", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": {"projectData": "F:/demo/export.txt"}}
    plan = PCBPlanner(model=FakeModel(), use_model=True, require_model=True).plan(state)
    assert plan["tool_calls"][0]["name"] == "pcb_extra_bga"
    assert "outside legal LangGraph transition" in plan["reason"]


# ====== 功能：验证模型在未选 router 时越级 layer_assign 会被改写为 router 选择停点。 ======
def test_model_layer_assign_before_router_rewritten_to_router_prompt():
    from pcb_agent_langgraph.models.pcb_model import ModelResult

    class FakeModel:
        def complete(self, messages, temperature=0.0):
            return ModelResult(content='{"intent":"global_fanout","workflow":"pcb_escape_flow","tool_calls":[{"name":"layer_assign","arguments":{"selectedBGA":"U5"}}]}', raw={}, elapsed_ms=1.0, usage={})

    cache = {"projectData": "board.txt", "fanoutEntities": {"selectedBGA": "U5", "targetBGAs": ["U5"]}}
    state = {"user_input": "U5", "workflow_state": "select_bga", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": cache}
    plan = PCBPlanner(model=FakeModel(), use_model=True, require_model=True).plan(state)
    assert plan["tool_calls"] == []
    assert plan["action"] == "router_type_prompt"


# ====== 功能：验证已有 rerouteContext 时模型重复 compress 会被改写为主模型重布。 ======
def test_model_repeated_compress_context_rewritten_to_reroute():
    from pcb_agent_langgraph.models.pcb_model import ModelResult

    class FakeModel:
        def complete(self, messages, temperature=0.0):
            return ModelResult(content='{"intent":"reroute","workflow":"pcb_reroute_flow","tool_calls":[{"name":"compress_reroute_context","arguments":{}}]}', raw={}, elapsed_ms=1.0, usage={})

    cache = {"deleteTracesResult": {"status": "ok"}, "rerouteInput": {"status": "ok", "selectedNets": ["Z7_SPI0_SCK"]}, "rerouteContext": {"status": "ok", "selectedNets": ["Z7_SPI0_SCK"]}}
    state = {"user_input": "继续", "workflow_state": "rip_up", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache}
    plan = PCBPlanner(model=FakeModel(), use_model=True, require_model=True).plan(state)
    assert plan["action"] == "reroute_loop"
    assert plan["tool_calls"][0]["name"] == "reroute_loop"
# ====== 功能：验证 help_planner 不会把 export.txt 伪装成 KiCad 输入。 ======
def test_help_planner_rejects_export_txt_input():
    config = load_config("missing-config.ini")
    tool = ExternalProgramTool("help_planner", config)
    context = {"session_id": "s1", "projectData": "F:/doctor/pcb/test/1/Output/demo/export.txt", "selectedNets": ["Z7_SPI0_SCK"]}
    result = asyncio.run(tool.ainvoke({}, context))
    assert result["status"] == "failed"
    assert "requires KiCad .kicad_pcb input" in result["reason"]


# ====== 功能：验证主模型 payload 使用 KiCad 输入契约，不暴露 PCB Builder txt 字段。 ======
def test_reroute_model_payload_uses_kicad_contract():
    from pcb_agent_langgraph.tools.external import _reroute_model_payload

    payload = _reroute_model_payload(
        {},
        {"rerouteContext": {"status": "ok"}, "selectedNets": ["DDR_DQ0"]},
        {"kicadBoardText": "(kicad_pcb demo)", "kicadBoardPath": "board.kicad_pcb", "missingRoutes": [{"net_name": "DDR_DQ0"}]},
        1,
    )
    assert payload["inputFormat"] == "kicad_pcb"
    assert payload["kicadBoardPreview"] == "(kicad_pcb demo)"
    assert payload["kicadBoardPath"] == "board.kicad_pcb"
    assert "projectDataPreview" not in payload


# ====== 功能：验证 DRC 失败超过耗时上限会进入 help_planner。 ======
def test_reroute_drc_failure_elapsed_limit_triggers_help_planner():
    import time

    config = load_config("missing-config.ini")
    config.reroute_help.max_elapsed_seconds = 1
    cache = {
        "deleteTracesResult": {"status": "ok"},
        "rerouteInput": {"status": "ok"},
        "rerouteContext": {"status": "ok"},
        "rerouteResult": {"status": "ok"},
        "drcResult": {"status": "failed", "passed": False},
        "rerouteDrcFailureCount": 1,
        "rerouteStartedAt": time.time() - 5,
    }
    plan = PCBPlanner(use_model=False, config=config).plan({"user_input": "继续", "workflow_state": "error", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["tool_calls"][0]["name"] == "help_planner"

# ====== 功能：验证 fanout 实体跨轮保存，U5 后续选择 135 不会丢失目标 BGA。 ======
def test_fanout_entities_persist_across_router_turn():
    cache = PCBLangGraphAgent._cache_for_turn({"projectData": "board.txt"}, "帮我进行逃逸布线，U5", "idle")
    cache = PCBLangGraphAgent._cache_for_turn(cache, "135", "wait_router_type")
    assert cache["fanoutEntities"]["selectedBGA"] == "U5"
    assert cache["fanoutEntities"]["routerType"] == "rule_135"
    plan = PCBPlanner(use_model=False).plan({"user_input": "135", "workflow_state": "wait_router_type", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": cache})
    assert plan["tool_calls"][0]["name"] == "layer_assign"
    assert plan["tool_calls"][0]["arguments"]["selectedBGA"] == "U5"


# ====== 功能：验证线宽线距会进入 fanoutEntities 并传给工具参数。 ======
def test_fanout_width_spacing_persist_to_layer_assign():
    cache = PCBLangGraphAgent._cache_for_turn({"projectData": "board.txt"}, "逃逸布线，U5，135，线宽 5 线距 4", "idle")
    plan = PCBPlanner(use_model=False).plan({"user_input": "逃逸布线，U5，135，线宽 5 线距 4", "workflow_state": "idle", "workflow_id": "pcb_escape_flow", "task_type": "global_fanout", "intermediate_cache": cache})
    args = plan["tool_calls"][0]["arguments"]
    assert args["constraints"] == {"LineWidth": 5, "LineSpacing": 4}


# ====== 功能：验证拒绝导入后再次 fanout 会清理旧拒绝状态。 ======
def test_new_fanout_clears_import_rejection_state():
    cache = {"importLinesRejected": True, "importLinesRejectedReason": "user rejected", "fanout_routeResult": {"status": "ok"}, "importLinesResult": {"status": "ok"}}
    cache = PCBLangGraphAgent._cache_for_turn(cache, "重新逃逸布线，U5", "result_review")
    assert "importLinesRejected" not in cache
    assert "importLinesRejectedReason" not in cache
    assert "fanout_routeResult" not in cache
    assert "importLinesResult" not in cache


# ====== 功能：验证旧主模型 unavailable 标记不再阻塞 VSEA reroute_loop。 ======
def test_legacy_reroute_unavailable_does_not_block_vsea_loop():
    cache = {"deleteTracesResult": {"status": "ok"}, "rerouteInput": {"status": "ok"}, "rerouteContext": {"status": "ok"}, "rerouteUnavailable": True, "rerouteUnavailableReason": "model 401"}
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续", "workflow_state": "rip_up", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["action"] == "reroute_loop"
    assert plan["tool_calls"][0]["name"] == "reroute_loop"


# ====== 功能：验证 patch fill 失败不能被 target scoped DRC 误判为通过。 ======
def test_drc_fill_failed_cannot_target_scope_pass(monkeypatch, tmp_path):
    from pcb_agent_langgraph.tools import external

    class FakeDrcModule:
        @staticmethod
        def validate_kicad_patch_with_drc(**kwargs):
            return {
                "passed": False,
                "drc_result": {"ok": True, "pass": True, "artifacts": {"issues": []}},
                "fill_detail": {"reason": "fill_failed", "error": "no segment or via"},
                "failure_summary": "KiCad patch fill failed: no segment or via",
                "filled_board_data_file_path": "",
            }

    config = load_config("missing-config.ini")
    config.drc.enabled = True
    config.drc.tool_path = "tools/pcb_reroute_drc.py"
    config.drc.eval_root = "."
    monkeypatch.setattr(external, "_load_module", lambda *args, **kwargs: FakeDrcModule)
    context = {
        "session_id": "s1",
        "rerouteInput": {"kicadBoardText": "(kicad_pcb demo)", "selectedNets": ["Z7_SPI0_SCK"]},
        "selectedNets": ["Z7_SPI0_SCK"],
        "rerouteResult": {"status": "ok", "routedText": "not a segment"},
    }
    result = asyncio.run(AnalysisTool("drc_check", config).ainvoke({}, context))
    assert result["status"] == "failed"
    assert result["passed"] is False
    assert result["drcExecutionValid"] is False
    assert result["targetScopedPassed"] is False


# ====== 功能：验证默认 reroute_loop 失败后 planner 直接进入 help_planner。 ======
def test_planner_reroute_loop_failure_calls_help_planner_not_legacy_retry():
    cache = {
        "deleteTracesResult": {"status": "ok"},
        "rerouteInput": {"status": "ok"},
        "rerouteContext": {"status": "ok"},
        "rerouteLoopResult": {"status": "failed", "reason": "drc_failed"},
    }
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续", "workflow_state": "error", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["action"] == "help_planner"
    assert [call["name"] for call in plan["tool_calls"]] == ["help_planner"]


# ====== 功能：验证 DRC 通过后才单独调用 explainability。 ======
def test_planner_calls_explainability_only_after_drc_passed():
    cache = {
        "deleteTracesResult": {"status": "ok"},
        "rerouteInput": {"status": "ok"},
        "rerouteContext": {"status": "ok"},
        "rerouteResult": {"status": "ok"},
        "drcResult": {"status": "ok", "passed": True, "drcExecutionValid": True},
    }
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续", "workflow_state": "report", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["action"] == "explainability"
    assert [call["name"] for call in plan["tool_calls"]] == ["explainability_report"]


# ====== 功能：验证 help_planner 完整板输出优先用于 full-board DRC。 ======
def test_drc_uses_help_planner_full_board_without_routed_text(monkeypatch, tmp_path):
    from pcb_agent_langgraph.tools import external

    routed_board = tmp_path / "output_bga_local.export.kicad_pcb"
    routed_board.write_text("(kicad_pcb routed)", encoding="utf-8")

    class FakeDrcModule:
        called = ""

        @staticmethod
        def validate_kicad_board_with_drc(**kwargs):
            FakeDrcModule.called = "board"
            return {
                "passed": True,
                "drc_result": {"ok": True, "pass": True, "artifacts": {"issues": []}},
                "failure_summary": "",
                "filled_board_data_file_path": str(routed_board),
            }

        @staticmethod
        def validate_kicad_patch_with_drc(**kwargs):
            FakeDrcModule.called = "patch"
            return {"passed": False, "drc_result": {"ok": False}, "failure_summary": "patch should not run"}

    config = load_config("missing-config.ini")
    config.drc.enabled = True
    config.drc.tool_path = "tools/pcb_reroute_drc.py"
    config.drc.eval_root = "."
    monkeypatch.setattr(external, "_load_module", lambda *args, **kwargs: FakeDrcModule)
    context = {
        "session_id": "s1",
        "rerouteInput": {"kicadBoardText": "(kicad_pcb original)", "selectedNets": ["Z7_SPI0_SCK"]},
        "helpPlannerResult": {"status": "ok", "routedKicadFilePath": str(routed_board)},
        "rerouteResult": {"status": "ok", "routedKicadFilePath": str(routed_board)},
    }
    result = asyncio.run(AnalysisTool("drc_check", config).ainvoke({}, context))
    assert FakeDrcModule.called == "board"
    assert result["status"] == "ok"
    assert result["drcInputMode"] == "full_board"
    assert result["drcInputSource"] in {"helpPlannerResult", "rerouteResult"}


# ====== 功能：验证 full-board 残留非目标错误可和 target scoped 结果区分。 ======
def test_drc_target_scoped_pass_with_full_board_residual_issue(monkeypatch, tmp_path):
    from pcb_agent_langgraph.tools import external

    routed_board = tmp_path / "routed.kicad_pcb"
    routed_board.write_text("(kicad_pcb routed)", encoding="utf-8")

    class FakeDrcModule:
        @staticmethod
        def validate_kicad_board_with_drc(**kwargs):
            return {
                "passed": False,
                "drc_result": {"ok": True, "pass": False, "artifacts": {"issues": [{"net": "PS_MIO50_501", "message": "unrelated clearance"}]}},
                "failure_summary": "hard_issue_count=1",
                "filled_board_data_file_path": str(routed_board),
            }

    config = load_config("missing-config.ini")
    config.drc.enabled = True
    config.drc.tool_path = "tools/pcb_reroute_drc.py"
    config.drc.eval_root = "."
    monkeypatch.setattr(external, "_load_module", lambda *args, **kwargs: FakeDrcModule)
    context = {
        "session_id": "s1",
        "rerouteInput": {"kicadBoardText": "(kicad_pcb original)", "selectedNets": ["Z7_SPI0_SCK"]},
        "selectedNets": ["Z7_SPI0_SCK"],
        "rerouteResult": {"status": "ok", "routedKicadFilePath": str(routed_board)},
    }
    result = asyncio.run(AnalysisTool("drc_check", config).ainvoke({}, context))
    assert result["passed"] is True
    assert result["fullBoardPassed"] is False
    assert result["targetScopedPassed"] is True
    assert result["fullBoardIssueCount"] == 1
    assert result["targetIssueCount"] == 0
