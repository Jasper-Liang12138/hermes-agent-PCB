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
# ====== 功能：验证 reroute 删除走线后先压缩 KiCad 上下文再进入模型重布。 ======
def test_reroute_after_delete_compresses_context_before_model():
    cache = {"deleteTracesResult": {"status": "ok", "selectedNets": ["DDR_DQ0"], "projectData": {"boardData": "(kicad_pcb (segment (net 1)))"}}}
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续 reroute", "workflow_state": "report", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["tool_calls"][0]["name"] == "compress_reroute_context"


# ====== 功能：验证 KiCad reroute 上下文压缩会优先检索目标网络片段。 ======
def test_reroute_context_retrieves_target_net_chunk():
    from pcb_agent_langgraph.tools.reroute_context import build_reroute_context

    board_text = "NET USB_DP unrelated\n" * 200 + "(segment (start 1 1) (end 2 2) (layer Top) (net DDR_DQ0))\n" + "NET HDMI unrelated\n" * 200
    result = build_reroute_context(board_text=board_text, task_description="repair DDR_DQ0", nets=["DDR_DQ0"], chunk_chars=512, overlap_chars=64, retrieve_k=1)
    assert result["status"] == "ok"
    assert "DDR_DQ0" in result["contextText"]
    assert result["stats"]["retrievedSegmentCount"] == 1
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

# ====== 功能：验证 reroute 上下文完成后先等待用户确认。 ======
def test_reroute_after_context_waits_for_confirm_before_reroute():
    cache = {"deleteTracesResult": {"status": "ok"}, "rerouteContext": {"status": "ok", "selectedNets": ["GND"]}}
    state = {"user_input": "继续", "workflow_state": "report", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache}
    plan = PCBPlanner(use_model=False).plan(state)
    assert plan["tool_calls"] == []
    assert plan["action"] == "reroute_context_ready"

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

# ====== 功能：验证 reroute 上下文压缩后等待用户确认。 ======
def test_reroute_context_waits_for_confirm():
    cache = {"deleteTracesResult": {"status": "ok"}, "rerouteContext": {"status": "ok", "selectedNets": ["GND"]}}
    plan = PCBPlanner(use_model=False).plan({"user_input": "继续", "workflow_state": "rip_up", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache})
    assert plan["tool_calls"] == []
    assert plan["action"] == "reroute_context_ready"


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


# ====== 功能：验证已有 rerouteContext 时模型重复 compress 会被改写为确认停点。 ======
def test_model_repeated_compress_context_rewritten_to_confirm_stop():
    from pcb_agent_langgraph.models.pcb_model import ModelResult

    class FakeModel:
        def complete(self, messages, temperature=0.0):
            return ModelResult(content='{"intent":"reroute","workflow":"pcb_reroute_flow","tool_calls":[{"name":"compress_reroute_context","arguments":{}}]}', raw={}, elapsed_ms=1.0, usage={})

    cache = {"deleteTracesResult": {"status": "ok"}, "rerouteContext": {"status": "ok", "selectedNets": ["Z7_SPI0_SCK"]}}
    state = {"user_input": "继续", "workflow_state": "rip_up", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache}
    plan = PCBPlanner(model=FakeModel(), use_model=True, require_model=True).plan(state)
    assert plan["tool_calls"] == []
    assert plan["action"] == "reroute_context_ready"


# ====== 功能：验证未确认 reroute 时模型越级 reroute 会被改写为等待确认。 ======
def test_model_reroute_before_confirm_rewritten_to_wait_confirm():
    from pcb_agent_langgraph.models.pcb_model import ModelResult

    class FakeModel:
        def complete(self, messages, temperature=0.0):
            return ModelResult(content='{"intent":"reroute","workflow":"pcb_reroute_flow","tool_calls":[{"name":"reroute","arguments":{}}]}', raw={}, elapsed_ms=1.0, usage={})

    cache = {"deleteTracesResult": {"status": "ok"}, "rerouteContext": {"status": "ok", "selectedNets": ["Z7_SPI0_SCK"]}}
    state = {"user_input": "先等等", "workflow_state": "confirm", "workflow_id": "pcb_reroute_flow", "task_type": "reroute", "intermediate_cache": cache}
    plan = PCBPlanner(model=FakeModel(), use_model=True, require_model=True).plan(state)
    assert plan["tool_calls"] == []
    assert plan["action"] == "wait_reroute_confirm"


# ====== 功能：验证 help_planner 不会把 export.txt 伪装成 KiCad 输入。 ======
def test_help_planner_rejects_export_txt_input():
    config = load_config("missing-config.ini")
    tool = ExternalProgramTool("help_planner", config)
    context = {"session_id": "s1", "projectData": "F:/doctor/pcb/test/1/Output/demo/export.txt", "selectedNets": ["Z7_SPI0_SCK"]}
    result = asyncio.run(tool.ainvoke({}, context))
    assert result["status"] == "failed"
    assert "requires KiCad .kicad_pcb input" in result["reason"]
