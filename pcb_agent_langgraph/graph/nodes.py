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
        cache = state.get("intermediate_cache", {}) or {}
        tool_names = [str(call.get("name")) for call in plan.get("tool_calls", []) if isinstance(call, dict)]
        fanout_entities = cache.get("fanoutEntities") or {}
        constraints = fanout_entities.get("constraints") if isinstance(fanout_entities, dict) else {}
        print(
            "planner_decision "
            f"source={plan.get('planner_source')} action={plan.get('action')} workflow={plan.get('workflow')} "
            f"tools={tool_names} validation={plan.get('validation', '')} model_action={plan.get('model_action', '')} "
            f"selectedBGA={(fanout_entities or {}).get('selectedBGA', '')} "
            f"routerType={(fanout_entities or {}).get('routerType', '')} "
            f"constraints={constraints or {}} "
            f"rerouteUnavailable={bool(cache.get('rerouteUnavailable'))} "
            f"rerouteAttemptCount={cache.get('rerouteAttemptCount', 0)} "
            f"drcResult={_result_brief(cache.get('drcResult'))} "
            f"reason={plan.get('reason', '')}"
        )
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
        elif action == "reroute_unavailable":
            message = planner_output.get("response") or "主模型 reroute 不可用。"
            workflow_state = "error"
        elif action in {"reroute_context_ready", "wait_reroute_confirm"}:
            message = planner_output.get("response") or "已完成拆线重布上下文压缩，请确认是否开始局部重布。"
            workflow_state = "confirm"
        elif action in {"reroute_report", "wait_reroute_import_confirm"}:
            message = planner_output.get("response") or "拆线重布和 DRC 检查已完成，请确认是否导入结果。"
            workflow_state = "report"
        elif action == "cancel_import":
            message = planner_output.get("response") or "已取消导入，fanout 结果保留在文件中。"
            workflow_state = "review"
        elif task_type == "global_fanout" and cache.get("importLinesRejected"):
            message = "已取消导入，fanout 结果保留在文件中。"
            workflow_state = "result_review"
        elif failed and not final_drc_passed:
            message = _failure_message(task_type, failed[-1], cache)
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
        if task_type == "reroute" and (failed or action == "reroute_unavailable" or cache.get("rerouteUnavailable")):
            diagnostics = _reroute_diagnostics(cache, failed[-1] if failed else None, action)
            report_payload = dict(report_payload or {})
            report_payload["task"] = "reroute"
            report_payload["rerouteDiagnostics"] = diagnostics
            if not markdown_report:
                message = _reroute_failure_text(diagnostics, fallback=message)
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
        if _import_lines_rejected(result):
            cache["importLinesRejected"] = True
            cache["importLinesRejectedReason"] = str(result)
            cache.pop("importLinesResult", None)
        else:
            cache.pop("importLinesRejected", None)
            cache.pop("importLinesRejectedReason", None)
            cache["importLinesResult"] = result
    elif tool_name == "pcb_extra_bga":
        cache["bgaCandidates"] = (result or {}).get("components", []) if isinstance(result, dict) else []
    elif tool_name == "compress_reroute_context":
        cache["rerouteContext"] = result
    elif tool_name in {"fanout_route", "reroute"}:
        if tool_name == "reroute":
            print(f"reroute_result status={_result_status(result)} reason={_result_reason(result)} attempt={_result_attempt(result)} selectedNets={cache.get('selectedNets')} workDir={_result_work_dir(result)} raw_summary={_result_summary(result)}")
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

# ====== 功能：生成日志用的工具结果简短状态。 ======
def _result_brief(result: Any) -> str:
    if not isinstance(result, dict):
        return type(result).__name__ if result is not None else "none"
    return str(result.get("status") or result.get("passed") or "dict")[:80]


def _result_status(result: Any) -> str:
    return str(result.get("status") if isinstance(result, dict) else type(result).__name__)


def _result_reason(result: Any) -> str:
    return str(result.get("reason") if isinstance(result, dict) else "")[:240]


def _result_attempt(result: Any) -> Any:
    return result.get("attempt") if isinstance(result, dict) else ""


def _result_summary(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)[:400]
    summary = result.get("rawSummary") or result.get("tracebackSummary") or result.get("modelRaw") or result.get("report") or result.get("reason") or result
    try:
        text = json.dumps(summary, ensure_ascii=False, default=str)
    except TypeError:
        text = str(summary)
    return " ".join(text.split())[:800]

