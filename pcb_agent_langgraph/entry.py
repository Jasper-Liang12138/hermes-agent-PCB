from __future__ import annotations

from typing import Any

ENTRY_MODULE_KEYS = ("entry_module", "entryModule", "module", "chain", "taskType", "task_type", "workflow", "workflow_id")
ENTRY_ACTION_KEYS = ("entry_action", "entryAction", "action")

_ENTRY_ALIASES = {
    "qa": "qa",
    "question": "qa",
    "chat": "qa",
    "knowledge": "qa",
    "pcb_qa_flow": "qa",
    "fanout": "global_fanout",
    "global_fanout": "global_fanout",
    "bga_escape": "global_fanout",
    "bga_escape_routing": "global_fanout",
    "escape": "global_fanout",
    "escape_flow": "global_fanout",
    "pcb_escape_flow": "global_fanout",
    "reroute": "reroute",
    "local_reroute": "reroute",
    "ripup": "reroute",
    "rip_up": "reroute",
    "pcb_reroute_flow": "reroute",
}

_ENTRY_WORKFLOWS = {
    "qa": ("qa", "pcb_qa_flow"),
    "global_fanout": ("global_fanout", "pcb_escape_flow"),
    "reroute": ("reroute", "pcb_reroute_flow"),
}


# ====== 功能：归一化前端入口按钮/chain/module 字段。 ======
def normalize_entry_module(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    key = text.replace("-", "_").lower()
    return _ENTRY_ALIASES.get(key, "")


# ====== 功能：把入口模块映射为 LangGraph task_type 和 workflow_id。 ======
def entry_task_workflow(value: Any) -> tuple[str, str] | None:
    module = normalize_entry_module(value)
    return _ENTRY_WORKFLOWS.get(module)
