from __future__ import annotations

import json
import re
import uuid
from typing import Any

from pcb_agent_langgraph.entry import ENTRY_MODULE_KEYS, entry_task_workflow, normalize_entry_module
from pcb_agent_langgraph.graph.state import PCBState, PlannerOutput, ToolCall
from pcb_agent_langgraph.models.pcb_model import PCBModel
from pcb_agent_langgraph.planner.intent_entities import extract_fanout_entities, normalize_router_type
from pcb_agent_langgraph.planner.prompts import planner_system_prompt
from pcb_agent_langgraph.planner.tool_call_parser import parse_tool_call_markup
from pcb_agent_langgraph.utils.config import AppConfig


# ====== 功能：判断 DRC 工具结果是否失败。 ======
def _drc_failed(result: Any) -> bool:
    return isinstance(result, dict) and (
        str(result.get("status", "")).lower() in {"failed", "error"}
        or result.get("passed") is False
    )


# ====== 功能：判断 DRC 是否真实通过。 ======
def _drc_passed(result: Any) -> bool:
    return isinstance(result, dict) and result.get("passed") is True and result.get("drcExecutionValid") is not False

# ====== 功能：判断阶段产物是否真实成功，避免 failed dict 被当成完成态。
def _stage_ok(result: Any) -> bool:
    return isinstance(result, dict) and str(result.get("status", "")).lower() == "ok"

# ====== 功能：合并多轮对话抽取到的 fanout 实体和约束。 ======
def _merge_entities(*items: Any) -> dict[str, Any]:
    # 多轮对话里，用户可能分几次补充 BGA、router 类型、线宽线距等约束。
    # 这里把历史缓存和本轮抽取结果合并，constraints 做增量覆盖。
    merged: dict[str, Any] = {}
    for item in items:
        if isinstance(item, dict):
            for key, value in item.items():
                if value not in (None, "", [], {}):
                    if key == "constraints" and isinstance(value, dict):
                        constraints = dict(merged.get("constraints") or {})
                        constraints.update(value)
                        merged["constraints"] = constraints
                    elif key == "routerType":
                        merged[key] = normalize_router_type(value) or value
                    else:
                        merged[key] = value
    return merged


# ====== 功能：从实体中筛选 fanout 工具需要的参数。 ======
def _selected_bga(entities: dict[str, Any]) -> str:
    return str(entities.get("selectedBGA") or entities.get("targetBGA") or "").strip().upper()


# ====== 功能：判断 fanout 是否已有明确目标器件。 ======
def _has_selected_bga(entities: dict[str, Any]) -> bool:
    return bool(_selected_bga(entities))


