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

# ====== 功能：验证 reroute 上下文完成后直接进入主模型重布。 ======
def test_reroute_after_context_calls_model_reroute_directly():
    cache = {"deleteTracesResult": {"status": "ok"}, "rerouteInput": {"status": "ok", "selectedNets": ["GND"]}, "rerouteContext": {"status": "ok", "selectedNets": ["GND"]}}
    state = {"user_input": "继续", "workflow_state": "report", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache}
    plan = PCBPlanner(use_model=False).plan(state)
    assert plan["action"] == "reroute"
    assert plan["tool_calls"][0]["name"] == "reroute"
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
    assert plan["action"] == "reroute"
    assert plan["tool_calls"][0]["name"] == "reroute"


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
    assert plan["action"] == "reroute"
    assert plan["tool_calls"][0]["name"] == "reroute"
# ====== 功能：验证 help_planner 不会把 export.txt 伪装成 KiCad 输入。 ======
def test_help_planner_rejects_export_txt_input():
    config = load_config("missing-config.ini")
    tool = ExternalProgramTool("help_planner", config)
    context = {"session_id": "s1", "projectData": "F:/doctor/pcb/test/1/Output/demo/export.txt", "selectedNets": ["Z7_SPI0_SCK"]}
    result = asyncio.run(tool.ainvoke({}, context))
    assert result["status"] == "failed"
    assert "requires KiCad .kicad_pcb input" in result["reason"]


# ====== 功能：验证 KiCad 文本识别兼容 S-expression 中左括号后的空白。
def test_kicad_board_text_accepts_whitespace_after_open_paren():
    from pcb_agent_langgraph.tools.external import _help_planner_input_error, _is_kicad_board_text

    assert _is_kicad_board_text("(kicad_pcb (version 20240108))")
    assert _is_kicad_board_text("\n ( kicad_pcb  ( version 20171130 ))")
    assert not _is_kicad_board_text("F:/doctor/pcb/test/1/Output/demo/export.txt")
    assert _help_planner_input_error("( kicad_pcb  ( version 20171130 ))", "") == ""


# ====== 功能：验证主 reroute 不再把带空格的 KiCad 文件头误判为缺失输入。
def test_reroute_accepts_kicad_header_with_space_after_open_paren():
    from pcb_agent_langgraph.models.pcb_model import ModelResult
    from pcb_agent_langgraph.tools import external as external_mod

    class FakePCBModel:
        def __init__(self, config):
            self.config = config

        def complete(self, messages, temperature=0.0):
            return ModelResult(content='{"routedText":"(kicad_pcb routed)","report":"ok"}', raw={}, elapsed_ms=1.0, usage={})

    config = load_config("missing-config.ini")
    config.model.base_url = "http://model.local"
    config.model.model = "pcb-reroute"
    tool = ExternalProgramTool("reroute", config)
    context = {
        "session_id": "s1",
        "rerouteInput": {
            "status": "ok",
            "kicadBoardText": "( kicad_pcb  ( version 20171130 ))",
            "kicadBoardPath": "board.kicad_pcb",
            "selectedNets": ["Z7_SPI0_SCK"],
        },
        "rerouteContext": {"status": "ok"},
    }
    old_model = external_mod.PCBModel
    external_mod.PCBModel = FakePCBModel
    try:
        result = asyncio.run(tool.ainvoke({}, context))
    finally:
        external_mod.PCBModel = old_model
    assert result["status"] == "ok"
    assert result["kicadBoardPath"] == "board.kicad_pcb"

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


# ====== 功能：验证主 reroute unavailable 直接报告，不进入 help_planner。 ======
def test_reroute_unavailable_does_not_call_help_planner():
    cache = {"deleteTracesResult": {"status": "ok"}, "rerouteInput": {"status": "ok"}, "rerouteContext": {"status": "ok"}, "rerouteUnavailable": True, "rerouteUnavailableReason": "model 401"}
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续", "workflow_state": "rip_up", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["tool_calls"] == []
    assert plan["action"] == "reroute_unavailable"
    assert "model 401" in plan["response"]


# ====== 功能：验证 reroute DRC 失败轮次不调用可解释性模型。 ======
def test_reroute_drc_failed_retries_without_explainability():
    cache = {
        "deleteTracesResult": {"status": "ok"},
        "rerouteInput": {"status": "ok"},
        "rerouteContext": {"status": "ok"},
        "rerouteResult": {"status": "ok"},
        "drcResult": {"status": "failed", "passed": False, "reason": "violation"},
        "rerouteDrcFailureCount": 1,
    }
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续", "workflow_state": "error", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["action"] == "reroute_retry"
    assert [call["name"] for call in plan["tool_calls"]] == ["reroute"]