def _result_work_dir(result: Any) -> str:
    if not isinstance(result, dict):
        return ""
    return str(result.get("workDir") or result.get("work_dir") or result.get("outputPath") or "")[:240]


# ====== 功能：判断工具结果是否表示失败。 ======
def _result_failed(result: Any) -> bool:
    if _import_lines_rejected(result):
        return True
    return isinstance(result, dict) and (
        str(result.get("status", "")).lower() in {"failed", "error"}
        or result.get("passed") is False
    )


# ====== 功能：识别前端工具审批拒绝 importLines 的返回值。 ======
def _import_lines_rejected(result: Any) -> bool:
    if isinstance(result, str):
        text = result.lower()
        return any(token in text for token in ("refused", "rejected", "cancel", "denied", "拒绝", "取消", "不导入"))
    if isinstance(result, dict):
        text = str(result.get("status") or result.get("result") or result.get("message") or result.get("reason") or "").lower()
        return any(token in text for token in ("refused", "rejected", "cancel", "denied", "拒绝", "取消", "不导入"))
    return False


# ====== 功能：生成工具失败时的用户可读提示。 ======
def _failure_message(task_type: str, record: dict[str, Any], cache: dict[str, Any] | None = None) -> str:
    if task_type == "reroute":
        return _reroute_failure_text(_reroute_diagnostics(cache or {}, record, ""))
    call = record.get("call", {})
    result = record.get("result")
    reason = record.get("error") or ((result or {}).get("reason") if isinstance(result, dict) else "")
    message = f"{task_type} 流程在工具 {call.get('name')} 处未完成：{reason or '工具返回失败'}"
    if isinstance(result, dict):
        details = _failure_detail_lines(result)
        if details:
            message += "\n" + "\n".join(details)
    return message

# ====== 功能：整理 reroute 失败诊断，供前端 reportPayload 直接展示。 ======
def _reroute_diagnostics(cache: dict[str, Any], record: dict[str, Any] | None, action: str = "") -> dict[str, Any]:
    record = record or {}
    call = record.get("call", {}) if isinstance(record, dict) else {}
    result = record.get("result") if isinstance(record, dict) else None
    result_dict = result if isinstance(result, dict) else {}
    tool_name = str(call.get("name") or result_dict.get("tool") or ("reroute" if cache.get("rerouteUnavailable") else ""))
    reason = str(record.get("error") or result_dict.get("reason") or cache.get("rerouteUnavailableReason") or "未知原因")
    status = str(result_dict.get("status") or ("unavailable" if cache.get("rerouteUnavailable") else "failed"))
    stage = _reroute_stage_label(tool_name, action)
    failure_type = str(result_dict.get("failureType") or _reroute_failure_type(tool_name, status, reason))
    selected_nets = result_dict.get("selectedNets") or cache.get("selectedNets") or []
    missing_routes = cache.get("missingRoutes") or []
    diagnostics = {
        "stage": stage,
        "tool": tool_name,
        "status": status,
        "failureType": failure_type,
        "reason": reason,
        "attempt": result_dict.get("attempt") or cache.get("rerouteAttemptCount") or 0,
        "selectedNets": selected_nets,
        "missingRouteCount": len(missing_routes) if isinstance(missing_routes, list) else 0,
        "workDir": result_dict.get("workDir") or result_dict.get("work_dir") or "",
        "nextAction": _reroute_next_action(failure_type, tool_name),
    }
    for key in ("tracebackSummary", "command", "stdout", "stderr", "stderrSummary", "pcbrouterBin", "sourceBoardPath", "inputBoardPath", "inputCsvPath", "tool_path", "eval_root", "python", "code_dir", "checkpoint", "input"):
        value = result_dict.get(key)
        if value not in (None, "", [], {}):
            diagnostics[key] = _short_failure_value(value, 1200 if key == "tracebackSummary" else 800)
    if cache.get("rerouteDrcFailureCount") is not None:
        diagnostics["drcFailureCount"] = cache.get("rerouteDrcFailureCount")
    if cache.get("rerouteDrcFeedbackHistory"):
        diagnostics["drcFeedbackHistory"] = cache.get("rerouteDrcFeedbackHistory")
    return diagnostics