# ====== 功能：从实体中筛选 fanout 工具需要的参数。 ======
def _fanout_args(entities: dict[str, Any]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for key in ("selectedBGA", "targetBGA", "targetBGAs", "bgaType", "bgaLayoutType", "routerType", "constraints"):
        value = entities.get(key)
        if value not in (None, "", [], {}):
            args[key] = value
    return args


# ====== 功能：判断用户是否确认继续执行当前等待步骤。 ======
def _is_confirm_text(text: str) -> bool:
    return bool(re.search(r"确认|继续|开始|可以|yes|ok|执行|导入", text, re.IGNORECASE))


# ====== 功能：把 BGA 候选转换成前端 v0.6 selection 结构，并保留 BGA 类型等元数据。 ======
def _bga_selection(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selection: list[dict[str, Any]] = []
    for item in candidates:
        refdes = str(item.get("refdes") or item.get("reference") or "").upper()
        if not refdes:
            continue
        pin_count = item.get("pinCount") or item.get("pincount") or item.get("pins") or ""
        footprint = str(item.get("footprint") or item.get("package") or "").strip()
        part = str(item.get("part") or item.get("name") or "").strip()
        bga_type, type_source = _bga_type(item)
        detail_parts = [part for part in (f"pins={pin_count}" if pin_count else "", footprint, f"type={bga_type}" if bga_type != "unknown" else "") if part]
        selection.append(
            {
                "label": refdes,
                "value": refdes,
                "detail": " ".join(detail_parts) or refdes,
                "componentId": refdes,
                "refdes": refdes,
                "pinCount": pin_count,
                "pincount": pin_count,
                "footprint": footprint,
                "package": footprint,
                "part": part,
                "bgaType": bga_type,
                "bgaLayoutType": bga_type,
                "typeSource": type_source,
            }
        )
    return selection


# ====== 功能：从候选 BGA 中读取前端或脚本提供的类型信息。 ======
def _bga_type(item: dict[str, Any]) -> tuple[str, str]:
    for key in ("bgaType", "bga_type", "bgaLayoutType", "layoutType", "type"):
        value = item.get(key)
        normalized = _normalize_bga_type(value)
        if normalized:
            return normalized, str(item.get("typeSource") or "provided")
    return "unknown", "missing"


# ====== 功能：统一前端传入的 BGA 类型命名。 ======
def _normalize_bga_type(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in {"rect", "rectangle", "rectangular", "矩形", "正交", "regular"}:
        return "rectangular"
    if text in {"stagger", "staggered", "交错", "错列"}:
        return "staggered"
    return text

# ====== 功能：根据用户输入和中间状态规划下一步工具调用。 ======
class PCBPlanner:
    # ====== 功能：初始化对象并保存运行所需依赖。 ======
    def __init__(self, model: PCBModel | None = None, use_model: bool = True, require_model: bool = False, config: AppConfig | None = None) -> None:
        self.model = model
        self.use_model = use_model
        self.require_model = require_model
        self.config = config

    # ====== 功能：根据当前状态生成下一步执行计划。 ======
    def plan(self, state: PCBState) -> PlannerOutput:
        entry_state = _state_with_explicit_entry(state)
        if entry_state is not None:
            plan = self._rule_plan(entry_state)
            plan["planner_source"] = "entry"
            plan["reason"] = _append_reason(plan.get("reason"), f"explicit entry module: {entry_state.get('entry_module', '')}")
            return plan
        if self.use_model and self.model is not None:
            try:
                return self._model_plan(state)
            except Exception as exc:
                if self.require_model:
                    raise RuntimeError(f"model planning required but failed: {exc}") from exc
                # 真实模型不可用或输出异常时，退回确定性规则规划，保证工具链仍可执行。
                fallback = self._rule_plan(state)
                fallback["reason"] = f"model planning failed, used deterministic fallback: {exc}"
                return fallback
        if self.require_model:
            raise RuntimeError("model planning required but PCBModel is not available")
        return self._rule_plan(state)

    # ====== 功能：使用真实模型生成计划并规范化输出。 ======
    def _model_plan(self, state: PCBState) -> PlannerOutput:
        messages = [{"role": "system", "content": planner_system_prompt()}]
        messages.extend(state.get("conversation_history", [])[-10:])
        messages.append({"role": "user", "content": _state_prompt(state)})
        result = self.model.complete(messages)
        calls, content = parse_tool_call_markup(result.content)
        if calls:
            plan = self._normalize_plan({"tool_calls": calls, "response": content}, state)
            plan["planner_source"] = "model"
            plan["model_elapsed_ms"] = result.elapsed_ms
            plan["model_usage"] = result.usage
            return plan
        try:
            parsed = json.loads(result.content)
        except json.JSONDecodeError:
            parsed = {"response": result.content}
        plan = self._normalize_plan(parsed, state)
        plan["planner_source"] = "model"
        plan["model_elapsed_ms"] = result.elapsed_ms
        plan["model_usage"] = result.usage
        if not plan.get("tool_calls") and not plan.get("response") and plan.get("action") != "finish":
            if self.require_model:
                raise RuntimeError("model returned no executable plan or response")
            fallback = self._rule_plan(state)
            fallback["reason"] = "model returned no executable plan; deterministic fallback completed tool plan"
            return fallback
        return plan

    # ====== 功能：使用确定性规则推进 fanout、reroute 或问答流程。 ======
    def _rule_plan(self, state: PCBState) -> PlannerOutput:
        text = state.get("user_input", "")
        lower = text.lower()
        workflow_state = state.get("workflow_state", "idle")
        cache = state.get("intermediate_cache", {})
        entities = _merge_entities(cache.get("fanoutParams"), cache.get("fanoutEntities"), extract_fanout_entities(text))
        explicit_entry = _entry_intent(state)

        if explicit_entry == "qa":
            return {"intent": "qa", "workflow": "pcb_qa_flow", "action": "chat", "tool_calls": [], "response": "我可以协助 PCB 工程问答、Fanout 和局部拆线重布。请描述你要处理的对象或约束。"}

        # 流程中的“解释/状态/为什么”等问题先作为上下文问答处理，不打断当前 fanout/reroute 状态。
        if explicit_entry is None and self._is_context_chat(text) and state.get("workflow_id") in {"pcb_escape_flow", "pcb_reroute_flow"}:
            return {"intent": "qa", "workflow": state.get("workflow_id", "pcb_qa_flow"), "action": "workflow_chat", "tool_calls": [], "response": "当前流程状态已保留。我可以解释刚才的步骤，也可以继续执行后续 PCB 操作。"}

        if explicit_entry is None and self._is_cancel(text) and not (workflow_state == "review" and state.get("workflow_id") == "pcb_escape_flow" and _is_reject_import_text(text)):
            return {"intent": "qa", "workflow": "idle", "action": "cancel_flow", "response": "已取消当前 PCB 流程。", "tool_calls": []}

        if explicit_entry == "reroute" or (explicit_entry is None and (self._is_reroute(text) or state.get("task_type") == "reroute" or state.get("workflow_id") == "pcb_reroute_flow")):
            # reroute 由 LangGraph 控制阶段推进：前端拆线 -> 重布 -> 导入 -> DRC/解释 -> 必要时 help_planner 兜底。
            if not cache.get("deleteTracesResult"):
                return self._with_calls("reroute", "pcb_reroute_flow", "reroute_entry", [{"name": "deleteTracesForRerouting", "arguments": {}, "timeout": 360.0}])
            if not _stage_ok(cache.get("rerouteInput")):
                return self._with_calls("reroute", "pcb_reroute_flow", "prepare_reroute_inputs", [{"name": "prepare_reroute_inputs", "arguments": {}, "timeout": 360.0}])
            if not cache.get("rerouteResult") and not _stage_ok(cache.get("rerouteContext")):
                return self._with_calls("reroute", "pcb_reroute_flow", "compress_context", [{"name": "compress_reroute_context", "arguments": {}, "timeout": 360.0}])
            if cache.get("rerouteLoopResult") and _drc_failed(cache.get("rerouteLoopResult")) and not cache.get("helpPlannerResult"):
                return self._with_calls("reroute", "pcb_reroute_flow", "help_planner", [{"name": "help_planner", "arguments": {"fallbackReason": "reroute_loop failed"}, "timeout": 900.0}])
            if not cache.get("rerouteResult"):
                return self._with_calls("reroute", "pcb_reroute_flow", "reroute_loop", [{"name": "reroute_loop", "arguments": {}, "timeout": 900.0}])
            if _drc_failed(cache.get("drcResult")) and (int(cache.get("rerouteDrcFailureCount", 0)) >= 3 or self._reroute_elapsed_limit_reached(cache)) and not cache.get("helpPlannerResult"):
                return self._with_calls("reroute", "pcb_reroute_flow", "help_planner", [{"name": "help_planner", "arguments": {"fallbackReason": "reroute DRC failed after retry/time limit"}, "timeout": 900.0}])
            if _drc_failed(cache.get("drcResult")):
                return self._with_calls("reroute", "pcb_reroute_flow", "reroute_retry", [{"name": "reroute", "arguments": {"attempt": int(cache.get("rerouteAttemptCount", 0)) + 1}, "timeout": 900.0}])
            if not cache.get("drcResult"):
                return self._with_calls("reroute", "pcb_reroute_flow", "drc", [{"name": "drc_check", "arguments": {}, "timeout": 360.0}])
            if _drc_passed(cache.get("drcResult")) and not cache.get("explainabilityReport"):
                return self._with_calls("reroute", "pcb_reroute_flow", "explainability", [{"name": "explainability_report", "arguments": {}, "timeout": 360.0}])
            import_file = self._extract_import_file(cache.get("rerouteResult"))
            if import_file and not cache.get("importLinesResult") and workflow_state != "report":
                return {"intent": "reroute", "workflow": "pcb_reroute_flow", "action": "reroute_report", "tool_calls": [], "response": "拆线重布和 DRC 检查已完成，请确认是否导入结果。"}
            if import_file and not cache.get("importLinesResult") and workflow_state == "report" and not _is_confirm_text(text):
                return {"intent": "reroute", "workflow": "pcb_reroute_flow", "action": "wait_reroute_import_confirm", "tool_calls": [], "response": "当前正在等待导入确认。请回复“确认导入”，或说明要重新重布。"}
            if import_file and not cache.get("importLinesResult"):
                return self._with_calls("reroute", "pcb_reroute_flow", "confirm_import", [{"name": "importLines", "arguments": {"filePath": import_file}, "timeout": 360.0}])
            return {"intent": "reroute", "workflow": "pcb_reroute_flow", "action": "finish", "tool_calls": [], "response": "局部拆线重布、导入、DRC 和分析报告已完成。"}


        if explicit_entry == "global_fanout" or (explicit_entry is None and (self._is_fanout(text) or state.get("task_type") == "global_fanout" or state.get("workflow_id") == "pcb_escape_flow" or (workflow_state == "result_review" and re.search(r"重新|重来|again|rerun", lower)))):
            # fanout 允许用户直接指定 U5/BGA 和线宽线距；未指定目标时必须先调用脚本提取 BGA 并展示候选。
            candidates = cache.get("bgaCandidates") if isinstance(cache.get("bgaCandidates"), list) else []
            if not cache.get("projectData") and not state.get("pcb_project"):
                return self._with_calls("global_fanout", "pcb_escape_flow", "get_project", [{"name": "getProjectData", "arguments": {}, "timeout": 360.0}])
            if not _has_selected_bga(entities) and not candidates:
                return self._with_calls("global_fanout", "pcb_escape_flow", "extract_bga", [{"name": "pcb_extra_bga", "arguments": {}, "timeout": 180.0}])
            if not _has_selected_bga(entities) and candidates:
                names = ", ".join(str(item.get("refdes") or item.get("reference") or "") for item in candidates[:10])
                return {"intent": "global_fanout", "workflow": "pcb_escape_flow", "action": "select_bga", "tool_calls": [], "response": f"检测到 BGA/高引脚器件：{names}。请选择要逃逸布线的器件。", "entities": entities, "selection": _bga_selection(candidates)}
            if _has_selected_bga(entities) and not entities.get("routerType"):
                return {"intent": "global_fanout", "workflow": "pcb_escape_flow", "action": "router_type_prompt", "tool_calls": [], "response": "已确认目标 BGA。请选择逃逸布线器：135 或 arc。", "entities": entities}
            if not cache.get("layerAssignResult"):
                return self._with_calls("global_fanout", "pcb_escape_flow", "layer_assign", [{"name": "layer_assign", "arguments": _fanout_args(entities), "timeout": 360.0}])
            if not cache.get("escapeOrderResult"):
                return self._with_calls("global_fanout", "pcb_escape_flow", "escape_order", [{"name": "escape_order", "arguments": _fanout_args(entities), "timeout": 360.0}])
            if not cache.get("fanout_routeResult") and workflow_state != "param_review":
                return {"intent": "global_fanout", "workflow": "pcb_escape_flow", "action": "fanout_params_review", "tool_calls": [], "response": "已生成逃逸层分配和逃逸顺序参数，请确认后开始布线。", "entities": entities}
            if not cache.get("fanout_routeResult") and workflow_state == "param_review" and not cache.get("fanoutParamsConfirmed") and not _is_confirm_text(text):
                return {"intent": "global_fanout", "workflow": "pcb_escape_flow", "action": "wait_fanout_params_confirm", "tool_calls": [], "response": "当前正在等待 fanout 参数确认。请在参数面板确认/修改后提交，或回复“确认”开始布线。", "entities": entities}
            if not cache.get("fanout_routeResult"):
                return self._with_calls("global_fanout", "pcb_escape_flow", "route", [{"name": "fanout_route", "arguments": _fanout_args(entities), "timeout": 1800.0}])
            if cache.get("importLinesRejected"):
                return {"intent": "global_fanout", "workflow": "pcb_escape_flow", "action": "cancel_import", "tool_calls": [], "response": "已取消导入，fanout 结果保留在文件中。", "entities": entities}
            import_file = self._extract_import_file(cache.get("fanout_routeResult"))
            if import_file and not cache.get("importLinesResult"):
                return self._with_calls("global_fanout", "pcb_escape_flow", "import", [{"name": "importLines", "arguments": {"filePath": import_file, "successPins": [], "failedPins": []}, "timeout": 360.0}])
            if cache.get("importLinesResult"):
                return {"intent": "global_fanout", "workflow": "pcb_escape_flow", "action": "finish", "tool_calls": [], "response": "Fanout 结果已导入，请在 PCB 版图中确认结果。"}
            return {"intent": "global_fanout", "workflow": "pcb_escape_flow", "action": "finish", "tool_calls": [], "response": "Fanout 布线已完成，结果尚未导入。"}
        if "drc" in lower or "解释" in text or "为什么" in text or "report" in lower:
            if cache.get("drcResult") and cache.get("explainabilityReport") and state.get("task_type") == "qa":
                return {"intent": "qa", "workflow": "pcb_qa_flow", "action": "finish", "tool_calls": [], "response": "我会检查当前 DRC 结果并生成说明。"}
            calls = [{"name": "drc_check", "arguments": {}, "timeout": 360.0}, {"name": "explainability_report", "arguments": {}, "timeout": 360.0}]
            return self._with_calls("qa", "pcb_qa_flow", "explain", calls, response="我会检查当前 DRC 结果并生成说明。")

        return {"intent": "qa", "workflow": "pcb_qa_flow", "action": "chat", "tool_calls": [], "response": "我可以协助 PCB 工程问答、Fanout 和局部拆线重布。请描述你要处理的对象或约束。"}

    # ====== 功能：把模型或规则输出整理成统一 PlannerOutput。 ======
    def _normalize_plan(self, data: dict[str, Any], state: PCBState) -> PlannerOutput:
        text = state.get("user_input", "")
        cache = state.get("intermediate_cache", {}) or {}
        model_entities = _entities_from_model_data(data)
        text_entities = extract_fanout_entities(text)
        cached_entities = cache.get("fanoutEntities") or cache.get("fanoutParams") or {}
        entities = _merge_entities(cached_entities, model_entities, data.get("entities"), text_entities)

        calls = []
        for raw in data.get("tool_calls") or data.get("toolCalls") or []:
            if not isinstance(raw, dict) or not raw.get("name"):
                continue
            calls.append(self._tool_call(_normalize_model_tool_call(raw, entities)))

        intent = data.get("intent") or data.get("task_type") or self._infer_intent(calls, state.get("user_input", ""))
        if intent == "qa" and (state.get("workflow_id") == "pcb_escape_flow" or _has_fanout_signal(entities, text)):
            intent = "global_fanout"
        workflow = _normalize_workflow(data.get("workflow") or data.get("workflow_id") or self._workflow_for_intent(intent), intent)
        if not calls:
            calls, action = self._repair_model_calls(intent, workflow, entities, state)
        else:
            action = data.get("action") or "plan"
        plan = {
            "intent": intent,
            "workflow": workflow,
            "action": action,
            "tool_calls": calls,
            "response": data.get("response") or data.get("content") or "",
            "reason": data.get("reason") or "",
            "entities": entities,
        }
        return validate_next_step(state, plan, self)

    # ====== 功能：当模型只返回实体参数时，根据 LangGraph 状态补出下一步工具调用。 ======
    def _repair_model_calls(self, intent: str, workflow: str, entities: dict[str, Any], state: PCBState) -> tuple[list[ToolCall], str]:
        cache = state.get("intermediate_cache", {}) or {}
        if intent == "global_fanout" or workflow == "pcb_escape_flow":
            args = _fanout_args(entities)
            if not cache.get("projectData") and not state.get("pcb_project"):
                return [self._tool_call({"name": "getProjectData", "arguments": args, "timeout": 360.0})], "get_project"
            candidates = cache.get("bgaCandidates") if isinstance(cache.get("bgaCandidates"), list) else []
            if not _has_selected_bga(entities) and not candidates:
                return [self._tool_call({"name": "pcb_extra_bga", "arguments": {}, "timeout": 180.0})], "extract_bga"
            if not _has_selected_bga(entities) and candidates:
                return [], "select_bga"
            if _has_selected_bga(entities) and not entities.get("routerType"):
                return [], "router_type_prompt"
            if not cache.get("layerAssignResult"):
                return [self._tool_call({"name": "layer_assign", "arguments": args, "timeout": 360.0})], "layer_assign"
            if not cache.get("escapeOrderResult"):
                return [self._tool_call({"name": "escape_order", "arguments": args, "timeout": 360.0})], "escape_order"
            if not cache.get("fanout_routeResult"):
                return [], "fanout_params_review"
            if cache.get("importLinesRejected"):
                return [], "cancel_import"
            import_file = self._extract_import_file(cache.get("fanout_routeResult"))
            if import_file and not cache.get("importLinesResult"):
                return [self._tool_call({"name": "importLines", "arguments": {"filePath": import_file, "successPins": [], "failedPins": []}, "timeout": 360.0})], "import"
            return [], "finish"

        if intent == "reroute" or workflow == "pcb_reroute_flow":
            if not cache.get("deleteTracesResult"):
                return [self._tool_call({"name": "deleteTracesForRerouting", "arguments": {}, "timeout": 360.0})], "reroute_entry"
            if not _stage_ok(cache.get("rerouteInput")):
                return [self._tool_call({"name": "prepare_reroute_inputs", "arguments": {}, "timeout": 360.0})], "prepare_reroute_inputs"
            if not cache.get("rerouteResult") and not _stage_ok(cache.get("rerouteContext")):
                return [self._tool_call({"name": "compress_reroute_context", "arguments": {}, "timeout": 360.0})], "compress_context"
            if cache.get("rerouteLoopResult") and _drc_failed(cache.get("rerouteLoopResult")) and not cache.get("helpPlannerResult"):
                return [self._tool_call({"name": "help_planner", "arguments": {"fallbackReason": "reroute_loop failed"}, "timeout": 900.0})], "help_planner"
            if not cache.get("rerouteResult"):
                return [self._tool_call({"name": "reroute_loop", "arguments": {}, "timeout": 900.0})], "reroute_loop"
            if _drc_failed(cache.get("drcResult")) and (int(cache.get("rerouteDrcFailureCount", 0)) >= 3 or self._reroute_elapsed_limit_reached(cache)) and not cache.get("helpPlannerResult"):
                return [self._tool_call({"name": "help_planner", "arguments": {"fallbackReason": "reroute DRC failed after retry/time limit"}, "timeout": 900.0})], "help_planner"
            if _drc_failed(cache.get("drcResult")):
                return [self._tool_call({"name": "reroute", "arguments": {"attempt": int(cache.get("rerouteAttemptCount", 0)) + 1}, "timeout": 900.0})], "reroute_retry"
            if not cache.get("drcResult"):
                return [self._tool_call({"name": "drc_check", "arguments": {}, "timeout": 360.0})], "drc"
            if _drc_passed(cache.get("drcResult")) and not cache.get("explainabilityReport"):
                return [self._tool_call({"name": "explainability_report", "arguments": {}, "timeout": 360.0})], "explainability"
            import_file = self._extract_import_file(cache.get("rerouteResult"))
            if import_file and not cache.get("importLinesResult"):
                return [], "reroute_report"
            return [], "finish"

        return [], "plan"

    # ====== 功能：构造包含工具调用的规划结果。 ======
    def _with_calls(self, intent: str, workflow: str, action: str, calls: list[dict[str, Any]], response: str = "") -> PlannerOutput:
        return {"intent": intent, "workflow": workflow, "action": action, "tool_calls": [self._tool_call(call) for call in calls], "response": response, "reason": "deterministic intent rule", "planner_source": "rule"}

    @staticmethod
    # ====== 功能：把原始工具调用字段标准化。 ======
    def _tool_call(raw: dict[str, Any]) -> ToolCall:
        return {
            "id": str(raw.get("id") or uuid.uuid4()),
            "name": str(raw["name"]),
            "arguments": dict(raw.get("arguments") or {}),
            "timeout": float(raw.get("timeout", 360.0)),
        }

    def _reroute_elapsed_limit_reached(self, cache: dict[str, Any]) -> bool:
        started = cache.get("rerouteStartedAt")
        try:
            started_at = float(started)
        except (TypeError, ValueError):
            return False
        import time

        limit = 900
        if self.config is not None:
            limit = int(getattr(self.config.reroute_help, "max_elapsed_seconds", 900) or 900)
        return limit > 0 and (time.time() - started_at) >= limit

    @staticmethod
    # ====== 功能：从工具结果中提取可导入的布线文件路径。 ======
    def _extract_import_file(result: Any) -> str:
        if not isinstance(result, dict):
            return ""
        for key in ("importLinesFilePath", "routedLayoutTxtFilePath", "routingResult"):
            value = str(result.get(key) or "").strip()
            if value:
                return value
        nested = result.get("rerouteResult") or result.get("fanoutResult")
        if isinstance(nested, dict) and nested is not result and nested:
            return PCBPlanner._extract_import_file(nested)
        return ""


    @staticmethod
    # ====== 功能：根据任务意图选择 LangGraph workflow。 ======
    def _workflow_for_intent(intent: str) -> str:
        return {"global_fanout": "pcb_escape_flow", "reroute": "pcb_reroute_flow", "qa": "pcb_qa_flow"}.get(intent, "idle")

    @staticmethod
    # ====== 功能：根据工具调用和用户文本推断任务意图。 ======
    def _infer_intent(calls: list[ToolCall], text: str) -> str:
        names = {call["name"] for call in calls}
        if {"deleteTracesForRerouting", "reroute"} & names:
            return "reroute"
        if {"fanout_route", "layer_assign", "escape_order", "getProjectData"} & names:
            return "global_fanout"
        if re.search(r"fanout|逃逸|扇出", text, re.IGNORECASE):
            return "global_fanout"
        if re.search(r"reroute|拆线|重布|局部", text, re.IGNORECASE):
            return "reroute"
        return "qa"

    @staticmethod
    # ====== 功能：判断输入是否为流程中的解释或状态追问。 ======
    def _is_context_chat(text: str) -> bool:
        return bool(re.search(r"解释|为什么|刚才|当前|状态|进度|说明|怎么|what|why|status|progress", text, re.IGNORECASE))


    @staticmethod
    # ====== 功能：判断输入是否触发 fanout/逃逸布线。 ======
    def _is_fanout(text: str) -> bool:
        return bool(re.search(r"fanout|逃逸|扇出|BGA.*布线|布线.*BGA", text, re.IGNORECASE))

    @staticmethod
    # ====== 功能：判断输入是否触发局部拆线重布。 ======
    def _is_reroute(text: str) -> bool:
        return bool(re.search(r"reroute|拆线重布|局部.*重布|重新布.*这|选中.*线|rip-?up", text, re.IGNORECASE))

    @staticmethod
    # ====== 功能：判断输入是否表达继续或确认。 ======
    def _is_confirm(text: str) -> bool:
        return bool(re.search(r"确认|继续|开始|可以|yes|ok|执行", text, re.IGNORECASE))

    @staticmethod
    # ====== 功能：判断输入是否取消当前流程。 ======
    def _is_cancel(text: str) -> bool:
        return bool(re.search(r"取消|终止|不做了|结束|cancel|stop", text, re.IGNORECASE))
# ====== 功能：判断模型实体和用户文本是否足以确认 fanout 流程。 ======
# ====== 功能：判断用户是否明确拒绝导入 fanout 结果。 ======
def _is_reject_import_text(text: str) -> bool:
    return bool(re.search(r"取消导入|不导入|拒绝导入|先不导入|不要导入|cancel import", text, re.IGNORECASE))

def _has_fanout_signal(entities: dict[str, Any], text: Any) -> bool:
    source = str(text or "").lower()
    has_route_word = any(word in source for word in ("fanout", "escape", "route", "routing", "135", "arc"))
    has_route_word = has_route_word or any(word in str(text or "") for word in ("逃逸", "扇出", "布线", "走线"))
    has_fanout_entity = any(entities.get(key) not in (None, "", [], {}) for key in ("selectedBGA", "targetBGA", "targetBGAs", "routerType"))
    has_constraint = bool((entities.get("constraints") or {}).get("LineWidth") or (entities.get("constraints") or {}).get("LineSpacing"))
    return has_fanout_entity and (has_route_word or has_constraint)

# ====== 功能：从模型返回中兼容提取 fanout 实体参数。 ======
def _entities_from_model_data(data: dict[str, Any]) -> dict[str, Any]:
    entities: dict[str, Any] = {}
    if not isinstance(data, dict):
        return entities
    nested = data.get("entities") if isinstance(data.get("entities"), dict) else {}
    constraints = _normalize_constraints(nested.get("constraints") if isinstance(nested, dict) else {})

    key_map = {
        "selectedBGA": "selectedBGA",
        "targetBGA": "targetBGA",
        "targetBGAs": "targetBGAs",
        "bga": "selectedBGA",
        "BGA": "selectedBGA",
        "component": "selectedBGA",
        "refdes": "selectedBGA",
    }
    for source_key, target_key in key_map.items():
        value = data.get(source_key, nested.get(source_key) if isinstance(nested, dict) else None)
        if value not in (None, "", [], {}):
            if target_key in {"selectedBGA", "targetBGA"}:
                entities[target_key] = str(value).upper()
            else:
                entities[target_key] = value

    router_type = data.get("routerType") or data.get("router") or data.get("route_type") or (nested.get("routerType") if isinstance(nested, dict) else "")
    router_type = normalize_router_type(router_type) or str(router_type or "")
    if router_type:
        entities["routerType"] = router_type

    constraints.update(_normalize_constraints(data))
    if constraints:
        entities["constraints"] = constraints
    return entities


# ====== 功能：把模型返回的 workflow 名称归一化为 LangGraph 内部名称。 ======
def _normalize_workflow(value: Any, intent: str) -> str:
    workflow = str(value or "").strip()
    if workflow in {"pcb_escape_flow", "pcb_reroute_flow", "pcb_qa_flow", "idle"}:
        return workflow
    if re.search(r"fanout|escape|逃逸|扇出", workflow, re.IGNORECASE):
        return "pcb_escape_flow"
    if re.search(r"reroute|重布|拆线", workflow, re.IGNORECASE):
        return "pcb_reroute_flow"
    return PCBPlanner._workflow_for_intent(intent)


# ====== 功能：读取显式入口按钮字段并生成 planner 使用的状态副本。 ======
def _state_with_explicit_entry(state: PCBState) -> PCBState | None:
    entry = _entry_task_from_state(state)
    if entry is None:
        return None
    task_type, workflow_id, module = entry
    copied = dict(state)
    copied["task_type"] = task_type
    copied["workflow_id"] = workflow_id
    copied["entry_module"] = module
    return copied


# ====== 功能：从 state 或 entry_payload 中解析显式入口意图。 ======
def _entry_intent(state: PCBState) -> str | None:
    entry = _entry_task_from_state(state)
    return entry[0] if entry else None


# ====== 功能：兼容 state.entry_module 和 payload.module/chain/workflow 等入口字段。 ======
def _entry_task_from_state(state: PCBState) -> tuple[str, str, str] | None:
    payload = state.get("entry_payload") if isinstance(state.get("entry_payload"), dict) else {}
    candidates = [state.get("entry_module")]
    candidates.extend(payload.get(key) for key in ENTRY_MODULE_KEYS)
    for value in candidates:
        mapped = entry_task_workflow(value)
        if mapped:
            return mapped[0], mapped[1], normalize_entry_module(value)
    return None


# ====== 功能：修正模型工具调用名称和参数结构。 ======
def _normalize_model_tool_call(raw: dict[str, Any], entities: dict[str, Any]) -> dict[str, Any]:
    call = dict(raw)
    name = str(call.get("name") or "")
    aliases = {
        "get_project_data": "getProjectData",
        "get_project": "getProjectData",
        "layerAssign": "layer_assign",
        "escapeOrder": "escape_order",
        "fanoutRoute": "fanout_route",
        "fanout": "fanout_route",
        "drc": "drc_check",
    }
    call["name"] = aliases.get(name, name)
    args = dict(call.get("arguments") or {})
    if call["name"] in {"layer_assign", "escape_order", "fanout_route"}:
        args = _merge_entities(args, _fanout_args(entities))
    call["arguments"] = args
    return call


# ====== 功能：从模型字段中提取并统一线宽线距约束。 ======
def _normalize_constraints(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {}
    constraints: dict[str, Any] = {}
    source = data.get("constraints") if isinstance(data.get("constraints"), dict) else data
    width = source.get("LineWidth") or source.get("lineWidth") or source.get("width") or source.get("trace_width")
    spacing = source.get("LineSpacing") or source.get("lineSpacing") or source.get("spacing") or source.get("clearance")
    if width not in (None, ""):
        constraints["LineWidth"] = _numeric_or_original(width)
    if spacing not in (None, ""):
        constraints["LineSpacing"] = _numeric_or_original(spacing)
    return constraints


# ====== 功能：把数字字符串转为 int/float，无法转换时保留原值。 ======
def _numeric_or_original(value: Any) -> Any:
    if isinstance(value, (int, float)):
        return value
    text = str(value).strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    if not match:
        return value
    number = float(match.group(0))
    return int(number) if number.is_integer() else number

# ====== 功能：为真实模型提供当前 LangGraph 状态摘要。 ======
def _state_prompt(state: PCBState) -> str:
    cache = state.get("intermediate_cache", {}) or {}
    summary = {
        "mode": "STRICT_LANGGRAPH_PLANNER_STATE",
        "instruction": "根据当前状态只规划下一步。不要重复已经完成的工具。必须返回 JSON 或 tool_call。",
        "workflow_id": state.get("workflow_id", "idle"),
        "workflow_state": state.get("workflow_state", "idle"),
        "task_type": state.get("task_type", "unknown"),
        "completed": {
            "projectData": bool(cache.get("projectData") or state.get("pcb_project")),
            "layerAssignResult": bool(cache.get("layerAssignResult")),
            "escapeOrderResult": bool(cache.get("escapeOrderResult")),
            "fanoutRouteResult": bool(cache.get("fanout_routeResult")),
            "importLinesResult": bool(cache.get("importLinesResult")),
            "drcResult": bool(cache.get("drcResult")),
            "deleteTracesResult": bool(cache.get("deleteTracesResult")),
            "rerouteResult": bool(cache.get("rerouteResult")),
            "helpPlannerResult": bool(cache.get("helpPlannerResult")),
        },
        "fanoutEntities": cache.get("fanoutEntities") or cache.get("fanoutParams") or {},
        "next_step_rules": [
            "global_fanout 顺序：getProjectData -> pcb_extra_bga/selection -> router选择(135或arc) -> layer_assign -> escape_order -> fanoutParams确认 -> fanout_route -> importLines(由前端工具审批确认/拒绝) -> result_review(导入成功后生成本轮work_dir报告)；fanout 不调用 DRC/Explainability",
            "reroute 顺序：deleteTracesForRerouting -> prepare_reroute_inputs -> compress_reroute_context -> reroute_loop(VSEA内部模型/fill/hard DRC/repair) -> explainability_report -> report确认 -> importLines；reroute_loop失败直接 help_planner 兜底；默认不调用旧reroute/drc_check",
            "用户指定 U5/U22 等器件时直接作为 selectedBGA，不要再要求选择 BGA",
            "用户指定线宽线距时写入 entities.constraints.LineWidth 和 LineSpacing，并传给工具 arguments.constraints",
        ],
    }
    return "Current LangGraph planning state:\n" + json.dumps(summary, ensure_ascii=False)
















# ====== 功能：把模型 planner 输出约束到当前 LangGraph 状态允许的合法下一步。 ======
def validate_next_step(state: PCBState, plan: PlannerOutput, planner: PCBPlanner) -> PlannerOutput:
    workflow = str(plan.get("workflow") or "")
    if workflow not in {"pcb_escape_flow", "pcb_reroute_flow"}:
        return plan

    legal = planner._rule_plan(_state_with_model_entities(state, plan))
    if not _plan_step_matches(plan, legal):
        rewritten = dict(legal)
        rewritten["planner_source"] = plan.get("planner_source", "model")
        rewritten["model_action"] = plan.get("action")
        rewritten["model_tool_calls"] = plan.get("tool_calls") or []
        rewritten["reason"] = _append_reason(plan.get("reason"), "model step was outside legal LangGraph transition; rewritten by validate_next_step")
        if plan.get("entities"):
            rewritten["entities"] = plan.get("entities")
            safe_entities = (_state_with_model_entities(state, plan).get("intermediate_cache", {}) or {}).get("fanoutEntities") or {}
            rewritten["tool_calls"] = [_normalize_validated_call(call, safe_entities) for call in rewritten.get("tool_calls", [])]
        return rewritten

    checked = dict(plan)
    safe_entities = (_state_with_model_entities(state, plan).get("intermediate_cache", {}) or {}).get("fanoutEntities") or {}
    checked["tool_calls"] = [_normalize_validated_call(call, safe_entities) for call in checked.get("tool_calls", [])]
    checked["validation"] = "legal"
    return checked


# ====== 功能：让规则状态机在校验模型计划时看到模型抽取到的实体。 ======
def _state_with_model_entities(state: PCBState, plan: PlannerOutput) -> PCBState:
    copied = dict(state)
    cache = dict(copied.get("intermediate_cache", {}) or {})
    # 合法转移只信任历史 cache 和用户明文抽取到的 BGA/router；模型猜测的 BGA/router 不能跳过 v0.6 交互停点。
    text_entities = extract_fanout_entities(copied.get("user_input", ""))
    model_entities = dict(plan.get("entities") or {})
    model_constraints = model_entities.get("constraints") if isinstance(model_entities.get("constraints"), dict) else {}
    entities = _merge_entities(cache.get("fanoutEntities"), text_entities)
    if model_constraints:
        entities = _merge_entities(entities, {"constraints": model_constraints})
    if entities:
        cache["fanoutEntities"] = entities
    copied["intermediate_cache"] = cache
    return copied


# ====== 功能：判断模型建议和规则合法下一步是否等价。 ======
def _plan_step_matches(plan: PlannerOutput, legal: PlannerOutput) -> bool:
    plan_names = [str(call.get("name") or "") for call in plan.get("tool_calls", [])]
    legal_names = [str(call.get("name") or "") for call in legal.get("tool_calls", [])]
    if plan_names or legal_names:
        return plan_names == legal_names
    return str(plan.get("action") or "") == str(legal.get("action") or "")


# ====== 功能：校验后给 fanout 工具调用补齐模型抽取的实体参数。 ======
def _normalize_validated_call(call: ToolCall, entities: dict[str, Any]) -> ToolCall:
    if not entities or call.get("name") not in {"layer_assign", "escape_order", "fanout_route"}:
        return call
    merged = dict(call)
    args = dict(call.get("arguments") or {})
    for key, value in _fanout_args(entities).items():
        if value not in (None, "", [], {}) and key not in args:
            args[key] = value
    merged["arguments"] = args
    return merged


# ====== 功能：追加 planner 诊断原因。 ======
def _append_reason(existing: Any, note: str) -> str:
    text = str(existing or "").strip()
    return f"{text}; {note}" if text else note