# ====== 功能：验证 reroute 只有最终 DRC 通过后才调用可解释性模型。 ======
def test_reroute_drc_pass_triggers_single_explainability_call():
    cache = {
        "deleteTracesResult": {"status": "ok"},
        "rerouteInput": {"status": "ok"},
        "rerouteContext": {"status": "ok"},
        "rerouteResult": {"status": "ok", "routedKicadFilePath": "board.kicad_pcb"},
        "drcResult": {"status": "ok", "passed": True},
    }
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续", "workflow_state": "drc", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["action"] == "explainability"
    assert [call["name"] for call in plan["tool_calls"]] == ["explainability_report"]


# ====== 功能：验证 help_planner 失败样例会写入 manifest 并标记缺失输出。 ======
def test_help_planner_repro_archive_manifest(tmp_path):
    from pcb_agent_langgraph.tools.external import _archive_help_planner_repro_case

    work_dir = tmp_path / "work"
    run_dir = work_dir / "pcbrouter_local_completion"
    run_dir.mkdir(parents=True)
    source_txt = tmp_path / "export.txt"
    input_board = run_dir / "local_route_input.kicad_pcb"
    input_csv = run_dir / "local_route_input.csv"
    stderr_log = run_dir / "pcbrouter_stderr.log"
    source_txt.write_text("export data", encoding="utf-8")
    input_board.write_text("( kicad_pcb (version 20171130))", encoding="utf-8")
    input_csv.write_text("net\nZ7_SPI0_IO3\n", encoding="utf-8")
    stderr_log.write_text("Cannot infer local target", encoding="utf-8")

    paths = _archive_help_planner_repro_case(
        work_dir=work_dir,
        source_layout_path=str(source_txt),
        source_board_path=str(input_board),
        input_board_path=str(input_board),
        input_csv_path=str(input_csv),
        output_board_path=str(run_dir / "output.kicad_pcb"),
        output_csv_path=str(run_dir / "output.csv"),
        import_lines_path=str(run_dir / "line.out"),
        route_params={"selectedNets": ["Z7_SPI0_IO3"]},
        selected_nets=["Z7_SPI0_IO3"],
        missing_routes=[{"net_name": "Z7_SPI0_IO3"}],
        status="failed",
        reason="router failed",
        command="pcbrouter ...",
    )

    manifest_path = Path(paths["reproManifestPath"])
    assert manifest_path.is_file()
    manifest = __import__("json").loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["files"]["inputBoardPath"]["exists"] is True
    assert manifest["files"]["inputCsvPath"]["exists"] is True
    assert manifest["files"]["outputBoardPath"]["exists"] is False
    assert manifest["files"]["importLinesFilePath"]["exists"] is False
    assert "Cannot infer local target" in manifest["stderrSummary"]

# ====== 功能：验证最终 DRC 失败时 smoke 可解释性报告路径会透传给前端 debug payload。 ======
def test_markdown_report_exposes_explainability_smoke_debug_path():
    from pcb_agent_langgraph.reports.markdown import build_markdown_report

    report = build_markdown_report(
        "reroute",
        {
            "drcResult": {
                "status": "failed",
                "passed": False,
                "debug": {"explainabilitySmoke": {"status": "ok", "report_path": "router_work/s1/explainability_smoke/report.txt"}},
            },
            "helpPlannerResult": {"status": "failed"},
        },
    )
    assert report["debug"]["explainabilitySmokeReportPath"] == "router_work/s1/explainability_smoke/report.txt"