# ====== 功能：把 reroute 诊断转成用户可读 final 文本。 ======
def _reroute_failure_text(diagnostics: dict[str, Any], fallback: str = "") -> str:
    if not diagnostics:
        return fallback or "拆线重布失败：未返回诊断信息。"
    lines = [
        f"拆线重布在【{diagnostics.get('stage') or '未知阶段'}】未完成。",
        f"原因：{diagnostics.get('reason') or '未知原因'}",
        f"失败类型：{diagnostics.get('failureType') or 'unknown'}",
    ]
    if diagnostics.get("attempt"):
        lines.append(f"尝试轮次：{diagnostics.get('attempt')}")
    if diagnostics.get("selectedNets"):
        lines.append(f"目标网络：{diagnostics.get('selectedNets')}")
    if diagnostics.get("workDir"):
        lines.append(f"工作目录：{diagnostics.get('workDir')}")
    if diagnostics.get("inputBoardPath"):
        lines.append(f"输入板文件：{diagnostics.get('inputBoardPath')}")
    if diagnostics.get("inputCsvPath"):
        lines.append(f"输入 CSV：{diagnostics.get('inputCsvPath')}")
    if diagnostics.get("stderr"):
        lines.append(f"stderr：{diagnostics.get('stderr')}")
    if diagnostics.get("tracebackSummary"):
        lines.append(f"traceback：{diagnostics.get('tracebackSummary')}")
    if diagnostics.get("nextAction"):
        lines.append(f"建议下一步：{diagnostics.get('nextAction')}")
    return "\n".join(lines)


def _reroute_stage_label(tool_name: str, action: str = "") -> str:
    mapping = {
        "deleteTracesForRerouting": "前端拆线",
        "compress_reroute_context": "上下文压缩",
        "reroute": "主模型重布",
        "drc_check": "DRC 检查",
        "explainability_report": "可解释性检查",
        "help_planner": "兜底规则布线",
        "importLines": "导入重布结果",
    }
    if action == "reroute_unavailable":
        return "主模型重布"
    return mapping.get(tool_name, tool_name or "未知阶段")


def _reroute_failure_type(tool_name: str, status: str, reason: str) -> str:
    reason_lower = reason.lower()
    if status.lower() == "unavailable":
        return "model_unavailable"
    if tool_name == "reroute":
        return "model_reroute_failed"
    if tool_name == "compress_reroute_context":
        return "context_compression_failed"
    if tool_name == "drc_check":
        return "drc_failed"
    if tool_name == "explainability_report":
        return "explainability_failed"
    if tool_name == "help_planner" and ("kicad" in reason_lower or "export.txt" in reason_lower):
        return "invalid_kicad_input"
    if tool_name == "help_planner":
        return "help_planner_failed"
    return "tool_failed"


def _reroute_next_action(failure_type: str, tool_name: str) -> str:
    if failure_type == "model_unavailable":
        return "检查 [reroute-model] 的 base_url/model/api_key，或查看模型服务返回的 traceback。"
    if failure_type == "context_compression_failed":
        return "确认 deleteTracesForRerouting 返回了 projectData/missing_routes，且项目数据是可解析的 KiCad/板级文本。"
    if failure_type == "invalid_kicad_input":
        return "help_planner 需要 .kicad_pcb 输入；请先确认 export.txt 到 KiCad 输入的转换链路。"
    if failure_type == "drc_failed":
        return "三轮内会携带 DRC 反馈继续调用主模型；满三轮后才进入 help_planner。"
    if tool_name == "help_planner":
        return "查看 workDir、inputBoardPath、inputCsvPath 和 pcbrouter stderr。"
    return "查看 reportPayload.rerouteDiagnostics 中的路径、stderr 和 tracebackSummary。"

# ====== 功能：提取工具失败时最关键的诊断字段，避免前端只显示 command failed。 ======
def _failure_detail_lines(result: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for key in ("command", "workDir", "input_path", "output_path", "tool_path", "pcbrouterBin", "sourceBoardPath", "inputBoardPath", "inputCsvPath"):
        value = result.get(key)
        if value not in (None, "", [], {}):
            lines.append(f"{key}: {_short_failure_value(value)}")
    for key in ("stdout", "stderr", "stderrSummary"):
        value = str(result.get(key) or "").strip()
        if value:
            lines.append(f"{key}: {_short_failure_value(value, 800)}")
    fanout_params = result.get("fanoutParams") if isinstance(result.get("fanoutParams"), dict) else {}
    for key in ("selectedBGA", "routerType"):
        value = fanout_params.get(key)
        if value not in (None, "", [], {}):
            lines.append(f"{key}: {value}")
    return lines


# ====== 功能：缩短失败诊断字段，保证消息可读。 ======
def _short_failure_value(value: Any, limit: int = 500) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "..."

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
