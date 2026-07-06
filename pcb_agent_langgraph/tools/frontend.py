from __future__ import annotations

from typing import Any, Awaitable, Callable


FrontendToolSender = Callable[[str, str, dict[str, Any], float], Awaitable[Any]]
ProgressSender = Callable[[str, str], Awaitable[None]]


# ====== 功能：封装需要前端执行的 PCB 工具调用。 ======
class FrontendTool:
    # ====== 功能：初始化对象并保存运行所需依赖。 ======
    def __init__(self, name: str, sender: FrontendToolSender | None = None) -> None:
        self.name = name
        self.sender = sender

    # ====== 功能：异步执行当前工具或 Agent 调用。 ======
    async def ainvoke(self, arguments: dict[str, Any], context: dict[str, Any]) -> Any:
        if self.sender is None:
            raise RuntimeError(f"Frontend tool {self.name!r} is unavailable outside a WebSocket session.")
        session_id = str(context.get("session_id") or "")
        call_id = str(context.get("call_id") or "")
        timeout = float(context.get("timeout") or 360.0)
        payload = dict(arguments)
        payload["__tool_name__"] = self.name
        return await self.sender(session_id, call_id, payload, timeout)


