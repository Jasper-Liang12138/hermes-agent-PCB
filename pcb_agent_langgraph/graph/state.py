from __future__ import annotations

from typing import Any, Literal, TypedDict

try:
    from typing import NotRequired
except ImportError:  # Python 3.10
    from typing_extensions import NotRequired

TaskType = Literal["qa", "global_fanout", "reroute", "unknown"]
Stage = Literal["input", "intent", "planning", "tool_execution", "reflection", "finished", "error"]


# ====== 功能：定义对话消息的数据结构。 ======
class ChatMessage(TypedDict):
    role: str
    content: str


# ====== 功能：定义 planner 生成的工具调用数据结构。 ======
class ToolCall(TypedDict):
    id: str
    name: str
    arguments: dict[str, Any]
    timeout: NotRequired[float]


# ====== 功能：定义一次工具执行记录的数据结构。 ======
class ToolRecord(TypedDict):
    call: ToolCall
    result: Any
    ok: bool
    elapsed_ms: float
    error: NotRequired[str]


# ====== 功能：定义 planner 输出的统一数据结构。 ======
class PlannerOutput(TypedDict, total=False):
    intent: TaskType
    workflow: str
    action: str
    tool_calls: list[ToolCall]
    response: str
    reason: str
    entities: dict[str, Any]
    planner_source: str
    model_elapsed_ms: float
    model_usage: dict[str, Any]
    selection: list[dict[str, Any]]
    fanout_params: dict[str, Any]


# ====== 功能：定义 LangGraph 在节点之间传递的整体状态。 ======
class PCBState(TypedDict, total=False):
    session_id: str
    project_id: str
    user_input: str
    conversation_history: list[ChatMessage]
    task_type: TaskType
    workflow_id: str
    workflow_state: str
    current_stage: Stage
    pcb_project: dict[str, Any]
    design_state: dict[str, Any]
    tool_calls: list[ToolCall]
    tool_history: list[ToolRecord]
    tool_results: dict[str, Any]
    planner_output: PlannerOutput
    drc_result: dict[str, Any]
    intermediate_cache: dict[str, Any]
    final_response: str
    markdown_report: str
    report_payload: dict[str, Any]
    selection: list[dict[str, Any]]
    error: str
    trace: list[dict[str, Any]]
    loop_count: int


# ====== 功能：创建一次用户请求的初始状态。 ======
def initial_state(session_id: str, project_id: str, user_input: str, history: list[ChatMessage] | None = None) -> PCBState:
    return {
        "session_id": session_id,
        "project_id": project_id,
        "user_input": user_input,
        "conversation_history": list(history or []) + [{"role": "user", "content": user_input}],
        "task_type": "unknown",
        "workflow_id": "idle",
        "workflow_state": "idle",
        "current_stage": "input",
        "pcb_project": {},
        "design_state": {},
        "tool_calls": [],
        "tool_history": [],
        "tool_results": {},
        "planner_output": {},
        "drc_result": {},
        "intermediate_cache": {},
        "final_response": "",
        "markdown_report": "",
        "report_payload": {},
        "selection": [],
        "fanout_params": {},
        "trace": [],
        "loop_count": 0,
    }


# ====== 功能：向状态中追加节点执行轨迹。 ======
def add_trace(state: PCBState, node: str, payload: dict[str, Any]) -> PCBState:
    trace = list(state.get("trace", []))
    trace.append({"node": node, "payload": payload})
    return {"trace": trace}





