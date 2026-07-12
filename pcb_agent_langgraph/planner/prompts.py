from __future__ import annotations

import json
from typing import Any


TOOLS: list[dict[str, Any]] = [
    {
        "name": "getProjectData",
        "description": "Ask PCB EDA frontend for current board/project data.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "importLines",
        "description": "Ask PCB EDA frontend to import routed line output.",
        "parameters": {"type": "object", "properties": {"filePath": {"type": "string"}}, "required": ["filePath"]},
    },
    {
        "name": "deleteTracesForRerouting",
        "description": "Ask PCB EDA frontend to delete selected traces for local reroute.",
        "parameters": {"type": "object", "properties": {}},
    },
    {
        "name": "layer_assign",
        "description": "Run layer assignment for global fanout.",
        "parameters": {"type": "object", "properties": {"boardPath": {"type": "string"}, "targetBGA": {"type": "string"}}},
    },
    {
        "name": "escape_order",
        "description": "Generate escape order for global fanout.",
        "parameters": {"type": "object", "properties": {"boardPath": {"type": "string"}, "targetBGA": {"type": "string"}}},
    },
    {
        "name": "pcb_extra_bga",
        "description": "Extract BGA components from board data.",
        "parameters": {"type": "object", "properties": {"boardData": {"type": "string"}}},
    },
    {
        "name": "fanout_route",
        "description": "Run external fanout router.",
        "parameters": {"type": "object", "properties": {"routerType": {"type": "string"}, "boardPath": {"type": "string"}}},
    },
    {
        "name": "prepare_reroute_inputs",
        "description": "Prepare KiCad board input and local route CSV before local reroute.",
        "parameters": {"type": "object", "properties": {"localRouteCsvPath": {"type": "string"}}},
    },
    # Legacy reroute is intentionally not exposed to the planner; VSEA reroute_loop is the only default model path.
    # {
    #     "name": "reroute",
    #     "description": "Debug/manual fallback: run one legacy local reroute model attempt after selected traces are removed.",
    #     "parameters": {"type": "object", "properties": {"localRerouteCompletionPolicy": {"type": "object"}}},
    # },
    {
        "name": "reroute_loop",
        "description": "Run the VSEA reroute pipeline for model generation, fill, hard DRC, and repair retry.",
        "parameters": {"type": "object", "properties": {"provider": {"type": "string"}}},
    },
    {
        "name": "compress_reroute_context",
        "description": "Chunk and retrieve compact KiCad board context before local reroute.",
        "parameters": {"type": "object", "properties": {"chunkChars": {"type": "integer"}, "retrieveK": {"type": "integer"}}},
    },
    {
        "name": "drc_check",
        "description": "Run full-board DRC for a helper-routed KiCad board.",
        "parameters": {"type": "object", "properties": {"routedKicadFilePath": {"type": "string"}}},
    },
    {
        "name": "explainability_report",
        "description": "Generate explainability report for PCB result and DRC status.",
        "parameters": {"type": "object", "properties": {}},
    },
]


# ====== 功能：生成 planner 使用的系统提示词。 ======
# ====== 功能：生成 planner 使用的系统提示词。 ======
def planner_system_prompt() -> str:
    return (
        "You are pcb-model, a PCB design agent planner. "
        "Classify the user request as qa, global_fanout, reroute, or unknown. "
        "Return only one JSON object. No Markdown. No explanation. "
        "The JSON must contain keys: intent, workflow, action, tool_calls, response, reason, entities. "
        "For global_fanout, workflow must be pcb_escape_flow. "
        "For reroute, workflow must be pcb_reroute_flow. "
        "For qa, workflow must be pcb_qa_flow. "
        "For fanout entities, use entities.selectedBGA, entities.routerType, "
        "entities.constraints.LineWidth, entities.constraints.LineSpacing. "
        "Do not return only extracted parameters; always return the full planner JSON. "
        "tool_calls should contain only the next executable step, not the whole workflow. "
        "If project data is not completed, the next global_fanout tool is getProjectData. "
        "If project data is completed but layer assignment is not completed, call layer_assign. "
        "If layer assignment is completed but escape order is not completed, call escape_order. "
        "If escape order is completed but fanout route is not completed, call fanout_route. "
        "If fanout route is completed and import file exists, call importLines directly; the frontend importLines approval UI is the only import confirmation. "
        "After fanout importLines succeeds, stop with result_review/report; never call drc_check or explainability_report for fanout. "
        "For reroute, after deleteTracesForRerouting completes, call prepare_reroute_inputs, then compress_reroute_context, then reroute_loop. "
        "If reroute_loop succeeds, trust its hard DRC result, auto-import with requireApproval=false, then call explainability_report. "
        "If reroute_loop fails, call help_planner fallback directly. When help_planner returns a routedKicadFilePath, call drc_check on that full board. "
        "If helper DRC passes, auto-import with requireApproval=false, then call explainability_report with the helper routedKicadFilePath. "
        "If helper DRC fails, never import, but still call explainability_report with the helper routedKicadFilePath and return the failure report. "
        "Do not call legacy reroute. Only call drc_check for a helper full-board result. "
        "Example: {\"intent\":\"global_fanout\",\"workflow\":\"pcb_escape_flow\",\"action\":\"get_project\","
        "\"tool_calls\":[{\"name\":\"getProjectData\",\"arguments\":{\"selectedBGA\":\"U22\","
        "\"routerType\":\"rule_135\",\"constraints\":{\"LineWidth\":4,\"LineSpacing\":3}}}],"
        "\"response\":\"\",\"reason\":\"user requested fanout\",\"entities\":{\"selectedBGA\":\"U22\","
        "\"routerType\":\"rule_135\",\"constraints\":{\"LineWidth\":4,\"LineSpacing\":3}}}. "
        "Available tools:\n"
        + json.dumps(TOOLS, ensure_ascii=False)
        + "\nResponse in INTENT_MODE."
    )
