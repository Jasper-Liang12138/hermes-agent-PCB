from __future__ import annotations

from typing import Any

from pcb_agent_langgraph.tools.base import Tool
from pcb_agent_langgraph.tools.external import AnalysisTool, ExternalProgramTool
from pcb_agent_langgraph.tools.frontend import FrontendTool, FrontendToolSender
from pcb_agent_langgraph.utils.config import AppConfig


# ====== 功能：构建 LangGraph 可用的工具注册表。 ======
def build_tool_registry(config: AppConfig, frontend_sender: FrontendToolSender | None = None) -> dict[str, Tool]:
    # 所有工具在这里集中注册，便于确认哪些是前端工具、哪些是真实外部程序。
    tools: dict[str, Tool] = {
        "getProjectData": FrontendTool("getProjectData", frontend_sender),
        "importLines": FrontendTool("importLines", frontend_sender),
        "deleteTracesForRerouting": FrontendTool("deleteTracesForRerouting", frontend_sender),
        "layer_assign": ExternalProgramTool("layer_assign", config),
        "escape_order": ExternalProgramTool("escape_order", config),
        "fanout_route": ExternalProgramTool("fanout_route", config),
        "prepare_reroute_inputs": ExternalProgramTool("prepare_reroute_inputs", config),
        "reroute_loop": ExternalProgramTool("reroute_loop", config),
        # Legacy reroute is intentionally not registered; VSEA reroute_loop is the default model/DRC/repair path.
        # "reroute": ExternalProgramTool("reroute", config),
        "compress_reroute_context": ExternalProgramTool("compress_reroute_context", config),
        "help_planner": ExternalProgramTool("help_planner", config),
        "pcb_extra_bga": AnalysisTool("pcb_extra_bga", config),
        # Legacy drc_check is intentionally not registered; VSEA hard DRC is trusted for reroute.
        # "drc_check": AnalysisTool("drc_check", config),
        "explainability_report": AnalysisTool("explainability_report", config),
    }
    return tools


# ====== 功能：从 LangGraph 状态组装工具调用上下文。 ======
def tool_context_from_state(state: dict[str, Any], call_id: str, timeout: float) -> dict[str, Any]:
    # 工具上下文由 LangGraph state 派生，避免工具直接读取全局会话状态。
    cache = state.get("intermediate_cache", {}) or {}
    return {
        "session_id": state.get("session_id", ""),
        "project_id": state.get("project_id", ""),
        "user_input": state.get("user_input", ""),
        "call_id": call_id,
        "timeout": timeout,
        "board_data": cache.get("boardData") or cache.get("projectData") or "",
        "fanoutParams": cache.get("fanoutParams"),
        **dict(cache.get("fanoutEntities") or {}),
        **cache,
    }