# ====== 功能：验证拆线 missing_routes 会同步转换成 KiCad 坐标并写入几何 CSV。 ======
def test_prepare_reroute_inputs_converts_missing_route_geometry(tmp_path):
    config = load_config("missing-config.ini")
    tool = ExternalProgramTool("prepare_reroute_inputs", config)
    board_path = tmp_path / "board.kicad_pcb"
    board_path.write_text("( kicad_pcb (version 20171130) (layers (0 Top signal) (31 Bottom signal)))\n", encoding="utf-8")
    context = {
        "session_id": "geom",
        "deleteTracesResult": {
            "projectData": str(board_path),
            "missing_routes": [
                {
                    "net_name": "DDR_D6",
                    "start": {"component": "U5", "pad": "C1", "layer": "Top", "x": 684.21, "y": 195.78},
                    "end": {"layer": "Top", "x": 747.19, "y": 195.78},
                }
            ],
        },
    }
    result = asyncio.run(tool.ainvoke({}, context))
    assert result["status"] == "ok"
    assert result["geometryConversionStatus"] == "ok"
    route = result["missingRoutesKicad"][0]
    assert route["start"]["coordinateSystem"] == "kicad_mm"
    assert route["start"]["sourceCoordinateSystem"] == "pcb_builder_export_dbu"
    assert route["start"]["outlineOnlyOriginApplied"] is True
    assert route["start"]["origin_x"] == 363386
    assert route["start"]["origin_y"] == 534646
    assert abs(route["start"]["x"] - 92.473834) < 0.000001
    assert abs(route["start"]["y"] - 135.849812) < 0.000001
    assert abs(route["end"]["x"] - 92.48983) < 0.000001
    assert abs(route["end"]["y"] - 135.849812) < 0.000001
    assert route["route_layer"] == "Top"
    csv_lines = Path(result["localRouteCsvPath"]).read_text(encoding="utf-8").splitlines()
    assert csv_lines[0].startswith("net,route_layer,start_x,start_y,end_x,end_y")
    assert "DDR_D6" in csv_lines[1]
    assert result["csvMode"] == "kicad_geometry"


# ====== 功能：验证 help_planner 优先使用转换后的 missingRoutesKicad。 ======
def test_help_route_params_prefers_missing_routes_kicad():
    from pcb_agent_langgraph.tools.external import _help_route_params

    params = _help_route_params(
        {},
        {
            "rerouteInput": {
                "missingRoutes": [{"net_name": "RAW", "start": {"x": 1000, "y": 2000}, "end": {"x": 3000, "y": 4000}}],
                "missingRoutesKicad": [{"net_name": "DDR_D6", "start": {"x": 0.17, "y": 0.05}, "end": {"x": 0.19, "y": 0.05}, "route_layer": "Top"}],
            }
        },
    )
    assert params["orderLines"] == params["missingRoutesKicad"]
    assert params["pcbrouterNets"] == params["missingRoutesKicad"]


# ====== 功能：验证 Cannot infer local target 不再被误报为 KiCad 输入无效。 ======
def test_help_planner_cannot_infer_target_failure_type():
    from pcb_agent_langgraph.graph.nodes import _reroute_failure_text

    text = _reroute_failure_text(
        {
            "stage": "兜底规则布线",
            "tool": "help_planner",
            "status": "failed",
            "failureType": "local_target_inference_failed",
            "reason": "ERROR: Cannot infer local target for net DDR_D6 because it has fewer than two physical pins and no existing route geometry.",
            "nextAction": "局部布线器无法从当前 CSV 推断目标；请检查 missingRoutes 几何转换、CSV header 和 start/end KiCad 坐标。",
        }
    )
    assert "local_target_inference_failed" in text
    assert "拆线几何" in text or "missingRoutes" in text

# ====== 功能：验证 help_planner 归档缺少 missingRoutesKicad 时不会抛 NameError。 ======
def test_help_planner_archive_handles_missing_kicad_routes(tmp_path):
    from pcb_agent_langgraph.tools.external import _archive_help_planner_repro_case

    paths = _archive_help_planner_repro_case(
        work_dir=tmp_path / "help_planner",
        source_layout_path="",
        source_board_path="",
        input_board_path="",
        input_csv_path="",
        output_board_path="",
        output_csv_path="",
        import_lines_path="",
        route_params={},
        selected_nets=[],
        missing_routes=[],
        status="failed",
        reason="router failed",
        command="",
    )
    manifest = __import__("json").loads(Path(paths["reproManifestPath"]).read_text(encoding="utf-8"))
    assert manifest["missingRoutesKicad"] == []
    assert manifest["hasRouteGeometry"] is False


