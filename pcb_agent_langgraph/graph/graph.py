from __future__ import annotations

from typing import Any

from pcb_agent_langgraph.graph.nodes import GraphNodes, route_after_plan, route_after_tools
from pcb_agent_langgraph.graph.state import PCBState
from pcb_agent_langgraph.models.pcb_model import PCBModel
from pcb_agent_langgraph.planner.planner import PCBPlanner
from pcb_agent_langgraph.tools.base import Tool
from pcb_agent_langgraph.tools.frontend import ProgressSender
from pcb_agent_langgraph.utils.config import AppConfig


# ====== 功能：创建并编译 PCB Agent 的 LangGraph 状态图。 ======
def build_graph(model: PCBModel | None, tools: dict[str, Tool], *, progress_sender: ProgressSender | None = None, use_model_planner: bool = True, require_model_planner: bool = False, config: AppConfig | None = None) -> Any:
    try:
        from langgraph.graph import END, StateGraph
    except Exception as exc:
        raise RuntimeError("LangGraph is required. Install requirements.txt before running the PCB agent.") from exc

    nodes = GraphNodes(PCBPlanner(model=model, use_model=use_model_planner, require_model=require_model_planner, config=config), tools, progress_sender=progress_sender)
    graph = StateGraph(PCBState)
    graph.add_node("intent", nodes.intent)
    graph.add_node("plan", nodes.plan)
    graph.add_node("execute_tools", nodes.execute_tools)
    graph.add_node("reflect", nodes.reflect)
    graph.add_node("finish", nodes.finish)

    graph.set_entry_point("intent")
    graph.add_edge("intent", "plan")
    graph.add_conditional_edges("plan", route_after_plan, {"execute_tools": "execute_tools", "reflect": "reflect"})
    graph.add_conditional_edges("execute_tools", route_after_tools, {"plan": "plan", "reflect": "reflect"})
    graph.add_edge("reflect", "finish")
    graph.add_edge("finish", END)
    return graph.compile()




