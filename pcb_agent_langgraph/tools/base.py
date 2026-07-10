from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol

from pcb_agent_langgraph.debug_logging import log_debug_event
from pcb_agent_langgraph.graph.state import ToolCall, ToolRecord


# ====== 功能：定义所有工具需要实现的异步调用接口。 ======
class Tool(Protocol):
    name: str

    # ====== 功能：异步执行当前工具或 Agent 调用。 ======
    async def ainvoke(self, arguments: dict[str, Any], context: dict[str, Any]) -> Any:
        ...


@dataclass(slots=True)
# ====== 功能：把普通异步函数包装为统一 Tool 接口。 ======
class FunctionTool:
    name: str
    description: str
    func: Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]

    # ====== 功能：异步执行当前工具或 Agent 调用。 ======
    async def ainvoke(self, arguments: dict[str, Any], context: dict[str, Any]) -> Any:
        return await self.func(arguments, context)


# ====== 功能：统一调用工具并记录耗时、结果和异常。 ======
async def invoke_tool(tool: Tool, call: ToolCall, context: dict[str, Any]) -> ToolRecord:
    start = time.perf_counter()
    log_debug_event("tool.start", {"tool": getattr(tool, "name", call.get("name", "")), "call": call, "context": context})
    try:
        result = await tool.ainvoke(call.get("arguments", {}), context)
        elapsed_ms = (time.perf_counter() - start) * 1000
        record: ToolRecord = {"call": call, "result": result, "ok": True, "elapsed_ms": elapsed_ms}
        log_debug_event("tool.end", {"tool": getattr(tool, "name", call.get("name", "")), "call": call, "result": result, "ok": True, "elapsed_ms": elapsed_ms})
        return record
    except Exception as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000
        record = {"call": call, "result": None, "ok": False, "elapsed_ms": elapsed_ms, "error": str(exc)}
        log_debug_event("tool.error", {"tool": getattr(tool, "name", call.get("name", "")), "call": call, "ok": False, "elapsed_ms": elapsed_ms, "error": str(exc)})
        return record