# ====== 功能：验证 help_planner 归档异常不会遮蔽原始失败。 ======
def test_safe_help_planner_archive_returns_repro_archive_error(tmp_path):
    from pcb_agent_langgraph.tools.external import _safe_archive_help_planner_repro_case

    result = _safe_archive_help_planner_repro_case(
        work_dir=tmp_path / "missing_parent" / "x",
        source_layout_path="",
        source_board_path="",
        input_board_path="",
        input_csv_path="",
        output_board_path="",
        output_csv_path="",
        import_lines_path="",
        route_params=None,
        selected_nets=None,
        missing_routes=None,
        status="failed",
        reason="router failed",
        command="",
    )
    assert "reproManifestPath" in result or "reproArchiveError" in result
# ====== 功能：验证主模型输出 raw 坐标会被识别为坐标语义错误。
def test_reroute_model_coordinate_check_rejects_raw_export_coords():
    from pcb_agent_langgraph.tools.external import _validate_reroute_model_coordinates

    routes = [
        {
            "net_name": "Z7_SPI0_SCK",
            "start": {"x": 92.441834, "y": 135.833813, "source_x": 558.23, "source_y": 132.79},
            "end": {"x": 92.441834, "y": 135.798367, "source_x": 558.23, "source_y": -6.76},
        }
    ]
    routed_text = '(segment (start 558.23 132.79) (end 558.23 -6.76) (width 0.1524) (layer Top) (net 58))'
    result = _validate_reroute_model_coordinates(routed_text, routes)
    assert result["passed"] is False
    assert result["rawHits"] > 0
    assert "raw PCB Builder" in result["reason"]


# ====== 功能：验证 help_planner 会把 missingRoutesKicad 注入临时 seed segment。
def test_help_planner_seed_geometry_injected_into_board(tmp_path):
    from importlib.util import module_from_spec, spec_from_file_location

    module_path = Path(__file__).resolve().parents[1] / "tools" / "pcb_local_router.py"
    spec = spec_from_file_location("pcb_local_router_test_seed", module_path)
    module = module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)

    board = tmp_path / "board.kicad_pcb"
    board.write_text('(kicad_pcb (version 20240108) (net 58 "Z7_SPI0_SCK") (layers (0 Top signal)))\n', encoding="utf-8")
    route_params = {
        "missingRoutesKicad": [
            {"net_name": "Z7_SPI0_SCK", "route_layer": "Top", "start": {"x": 92.441834, "y": 135.833813}, "end": {"x": 92.441834, "y": 135.798367}}
        ]
    }
    injected, count, seed_path = module._inject_seed_geometry(board, route_params, tmp_path)
    text = board.read_text(encoding="utf-8")
    assert injected is True
    assert count == 1
    assert seed_path and seed_path.is_file()
    assert "(segment" in text
    assert "92.441834" in text
    assert "(net 58)" in text


# ====== 功能：验证 help_planner diagnostics 使用完整 stderr 并定位实际 export.kicad_pcb。
def test_help_planner_diagnostics_prefers_stderr_and_actual_input_board(tmp_path):
    from pcb_agent_langgraph.tools.external import _help_planner_diagnostics, _help_planner_failure_reason

    work_dir = tmp_path / "help_planner"
    run_dir = work_dir / "pcbrouter_local_completion"
    run_dir.mkdir(parents=True)
    board = run_dir / "export.kicad_pcb"
    board.write_text("(kicad_pcb demo)", encoding="utf-8")
    stderr = run_dir / "pcbrouter.stderr.log"
    stderr.write_text("Build Kicad Pcb database...\nERROR: Cannot infer local target for net Z7_SPI0_SCK because it has fewer than two physical pins and no existing route geometry.\n", encoding="utf-8")

    diagnostics = _help_planner_diagnostics(work_dir, tmp_path / "pcbrouter.exe", str(board))
    reason = _help_planner_failure_reason("truncated stdout", diagnostics)
    assert diagnostics["inputBoardPath"] == str(board)
    assert "Cannot infer local target" in reason
    assert "truncated stdout" not in reason

