from __future__ import annotations

import asyncio
from typing import Any

from pcb_agent_langgraph.graph.state import PCBState, add_trace
from pcb_agent_langgraph.planner.planner import PCBPlanner
from pcb_agent_langgraph.reports.markdown import build_fanout_route_report, build_markdown_report
from pcb_agent_langgraph.tools.base import Tool, invoke_tool
from pcb_agent_langgraph.tools.registry import tool_context_from_state
from pcb_agent_langgraph.tools.frontend import ProgressSender


# ====== 功能：实现 PCB LangGraph 的各个节点处理逻辑。 ======
class GraphNodes:
    # 每个方法对应 LangGraph 的一个节点，节点只改状态，不把旧 Agent 流程逻辑塞进工具。
    # ====== 功能：初始化对象并保存运行所需依赖。 ======
    def __init__(self, planner: PCBPlanner, tools: dict[str, Tool], *, progress_sender: ProgressSender | None = None) -> None:
        self.planner = planner
        self.tools = tools
        self.progress_sender = progress_sender

    # ====== 功能：记录意图识别节点进入状态。 ======
    async def intent(self, state: PCBState) -> dict[str, Any]:
        return {
            "current_stage": "intent",
            "loop_count": int(state.get("loop_count", 0)) + 1,
            **add_trace(state, "intent", {"user_input": state.get("user_input", "")}),
        }

    # ====== 功能：根据当前状态生成下一步执行计划。 ======
    async def plan(self, state: PCBState) -> dict[str, Any]:
        plan = self.planner.plan(state)
        return {
            "current_stage": "planning",
            "loop_count": int(state.get("loop_count", 0)) + 1,
            "task_type": plan.get("intent", "unknown"),
            "workflow_id": plan.get("workflow", "idle"),
            "planner_output": plan,
            "tool_calls": list(plan.get("tool_calls", [])),
            **add_trace(state, "plan", {"planner_output": plan}),
        }

    # ====== 功能：执行 planner 生成的工具调用并更新中间缓存。 ======
    async def execute_tools(self, state: PCBState) -> dict[str, Any]:
        records = list(state.get("tool_history", []))
        results = dict(state.get("tool_results", {}))
        cache = dict(state.get("intermediate_cache", {}))

        # planner 产出的 tool_calls 在这里统一执行，并把结果写入 cache 供下一轮规划使用。
        for call in state.get("tool_calls", []):
            call = _merge_call_with_entities(call, state.get("planner_output", {}).get("entities") or {})
            tool = self.tools.get(call["name"])
            if tool is None:
                record = {"call": call, "result": None, "ok": False, "elapsed_ms": 0.0, "error": f"Tool not registered: {call['name']}"}
            else:
                context = tool_context_from_state(state, call["id"], float(call.get("timeout", 360.0)))
                record = await self._invoke_with_progress(tool, call, context)
            records.append(record)
            results[call["id"]] = record.get("result")
            _update_cache_from_tool(cache, call["name"], record.get("result"))

        return {
            "current_stage": "tool_execution",
            "tool_history": records,
            "tool_results": results,
            "intermediate_cache": cache,
            "tool_calls": [],
            **add_trace(state, "execute_tools", {"executed": [call["name"] for call in state.get("tool_calls", [])]}),
        }

    # ====== 功能：执行工具时向前端发送进度和心跳。 ======
    async def _invoke_with_progress(self, tool: Tool, call: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        session_id = str(context.get("session_id") or "")
        tool_name = str(call.get("name") or "工具")
        is_frontend_tool = tool_name in {"getProjectData", "importLines", "deleteTracesForRerouting"}
        if self.progress_sender and session_id and not is_frontend_tool:
            await self.progress_sender(session_id, f"正在调用 {tool_name}...")
        task = asyncio.create_task(invoke_tool(tool, call, context))
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError:
                if self.progress_sender and session_id and not is_frontend_tool:
                    await self.progress_sender(session_id, f"{tool_name} 执行中...")
        record = await task
        if self.progress_sender and session_id and not is_frontend_tool:
            suffix = "已完成，继续处理..." if record.get("ok") and not _result_failed(record.get("result")) else "返回失败，正在整理原因..."
            await self.progress_sender(session_id, f"{tool_name} {suffix}")
        return record
    # ====== 功能：根据工具结果生成当前轮回复和流程状态。 ======
    async def reflect(self, state: PCBState) -> dict[str, Any]:
        task_type = state.get("task_type", "unknown")
        tool_history = state.get("tool_history", [])
        failed = [item for item in tool_history if not item.get("ok") or _result_failed(item.get("result"))]
        cache = state.get("intermediate_cache", {}) or {}
        final_drc_passed = isinstance(cache.get("drcResult"), dict) and cache.get("drcResult", {}).get("passed") is True
        planner_output = state.get("planner_output", {}) or {}
        action = str(planner_output.get("action") or "")
        selection = planner_output.get("selection") if isinstance(planner_output.get("selection"), list) else None
        fanout_params = cache.get("fanoutParams") if isinstance(cache.get("fanoutParams"), dict) else None
        if action == "select_bga":
            message = planner_output.get("response") or "检测到多个 BGA/高引脚器件，请选择要逃逸布线的器件。"
            workflow_state = "select_bga"
        elif action == "router_type_prompt":
            message = planner_output.get("response") or "请选择逃逸布线器：135 或 arc。"
            workflow_state = "wait_router_type"
        elif action in {"fanout_params_review", "wait_fanout_params_confirm"}:
            message = planner_output.get("response") or "已生成逃逸参数，请确认后开始布线。"
            workflow_state = "param_review"
        elif action in {"route_review", "wait_route_import_confirm"}:
            message = planner_output.get("response") or "逃逸布线已生成，是否导入到 PCB 版图？请回复“确认导入”或“取消导入”。"
            workflow_state = "review"
        elif action in {"reroute_context_ready", "wait_reroute_confirm"}:
            message = planner_output.get("response") or "已完成拆线重布上下文压缩，请确认是否开始局部重布。"
            workflow_state = "confirm"
        elif action in {"reroute_report", "wait_reroute_import_confirm"}:
            message = planner_output.get("response") or "拆线重布和 DRC 检查已完成，请确认是否导入结果。"
            workflow_state = "report"
        elif action == "cancel_import":
            message = planner_output.get("response") or "已取消导入，fanout 结果保留在文件中。"
            workflow_state = "review"
        elif failed and not final_drc_passed:
            message = _failure_message(task_type, failed[-1])
            workflow_state = "error"
        elif task_type == "global_fanout":
            if cache.get("importLinesResult"):
                message = "Fanout 结果已导入，请在 PCB 版图中确认结果，或回复重新 fanout。"
                workflow_state = "result_review"
            else:
                message = planner_output.get("response") or "Fanout 流程已执行到当前交互步骤。"
                workflow_state = state.get("workflow_state", "select_bga")
        elif task_type == "reroute":
            if cache.get("importLinesResult"):
                message = "局部拆线重布结果已导入，请在 PCB 版图中确认结果。"
                workflow_state = "import"
            else:
                message = planner_output.get("response") or "局部拆线重布流程已执行到当前交互步骤。"
                workflow_state = state.get("workflow_state", "report")
        else:
            message = state.get("planner_output", {}).get("response") or "已完成当前 PCB 问答处理。"
            workflow_state = "idle"
        report_payload: dict[str, Any] = {}
        markdown_report = ""
        if task_type == "global_fanout" and cache.get("importLinesResult"):
            report_payload = build_fanout_route_report(cache)
            message = str(report_payload.get("report") or report_payload.get("markdown") or message)
        elif action in {"route_review", "wait_route_import_confirm"}:
            route = cache.get("fanout_routeResult") if isinstance(cache.get("fanout_routeResult"), dict) else {}
            report_payload = {"task": "global_fanout", "stage": "import_pending", "routingResult": route.get("routingResult"), "importLinesFilePath": route.get("importLinesFilePath"), "workDir": route.get("workDir")}
        elif action in {"reroute_report", "wait_reroute_import_confirm"} or (task_type == "reroute" and cache.get("drcResult")):
            report_payload = build_markdown_report(task_type, cache)
            markdown_report = str(report_payload.get("markdown") or "")
            if markdown_report:
                message = markdown_report
        return {
            "current_stage": "reflection",
            "workflow_state": workflow_state,
            "final_response": message,
            "markdown_report": markdown_report,
            "report_payload": report_payload,
            "selection": selection,
            "fanout_params": fanout_params if action in {"fanout_params_review", "wait_fanout_params_confirm"} else None,
            **add_trace(state, "reflect", {"workflow_state": workflow_state, "response": message, "report_payload": report_payload}),
        }

    # ====== 功能：收尾当前图执行并写入对话历史。 ======
    async def finish(self, state: PCBState) -> dict[str, Any]:
        history = list(state.get("conversation_history", []))
        if state.get("final_response"):
            history.append({"role": "assistant", "content": state["final_response"]})
        return {"current_stage": "finished", "conversation_history": history, **add_trace(state, "finish", {"final": True})}


# ====== 功能：决定计划节点之后进入工具执行还是反思节点。 ======
def route_after_plan(state: PCBState) -> str:
    if state.get("tool_calls"):
        return "execute_tools"
    return "reflect"


# ====== 功能：决定工具执行后继续规划还是结束反思。 ======
def route_after_tools(state: PCBState) -> str:
    # 工具成功后回到 plan；reroute 的 DRC 失败可恢复，需要继续回 planner 触发重试或 help_planner。
    if int(state.get("loop_count", 0)) > 18:
        return "reflect"
    failed = [item for item in state.get("tool_history", []) if not item.get("ok") or _result_failed(item.get("result"))]
    if failed and not _only_recoverable_drc_failures(failed, state):
        return "reflect"
    return "plan"


# ====== 功能：判断失败是否属于 reroute DRC-loop 可恢复失败。 ======
def _only_recoverable_drc_failures(failed: list[dict[str, Any]], state: PCBState) -> bool:
    if state.get("workflow_id") != "pcb_reroute_flow":
        return False
    allowed = {"drc_check", "explainability_report"}
    for item in failed:
        call = item.get("call", {}) if isinstance(item, dict) else {}
        if call.get("name") not in allowed:
            return False
        result = item.get("result") if isinstance(item, dict) else {}
        if not isinstance(result, dict) or str(result.get("status", "")).lower() not in {"failed", "error"}:
            return False
    return bool(failed)

# ====== 功能：把 planner 抽取实体补入前端或 fanout 工具参数。 ======
def _merge_call_with_entities(call: dict[str, Any], entities: dict[str, Any]) -> dict[str, Any]:
    if call.get("name") not in {"layer_assign", "escape_order", "fanout_route"}:
        return call
    args = dict(call.get("arguments") or {})
    for key in ("selectedBGA", "targetBGA", "targetBGAs", "routerType", "constraints"):
        value = entities.get(key) if isinstance(entities, dict) else None
        if value not in (None, "", [], {}) and key not in args:
            args[key] = value
    merged = dict(call)
    merged["arguments"] = args
    return merged


# ====== 功能：按工具名称把结果写入后续节点需要的缓存。 ======
def _update_cache_from_tool(cache: dict[str, Any], tool_name: str, result: Any) -> None:
    # cache 是多轮 fanout/reroute 的共享工作台，只保存后续节点真正需要的中间结果。
    if tool_name == "getProjectData":
        cache["projectData"] = result
        if isinstance(result, str):
            cache["boardData"] = result
        elif isinstance(result, dict):
            cache["boardData"] = result.get("boardData") or result.get("data") or result.get("content") or cache.get("boardData", "")
    elif tool_name == "deleteTracesForRerouting":
        cache["deleteTracesResult"] = result
        if isinstance(result, dict):
            cache["projectData"] = result.get("projectData") or result.get("project_data") or result.get("boardData") or cache.get("projectData")
            missing_routes = result.get("missing_routes") or result.get("missingRoutes") or []
            if isinstance(missing_routes, list):
                cache["missingRoutes"] = missing_routes
                nets = _nets_from_routes(missing_routes)
                if nets:
                    cache["selectedNets"] = nets
    elif tool_name == "layer_assign":
        cache["layerAssignResult"] = result
        if isinstance(result, dict) and isinstance(result.get("fanoutParams"), dict):
            cache["fanoutParams"] = result.get("fanoutParams")
            cache["fanoutEntities"] = {**dict(cache.get("fanoutEntities") or {}), **{k: v for k, v in result.get("fanoutParams", {}).items() if k in {"selectedBGA", "routerType", "constraints"}}}
    elif tool_name == "escape_order":
        cache["escapeOrderResult"] = result
        if isinstance(result, dict) and isinstance(result.get("fanoutParams"), dict):
            cache["fanoutParams"] = result.get("fanoutParams")
    elif tool_name == "importLines":
        cache["importLinesResult"] = result
    elif tool_name == "pcb_extra_bga":
        cache["bgaCandidates"] = (result or {}).get("components", []) if isinstance(result, dict) else []
    elif tool_name == "compress_reroute_context":
        cache["rerouteContext"] = result
    elif tool_name in {"fanout_route", "reroute"}:
        if tool_name == "reroute" and isinstance(result, dict) and str(result.get("status", "")).lower() == "unavailable":
            cache["rerouteUnavailable"] = True
            cache["rerouteUnavailableReason"] = result.get("reason") or "reroute unavailable"
            return
        cache[f"{tool_name}Result"] = result
        if tool_name == "reroute" and isinstance(result, dict):
            cache["lastRerouteResult"] = result
            cache["rerouteAttemptCount"] = max(int(cache.get("rerouteAttemptCount", 0)), int(result.get("attempt") or 0))
            cache.pop("drcResult", None)
            cache.pop("explainabilityReport", None)
        if tool_name == "fanout_route" and isinstance(result, dict) and isinstance(result.get("fanoutParams"), dict):
            cache["fanoutParams"] = result.get("fanoutParams")
    elif tool_name == "drc_check":
        cache["drcResult"] = result
        if _result_failed(result):
            cache["rerouteDrcFailureCount"] = int(cache.get("rerouteDrcFailureCount", 0)) + 1
            history = list(cache.get("rerouteDrcFeedbackHistory") or [])
            history.append(_reroute_feedback_summary(cache))
            cache["rerouteDrcFeedbackHistory"] = history[-3:]
            cache["lastDrcResult"] = result
            if int(cache.get("rerouteDrcFailureCount", 0)) < 3:
                cache.pop("rerouteResult", None)
    elif tool_name == "help_planner":
        cache["helpPlannerResult"] = result
        cache["rerouteResult"] = result
    elif tool_name == "explainability_report":
        cache["explainabilityReport"] = result



# ====== 功能：从前端 missing_routes 中提取 net 名称。 ======
def _nets_from_routes(routes: list[Any]) -> list[str]:
    nets: list[str] = []
    for item in routes:
        if isinstance(item, dict):
            value = item.get("net") or item.get("net_name") or item.get("netName") or item.get("name")
        else:
            value = item
        net = str(value or "").strip()
        if net and net not in nets:
            nets.append(net)
    return nets

# ====== 功能：判断工具结果是否表示失败。 ======
def _result_failed(result: Any) -> bool:
    return isinstance(result, dict) and (
        str(result.get("status", "")).lower() in {"failed", "error"}
        or result.get("passed") is False
    )


# ====== 功能：生成工具失败时的用户可读提示。 ======
def _failure_message(task_type: str, record: dict[str, Any]) -> str:
    call = record.get("call", {})
    result = record.get("result")
    reason = record.get("error") or ((result or {}).get("reason") if isinstance(result, dict) else "")
    return f"{task_type} 流程在工具 {call.get('name')} 处未完成：{reason or '工具返回失败'}"
# ====== 功能：把 DRC/解释失败压缩成下一轮 reroute 模型反馈。 ======
def _reroute_feedback_summary(cache: dict[str, Any]) -> dict[str, Any]:
    drc = cache.get("drcResult") if isinstance(cache.get("drcResult"), dict) else {}
    explain = cache.get("explainabilityReport") if isinstance(cache.get("explainabilityReport"), dict) else {}
    detail = drc.get("detail") if isinstance(drc.get("detail"), dict) else {}
    return {
        "drcStatus": drc.get("status"),
        "drcPassed": drc.get("passed") is True,
        "errors": drc.get("errors") or [],
        "failureSummary": detail.get("failure_summary") or detail.get("reason") or drc.get("reason"),
        "explainStatus": explain.get("status"),
        "explainReason": explain.get("reason") or str(explain.get("report") or "")[:1200],
    }

