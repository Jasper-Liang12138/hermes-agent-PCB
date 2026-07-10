from __future__ import annotations

import asyncio
import json
import math
import re
import time
from pathlib import Path
from typing import Any, Awaitable, Callable

from pcb_agent_langgraph.debug_logging import log_debug_event
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

    # ====== 功能：统一记录节点输入、输出和异常。 ======
    async def _run_logged_node(self, name: str, state: PCBState, func: Callable[[PCBState], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
        log_debug_event(f"node.{name}.start", {"node": name, "state": state})
        try:
            result = await func(state)
            log_debug_event(f"node.{name}.end", {"node": name, "state": state, "result": result})
            return result
        except Exception as exc:
            log_debug_event(f"node.{name}.error", {"node": name, "state": state, "error": str(exc)})
            raise

    # ====== 功能：记录意图识别节点进入状态。 ======
    async def intent(self, state: PCBState) -> dict[str, Any]:
        return await self._run_logged_node("intent", state, self._intent)

    # ====== 功能：执行意图识别节点的核心逻辑。 ======
    async def _intent(self, state: PCBState) -> dict[str, Any]:
        return {
            "current_stage": "intent",
            "loop_count": int(state.get("loop_count", 0)) + 1,
            **add_trace(state, "intent", {"user_input": state.get("user_input", "")}),
        }

    # ====== 功能：根据当前状态生成下一步执行计划。 ======
    async def plan(self, state: PCBState) -> dict[str, Any]:
        return await self._run_logged_node("plan", state, self._plan)

    # ====== 功能：执行计划节点的核心逻辑。 ======
    async def _plan(self, state: PCBState) -> dict[str, Any]:
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
        return await self._run_logged_node("execute_tools", state, self._execute_tools)

    # ====== 功能：执行工具节点的核心逻辑。 ======
    async def _execute_tools(self, state: PCBState) -> dict[str, Any]:
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
            await self.progress_sender(session_id, _tool_progress_start_message(call))
        task = asyncio.create_task(invoke_tool(tool, call, context))
        started_at = time.monotonic()
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
            except asyncio.TimeoutError:
                if self.progress_sender and session_id and not is_frontend_tool:
                    await self.progress_sender(session_id, _tool_progress_wait_message(call, time.monotonic() - started_at))
        record = await task
        if self.progress_sender and session_id and not is_frontend_tool:
            await self.progress_sender(session_id, _tool_progress_done_message(record))
        return record
    # ====== 功能：根据工具结果生成当前轮回复和流程状态。 ======
    async def reflect(self, state: PCBState) -> dict[str, Any]:
        return await self._run_logged_node("reflect", state, self._reflect)

    # ====== 功能：执行反思节点的核心逻辑。 ======
    async def _reflect(self, state: PCBState) -> dict[str, Any]:
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
        return await self._run_logged_node("finish", state, self._finish)

    # ====== 功能：执行收尾节点的核心逻辑。 ======
    async def _finish(self, state: PCBState) -> dict[str, Any]:
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
    # 工具成功后回到 plan；reroute_loop 失败可恢复，继续回 planner 触发 help_planner。
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
    allowed = {"explainability_report", "reroute_loop"}
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
    for key in ("selectedBGA", "targetBGA", "targetBGAs", "bgaType", "bgaLayoutType", "routerType", "constraints"):
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
            cache["fanoutEntities"] = {**dict(cache.get("fanoutEntities") or {}), **{k: v for k, v in result.get("fanoutParams", {}).items() if k in {"selectedBGA", "bgaType", "bgaLayoutType", "routerType", "constraints"}}}
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
    elif tool_name == "prepare_reroute_inputs":
        cache["rerouteInput"] = result
        if isinstance(result, dict):
            if result.get("selectedNets"):
                cache["selectedNets"] = result.get("selectedNets")
            if result.get("missingRoutes"):
                cache["missingRoutes"] = result.get("missingRoutes")
            if result.get("kicadBoardText"):
                cache["projectData"] = result.get("kicadBoardText")
                cache["boardData"] = result.get("kicadBoardText")
            if result.get("localRouteCsvPath"):
                cache["localRouteCsvPath"] = result.get("localRouteCsvPath")
    elif tool_name == "compress_reroute_context":
        cache["rerouteContext"] = result
    elif tool_name == "reroute_loop":
        cache["rerouteLoopResult"] = result
        if isinstance(result, dict) and str(result.get("status", "")).lower() == "ok":
            cache["rerouteResult"] = result
            cache.pop("rerouteUnavailable", None)
            cache.pop("rerouteUnavailableReason", None)
            if isinstance(result.get("drcResult"), dict):
                cache["drcResult"] = result.get("drcResult")
        else:
            cache.pop("rerouteResult", None)
            cache.pop("drcResult", None)
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
            cache.setdefault("rerouteStartedAt", time.time())
            cache["rerouteAttemptCount"] = max(int(cache.get("rerouteAttemptCount", 0)), int(result.get("attempt") or 0))
            cache.pop("drcResult", None)
            cache.pop("explainabilityReport", None)
        if tool_name == "fanout_route" and isinstance(result, dict) and isinstance(result.get("fanoutParams"), dict):
            cache["fanoutParams"] = result.get("fanoutParams")
    elif tool_name == "drc_check":
        cache["drcResult"] = result
        if isinstance(result, dict) and isinstance(cache.get("rerouteResult"), dict):
            detail = result.get("detail") if isinstance(result.get("detail"), dict) else {}
            filled_path = detail.get("filled_board_data_file_path") or result.get("filledBoardDataFilePath")
            if filled_path:
                reroute_result = dict(cache.get("rerouteResult") or {})
                reroute_result["routedKicadFilePath"] = filled_path
                import_path, notes = _write_reroute_incremental_import_file(cache, reroute_result)
                if import_path:
                    reroute_result["importLinesFilePath"] = import_path
                    reroute_result["incrementalImportFilePath"] = import_path
                    reroute_result["incrementalImportNotes"] = notes
                else:
                    reroute_result.pop("importLinesFilePath", None)
                    reroute_result["incrementalImportNotes"] = notes or ["reroute_incremental_import_not_generated"]
                cache["rerouteResult"] = reroute_result
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


def _tool_timeout_seconds(call: dict[str, Any]) -> float:
    try:
        timeout = float(call.get("timeout") or 0)
    except (TypeError, ValueError):
        timeout = 0.0
    return timeout if timeout > 0 else 900.0


def _eta_minutes(call: dict[str, Any], elapsed_seconds: float = 0.0) -> int:
    # VSEA reroute usually runs model sampling, fill and hard DRC internally. Use the tool timeout
    # as a coarse upper bound so frontend text stays useful without pretending precision.
    remaining = max(60.0, _tool_timeout_seconds(call) - max(0.0, elapsed_seconds))
    return max(1, int(math.ceil(remaining / 60.0)))


def _tool_progress_start_message(call: dict[str, Any]) -> str:
    tool_name = str(call.get("name") or "工具")
    if tool_name == "reroute_loop":
        minutes = _eta_minutes(call)
        if minutes >= 10:
            lower = max(1, minutes - 5)
            return f"正在重布中，预计还需要 {lower}-{minutes} 分钟。"
        return f"正在重布中，预计还需要约 {minutes} 分钟。"
    return f"正在调用 {tool_name}..."


def _tool_progress_wait_message(call: dict[str, Any], elapsed_seconds: float) -> str:
    tool_name = str(call.get("name") or "工具")
    if tool_name == "reroute_loop":
        minutes = _eta_minutes(call, elapsed_seconds)
        return f"重布中，预计还需要约 {minutes} 分钟；正在生成候选路线、回填并检查规则..."
    return f"{tool_name} 执行中..."


def _tool_progress_done_message(record: dict[str, Any]) -> str:
    result = record.get("result")
    tool_name = str((record.get("call") or {}).get("name") or (result.get("tool") if isinstance(result, dict) else "") or "工具")
    suffix = _tool_progress_suffix(record)
    if tool_name == "reroute_loop":
        if not record.get("ok") or _result_failed(result):
            return "重布正在加速进行，快完成了！"
        return f"重布已完成，继续准备导入结果...（{suffix}）"
    return f"{tool_name} {suffix}"


def _tool_progress_suffix(record: dict[str, Any]) -> str:
    result = record.get("result")
    if not record.get("ok") or _result_failed(result):
        return "返回失败，正在整理原因..."
    if isinstance(result, dict) and result.get("tool") in {"reroute", "reroute_loop"}:
        details = []
        elapsed = record.get("elapsed_ms")
        model_elapsed = result.get("elapsedMs")
        output_chars = result.get("modelOutputChars")
        if result.get("tool") == "reroute_loop":
            output_chars = output_chars if isinstance(output_chars, int) else len(str(result.get("modelOutputText") or ""))
        work_dir = result.get("workDir")
        if isinstance(elapsed, (int, float)):
            details.append(f"工具耗时 {elapsed / 1000:.1f}s")
        if isinstance(model_elapsed, (int, float)):
            details.append(f"模型耗时 {model_elapsed / 1000:.1f}s")
        if isinstance(output_chars, int):
            details.append(f"模型输出 {output_chars} 字符")
        if work_dir:
            details.append(f"workDir={work_dir}")
        if details:
            return "已完成（" + "，".join(details) + "），继续处理..."
    return "已完成，继续处理..."


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
    for key in ("tracebackSummary", "command", "stdout", "stderr", "stdoutSummary", "stderrSummary", "pcbrouterBin", "sourceBoardPath", "inputBoardPath", "inputCsvPath", "inputCsvPreview", "tool_path", "eval_root", "python", "code_dir", "checkpoint", "input", "aiPcbEvalPath", "drcAgentPackage", "pipelineRoot"):
        value = result_dict.get(key)
        if value not in (None, "", [], {}):
            diagnostics[key] = _short_failure_value(value, 1200 if key == "tracebackSummary" else 800)
    loop_result = cache.get("rerouteLoopResult") if isinstance(cache.get("rerouteLoopResult"), dict) else {}
    if loop_result and result_dict is not loop_result:
        diagnostics["rerouteLoopFailure"] = {
            "status": loop_result.get("status"),
            "failureStage": loop_result.get("failureStage"),
            "failureType": loop_result.get("failureType"),
            "reason": _short_failure_value(loop_result.get("reason"), 1000),
            "modelCalled": bool(loop_result.get("modelCalled")),
            "workDir": loop_result.get("workDir"),
            "pipelineRoot": loop_result.get("pipelineRoot"),
            "aiPcbEvalPath": loop_result.get("aiPcbEvalPath"),
            "drcAgentPackage": loop_result.get("drcAgentPackage"),
        }
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
    if diagnostics.get("inputCsvPreview"):
        lines.append(f"输入 CSV 预览：{diagnostics.get('inputCsvPreview')}")
    loop_failure = diagnostics.get("rerouteLoopFailure")
    if isinstance(loop_failure, dict) and loop_failure:
        called_text = "已进入模型调用" if loop_failure.get("modelCalled") else "未进入模型调用"
        lines.append(
            "VSEA 主流程："
            f"{called_text}；阶段={loop_failure.get('failureStage') or ''}；"
            f"类型={loop_failure.get('failureType') or ''}；原因={loop_failure.get('reason') or ''}"
        )
        if loop_failure.get("workDir"):
            lines.append(f"VSEA workDir：{loop_failure.get('workDir')}")
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
        "prepare_reroute_inputs": "重布线输入准备",
        "compress_reroute_context": "上下文压缩",
        "reroute_loop": "VSEA 重布线主流程",
        # Legacy reroute/drc_check labels are kept commented for debugging reference only.
        # "reroute": "主模型重布",
        # "drc_check": "DRC 检查",
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
    if tool_name == "prepare_reroute_inputs":
        return "reroute_input_prepare_failed"
    if tool_name == "reroute_loop":
        return "reroute_loop_failed"
    # if tool_name == "reroute":
    #     return "model_reroute_failed"
    if tool_name == "compress_reroute_context":
        return "context_compression_failed"
    # if tool_name == "drc_check":
    #     return "drc_failed"
    if tool_name == "explainability_report":
        return "explainability_failed"
    if tool_name == "help_planner" and ("requires kicad" in reason_lower or "got pcb builder/export txt" in reason_lower or "current projectdata is not kicad" in reason_lower):
        return "invalid_kicad_input"
    if tool_name == "help_planner":
        return "help_planner_failed"
    return "tool_failed"


def _reroute_next_action(failure_type: str, tool_name: str) -> str:
    if failure_type == "model_unavailable":
        return "检查 [reroute-model] 的 base_url/model/api_key，或查看模型服务返回的 traceback。"
    if failure_type == "reroute_input_prepare_failed":
        return "确认前端拆线结果包含可转换的版图数据，并检查格式转换输出目录。"
    if failure_type == "reroute_loop_failed":
        return "已切换到 help_planner 兜底；查看 workDir 中的 VSEA 输出、debug 和 DRC 报告。"
    if failure_type == "context_compression_failed":
        return "确认 deleteTracesForRerouting 返回了 projectData/missing_routes，且项目数据是可解析的 KiCad/板级文本。"
    if failure_type == "invalid_kicad_input":
        return "help_planner 需要 .kicad_pcb 输入；请先确认 export.txt 到 KiCad 输入的转换链路。"
    if failure_type == "pcbrouter_execution_failed":
        return "查看 inputCsvPreview、pcbrouter stdout/stderr，确认目标 net、target_x_mm/target_y_mm 和 target_layer 是否符合 helper-router 要求。"
    # if failure_type == "drc_failed":
    #     return "默认主链路由 VSEA 内部处理 DRC/repair；旧 drc_check 失败时可手动检查 patch fill 和 DRC 明细。"
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


def _write_reroute_incremental_import_file(cache: dict[str, Any], reroute_result: dict[str, Any]) -> tuple[str, list[str]]:
    patch_text = str(reroute_result.get("modelOutputText") or reroute_result.get("routedText") or "").strip()
    reroute_input = cache.get("rerouteInput") if isinstance(cache.get("rerouteInput"), dict) else {}
    board_text = str(reroute_input.get("kicadBoardText") or cache.get("boardData") or cache.get("projectData") or "")
    work_dir = Path(str(reroute_result.get("workDir") or (reroute_input or {}).get("workDir") or "."))
    session_id = str(cache.get("session_id") or "session")
    local_context = {"missingRoutes": cache.get("missingRoutes") or reroute_input.get("missingRoutes") or []}
    if not patch_text:
        return "", ["reroute_incremental_import_missing_patch_text"]
    if not board_text:
        return "", ["reroute_incremental_import_missing_board_text"]

    net_id_to_name = _kicad_net_id_to_name(board_text)
    line_records: list[str] = []
    for block in _extract_balanced_sexpr_blocks(patch_text, "segment"):
        start = re.search(r"\(\s*start\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", block, re.IGNORECASE)
        end = re.search(r"\(\s*end\s+(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)", block, re.IGNORECASE)
        width = re.search(r"\(\s*width\s+(-?\d+(?:\.\d+)?)\s*\)", block, re.IGNORECASE)
        layer = re.search(r"\(\s*layer\s+\"?([^\s\)\"]+)\"?\s*\)", block, re.IGNORECASE)
        net = re.search(r"\(\s*net\s+(\d+)\s*\)", block, re.IGNORECASE)
        if not (start and end and width and layer and net):
            continue
        net_name = net_id_to_name.get(net.group(1).strip(), net.group(1).strip()).replace("!", "_")
        line_layer = _kicad_layer_to_line_out_layer(layer.group(1))
        width_mil = _kicad_mm_to_mil_text(width.group(1))
        x1_raw = float(start.group(1))
        y1_raw = float(start.group(2))
        x2_raw = float(end.group(1))
        y2_raw = float(end.group(2))
        clip = _single_axis_missing_route_clip(local_context, net_name, layer.group(1))
        clipped = _clip_segment_to_single_axis_missing_route(x1=x1_raw, y1=y1_raw, x2=x2_raw, y2=y2_raw, clip=clip)
        if clipped is None:
            continue
        x1_raw, y1_raw, x2_raw, y2_raw = clipped
        line_records.append(
            f"{line_layer}!LINE!0!{net_name}!"
            f"{_kicad_mm_to_line_out_coord_text(str(x1_raw), 'x')}!"
            f"{_kicad_mm_to_line_out_coord_text(str(y1_raw), 'y')}!"
            f"{_kicad_mm_to_line_out_coord_text(str(x2_raw), 'x')}!"
            f"{_kicad_mm_to_line_out_coord_text(str(y2_raw), 'y')}!"
            f"{width_mil}"
        )
    if not line_records:
        return "", ["reroute_incremental_import_no_segment_records"]

    import_dir = work_dir / "import"
    import_dir.mkdir(parents=True, exist_ok=True)
    safe_session = re.sub(r"[^A-Za-z0-9_.-]+", "_", session_id).strip("_") or "session"
    output_path = import_dir / f"{safe_session}_reroute_line.out"
    import_text = "\n".join(line_records) + "\n"
    passed, reason, stats = _validate_reroute_incremental_import_text(import_text)
    if not passed:
        return "", [f"reroute_incremental_import_invalid:{reason}"]
    output_path.write_text(import_text, encoding="utf-8")
    return str(output_path), [f"generated_reroute_incremental_line_out:{output_path}", f"lineCount:{stats.get('lineCount', 0)}"]


def _extract_balanced_sexpr_blocks(text: str, head: str) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    blocks: list[str] = []
    pattern = re.compile(rf"\(\s*{re.escape(head)}\b", re.IGNORECASE)
    for match in pattern.finditer(text):
        depth = 0
        in_string = False
        escaped = False
        for pos in range(match.start(), len(text)):
            char = text[pos]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
                continue
            if char == "(":
                depth += 1
                continue
            if char == ")":
                depth -= 1
                if depth == 0:
                    blocks.append(text[match.start():pos + 1].strip())
                    break
    return blocks


def _parse_kicad_net_name_to_id(board_text: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not isinstance(board_text, str) or not board_text:
        return mapping
    for pattern in (
        re.compile(r"\(\s*net\s+(\d+)\s+\"([^\"]+)\"\s*\)", re.IGNORECASE),
        re.compile(r"\(\s*net\s+(\d+)\s+([^\s\)]+)\s*\)", re.IGNORECASE),
    ):
        for match in pattern.finditer(board_text):
            net_id = match.group(1).strip()
            net_name = match.group(2).strip()
            if net_name and net_name not in mapping:
                mapping[net_name] = net_id
    return mapping


def _kicad_net_id_to_name(board_text: str) -> dict[str, str]:
    return {net_id: net_name for net_name, net_id in _parse_kicad_net_name_to_id(board_text).items()}


def _kicad_mm_to_mil_text(value: str) -> str:
    return f"{(float(value) / 0.0254):.2f}"


def _kicad_mm_to_line_out_coord_text(value: str, axis: str) -> str:
    origin = 363386.0 if axis == "x" else 534646.0
    dbu_mm = 0.000254
    local_mil = (float(value) / dbu_mm - origin) / 100.0
    return f"{local_mil:.2f}"


def _kicad_layer_to_line_out_layer(layer: str) -> str:
    layer = str(layer or "").strip().strip('"')
    aliases = {"F.Cu": "TOP", "B.Cu": "BOTTOM", "Top": "TOP", "Bottom": "BOTTOM", "Conductor/Top": "TOP", "Conductor/Bottom": "BOTTOM"}
    if layer in aliases:
        return aliases[layer]
    if layer.startswith("Conductor/"):
        layer = layer.split("/", 1)[1]
    if layer.lower() == "top":
        return "TOP"
    if layer.lower() == "bottom":
        return "BOTTOM"
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", layer).upper()


def _validate_reroute_incremental_import_text(text: str) -> tuple[bool, str, dict[str, Any]]:
    stripped = str(text or "").lstrip()
    if not stripped:
        return False, "轻量 line.out 导入文件为空", {"lineCount": 0}
    if stripped.startswith("(layout"):
        return False, "轻量 line.out 导入文件不能是完整 layout", {}
    if stripped.startswith("(wires"):
        return False, "轻量 line.out 导入文件不能是 (wires ...) 子结构", {}
    valid_count = 0
    layers: list[str] = []
    invalid_reasons: list[str] = []
    for line_no, raw in enumerate(stripped.splitlines(), start=1):
        parts = [part.strip() for part in raw.split("!")]
        if len(parts) != 9 or parts[1].upper() != "LINE":
            invalid_reasons.append(f"line {line_no}: 不是 LINE 记录")
            continue
        layer, _, _, net, x1, y1, x2, y2, width = parts
        if not layer or "/" in layer or ".Cu" in layer:
            invalid_reasons.append(f"line {line_no}: 层名不是 line.out 原生层名")
            continue
        if not net:
            invalid_reasons.append(f"line {line_no}: 缺少 net")
            continue
        try:
            values = [float(x1), float(y1), float(x2), float(y2), float(width)]
        except ValueError:
            invalid_reasons.append(f"line {line_no}: 坐标或线宽不是数字")
            continue
        if any(not math.isfinite(value) for value in values):
            invalid_reasons.append(f"line {line_no}: 坐标或线宽不是有限数字")
            continue
        if values[-1] < 0 or values[-1] > 250:
            invalid_reasons.append(f"line {line_no}: 线宽超出 importLines 范围")
            continue
        layers.append(layer.upper())
        valid_count += 1
    if valid_count <= 0:
        reason = "轻量 line.out 导入文件未包含有效 LINE 记录"
        if invalid_reasons:
            reason += "：" + "; ".join(invalid_reasons[:3])
        return False, reason, {"lineCount": 0, "layers": sorted(set(layers))}
    return True, "", {"lineCount": valid_count, "layers": sorted(set(layers))}


def _single_axis_missing_route_clip(local_context: Any, net_name: str, layer_name: str) -> dict[str, Any] | None:
    if not isinstance(local_context, dict):
        return None
    routes = local_context.get("missingRoutes")
    if not isinstance(routes, list) or len(routes) != 1:
        return None
    route = routes[0]
    if not isinstance(route, dict):
        return None
    route_net = str(route.get("net_name") or route.get("net") or "").strip()
    if route_net and route_net != net_name:
        return None
    start = route.get("start")
    end = route.get("end")
    if not isinstance(start, dict) or not isinstance(end, dict):
        return None
    route_layer = str(start.get("layer") or end.get("layer") or "").strip()
    if route_layer and _kicad_layer_to_line_out_layer(route_layer) != _kicad_layer_to_line_out_layer(layer_name):
        return None
    try:
        sx = float(start["x"])
        sy = float(start["y"])
        ex = float(end["x"])
        ey = float(end["y"])
    except Exception:
        return None
    tolerance = 0.05
    if abs(sx - ex) <= tolerance:
        return {"axis": "y", "fixed": sx, "min": min(sy, ey), "max": max(sy, ey), "tolerance": tolerance}
    if abs(sy - ey) <= tolerance:
        return {"axis": "x", "fixed": sy, "min": min(sx, ex), "max": max(sx, ex), "tolerance": tolerance}
    return None


def _clip_segment_to_single_axis_missing_route(*, x1: float, y1: float, x2: float, y2: float, clip: dict[str, Any] | None) -> tuple[float, float, float, float] | None:
    if not clip:
        return x1, y1, x2, y2
    axis = clip.get("axis")
    fixed = float(clip.get("fixed"))
    lower = float(clip.get("min"))
    upper = float(clip.get("max"))
    tolerance = float(clip.get("tolerance", 0.05))
    if axis == "y":
        if abs(x1 - fixed) > tolerance or abs(x2 - fixed) > tolerance:
            return None
        seg_lower, seg_upper = min(y1, y2), max(y1, y2)
        overlap_lower, overlap_upper = max(seg_lower, lower), min(seg_upper, upper)
        if overlap_upper - overlap_lower <= tolerance:
            return None
        return (fixed, overlap_lower, fixed, overlap_upper) if y1 <= y2 else (fixed, overlap_upper, fixed, overlap_lower)
    if axis == "x":
        if abs(y1 - fixed) > tolerance or abs(y2 - fixed) > tolerance:
            return None
        seg_lower, seg_upper = min(x1, x2), max(x1, x2)
        overlap_lower, overlap_upper = max(seg_lower, lower), min(seg_upper, upper)
        if overlap_upper - overlap_lower <= tolerance:
            return None
        return (overlap_lower, fixed, overlap_upper, fixed) if x1 <= x2 else (overlap_upper, fixed, overlap_lower, fixed)
    return x1, y1, x2, y2