# ====== 功能：验证 help_planner 只有完整 KiCad 输出板时 DRC 走 full-board 校验。
def test_drc_uses_help_planner_full_board_without_routed_text(monkeypatch, tmp_path):
    from dataclasses import dataclass
    from pcb_agent_langgraph.tools import external as external_mod

    @dataclass
    class FakeAttempt:
        passed: bool
        drc_result: dict
        failure_summary: str = ""
        filled_board_data_file_path: str = ""

    class FakeDrcModule:
        called_board_path = ""

        @staticmethod
        def set_eval_root(_path):
            return None

        @staticmethod
        def validate_kicad_board_with_drc(board_path, sample_id, iteration):
            FakeDrcModule.called_board_path = str(board_path)
            return FakeAttempt(passed=True, drc_result={"mode": "full_board"}, filled_board_data_file_path=str(board_path))

        @staticmethod
        def validate_kicad_patch_with_drc(**_kwargs):
            raise AssertionError("patch DRC should not be used when help_planner produced a full board")

    board = tmp_path / "output_bga_local.export.kicad_pcb"
    board.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool_path = tmp_path / "fake_drc.py"
    tool_path.write_text("# fake\n", encoding="utf-8")
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    cfg = tmp_path / "config.ini"
    cfg.write_text(
        "[drc]\n"
        "enabled = 1\n"
        f"tool_path = {tool_path}\n"
        f"eval_root = {eval_root}\n",
        encoding="utf-8",
    )
    config = load_config(cfg)
    monkeypatch.setattr(external_mod, "_load_module", lambda _name, _path: FakeDrcModule)

    result = asyncio.run(AnalysisTool("drc_check", config).ainvoke({}, {"session_id": "s-full", "helpPlannerResult": {"status": "ok", "routedKicadFilePath": str(board)}}))

    assert result["status"] == "ok"
    assert result["passed"] is True
    assert result["drcInputMode"] == "full_board"
    assert result["drcInputSource"] == "helpPlannerResult"
    assert result["routedBoardPath"] == str(board)
    assert result["routedTextChars"] == 0
    assert FakeDrcModule.called_board_path == str(board)


# ====== 功能：验证 reroute DRC 报告会显示 full-board 输入来源和路径。
def test_markdown_report_shows_drc_input_diagnostics(tmp_path):
    from pcb_agent_langgraph.reports.markdown import build_markdown_report

    board = tmp_path / "out.kicad_pcb"
    board.write_text("(kicad_pcb)\n", encoding="utf-8")
    report = build_markdown_report(
        "reroute",
        {
            "drcResult": {
                "status": "failed",
                "passed": False,
                "errors": ["clearance violation"],
                "drcInputMode": "full_board",
                "drcInputSource": "helpPlannerResult",
                "routedBoardPath": str(board),
                "routedTextChars": 0,
            },
            "helpPlannerResult": {"status": "ok", "routedKicadFilePath": str(board)},
        },
    )
    markdown = report["markdown"]
    assert "DRC input mode: `full_board`" in markdown
    assert "DRC input source: `helpPlannerResult`" in markdown
    assert str(board) in markdown

# ====== 功能：验证 reroute DRC 以目标网络为准，不被全板其它历史错误误杀。
def test_reroute_drc_passes_when_target_net_has_no_issues(monkeypatch, tmp_path):
    from dataclasses import dataclass
    from pcb_agent_langgraph.tools import external as external_mod

    @dataclass
    class FakeAttempt:
        passed: bool
        drc_result: dict
        failure_summary: str = "full board failed"
        filled_board_data_file_path: str = ""

    class FakeDrcModule:
        @staticmethod
        def set_eval_root(_path):
            return None

        @staticmethod
        def validate_kicad_board_with_drc(board_path, sample_id, iteration):
            return FakeAttempt(
                passed=False,
                filled_board_data_file_path=str(board_path),
                drc_result={
                    "ok": True,
                    "pass": False,
                    "details": {"hard_issue_count": 1, "hard_rule_counts": {"HR_CONNECT_PAD_NOT_ESCAPED": 1}},
                    "artifacts": {"issues": [{"rule": "HR_CONNECT_PAD_NOT_ESCAPED", "severity": "ERROR", "message": "BGA pad U5.B13 on net PS_MIO50_501 has no initial escape connection."}]},
                },
            )

        @staticmethod
        def validate_kicad_patch_with_drc(**_kwargs):
            raise AssertionError("patch DRC should not be used")

    board = tmp_path / "output.kicad_pcb"
    board.write_text("(kicad_pcb (version 20240108))\n", encoding="utf-8")
    tool_path = tmp_path / "fake_drc.py"
    tool_path.write_text("# fake\n", encoding="utf-8")
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    cfg = tmp_path / "config.ini"
    cfg.write_text("[drc]\nenabled = 1\n" f"tool_path = {tool_path}\n" f"eval_root = {eval_root}\n", encoding="utf-8")
    config = load_config(cfg)
    monkeypatch.setattr(external_mod, "_load_module", lambda _name, _path: FakeDrcModule)

    result = asyncio.run(AnalysisTool("drc_check", config).ainvoke({}, {"session_id": "target-pass", "selectedNets": ["Z7_SPI0_SCK"], "helpPlannerResult": {"status": "ok", "routedKicadFilePath": str(board)}}))

    assert result["status"] == "ok"
    assert result["passed"] is True
    assert result["fullBoardPassed"] is False
    assert result["targetScopedPassed"] is True
    assert result["targetIssueCount"] == 0
    assert result["fullBoardResidualIssueCount"] == 1


# ====== 功能：验证 reroute DRC 报告展示目标网络通过和全板残留错误。
def test_markdown_report_shows_target_scoped_drc_and_residual_issues():
    from pcb_agent_langgraph.reports.markdown import build_markdown_report

    report = build_markdown_report(
        "reroute",
        {
            "drcResult": {
                "status": "ok",
                "passed": True,
                "fullBoardPassed": False,
                "targetScopedPassed": True,
                "targetNets": ["Z7_SPI0_SCK"],
                "targetIssueCount": 0,
                "fullBoardIssueCount": 27,
                "fullBoardResidualIssues": [{"rule": "HR_CONNECT_PAD_NOT_ESCAPED", "message": "BGA pad U5.B13 on net PS_MIO50_501 has no initial escape connection."}],
            }
        },
    )
    markdown = report["markdown"]
    assert "Target-scoped DRC passed: `True`" in markdown
    assert "Full-board DRC passed: `False`" in markdown
    assert "Z7_SPI0_SCK" in markdown
    assert "PS_MIO50_501" in markdown
# ====== 功能：验证 patch fill failed 时不能因为目标网无 issue 被误判为 target-scoped 通过。
def test_drc_fill_failed_cannot_target_scope_pass(monkeypatch, tmp_path):
    from dataclasses import dataclass
    from pcb_agent_langgraph.tools import external as external_mod

    @dataclass
    class FakeAttempt:
        passed: bool
        drc_result: dict
        fill_detail: dict
        failure_summary: str
        filled_board_data_file_path: str = ""

    class FakeDrcModule:
        @staticmethod
        def set_eval_root(_path):
            return None

        @staticmethod
        def validate_kicad_patch_with_drc(**_kwargs):
            return FakeAttempt(
                passed=False,
                drc_result={},
                fill_detail={"reason": "fill_failed", "error": "no segment or via"},
                failure_summary="KiCad patch fill failed: no segment or via",
            )

    tool_path = tmp_path / "fake_drc.py"
    tool_path.write_text("# fake\n", encoding="utf-8")
    eval_root = tmp_path / "eval"
    eval_root.mkdir()
    cfg = tmp_path / "config.ini"
    cfg.write_text("[drc]\nenabled = 1\n" f"tool_path = {tool_path}\n" f"eval_root = {eval_root}\n", encoding="utf-8")
    config = load_config(cfg)
    monkeypatch.setattr(external_mod, "_load_module", lambda _name, _path: FakeDrcModule)

    result = asyncio.run(AnalysisTool("drc_check", config).ainvoke({}, {"session_id": "fill-failed", "selectedNets": ["Z7_SPI0_SCK"], "rerouteInput": {"kicadBoardText": "(kicad_pcb)"}, "rerouteResult": {"status": "ok", "routedText": "not a segment"}}))

    assert result["status"] == "failed"
    assert result["passed"] is False
    assert result["drcExecutionValid"] is False
    assert result["targetScopedPassed"] is False
    assert "target-scoped pass is not allowed" in result["targetFailureSummary"]


# ====== 功能：验证 DRC fill failed 后 planner 继续 reroute，不进入 explainability。
def test_planner_retries_after_drc_fill_failed_not_explainability():
    cache = {
        "deleteTracesResult": {"status": "ok"},
        "rerouteInput": {"status": "ok", "selectedNets": ["Z7_SPI0_SCK"]},
        "rerouteContext": {"status": "ok"},
        "rerouteResult": {"status": "ok", "routedText": "not a segment"},
        "rerouteAttemptCount": 1,
        "rerouteDrcFailureCount": 1,
        "drcResult": {"status": "failed", "passed": False, "drcExecutionValid": False, "reason": "fill failed"},
    }
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续", "workflow_state": "report", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["action"] == "reroute_retry"
    assert plan["tool_calls"][0]["name"] == "reroute"