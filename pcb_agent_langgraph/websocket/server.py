from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from typing import Any

from pcb_agent_langgraph.debug_logging import log_debug_event
from pcb_agent_langgraph.agent import PCBLangGraphAgent
from pcb_agent_langgraph.utils.config import load_config
from pcb_agent_langgraph.websocket.protocol import agent_message, error_message, parse_tool_result, parse_user_message, tool_call_message


# ====== 功能：提供前端与 PCB Agent 通信的 WebSocket 服务。 ======
class PCBWebSocketServer:
    # ====== 功能：初始化对象并保存运行所需依赖。 ======
    def __init__(self, config_path: str | None = None, *, use_model_planner: bool = True) -> None:
        self.config = load_config(config_path)
        self._pending: dict[str, asyncio.Future[Any]] = {}
        self._connections: dict[str, tuple[Any, str]] = {}
        self._connection_sessions: dict[tuple[int, str], str] = {}
        self._active_message_ids: dict[str, str] = {}
        self._last_progress: dict[str, tuple[str, float]] = {}
        self._session_pending_tools: dict[str, int] = {}
        self._active_turn_tasks: dict[str, asyncio.Task[Any]] = {}
        self._websocket_sessions: dict[int, set[str]] = {}
        self.agent = PCBLangGraphAgent(self.config, self.send_tool_call, progress_sender=self.send_progress, use_model_planner=use_model_planner)

    # ====== 功能：向前端发送非 final 的进度消息，并复用当前轮 msgId。 ======
    async def send_progress(self, session_id: str, message: str) -> None:
        connection = self._connections.get(session_id)
        if not connection:
            return
        if message == "正在分析任务..." and self._session_pending_tools.get(session_id, 0) > 0:
            return
        now = time.monotonic()
        last_message, last_at = self._last_progress.get(session_id, ("", 0.0))
        if message == last_message and now - last_at < 5.0:
            return
        self._last_progress[session_id] = (message, now)
        ws, project_id = connection
        msg_id = self._active_message_ids.get(session_id)
        await ws.send(json.dumps(agent_message(session_id, project_id, message, isFinal=False, msgId=msg_id), ensure_ascii=False))

    # ====== 功能：等待任务完成时持续向前端发送心跳进度。 ======
    async def _await_with_progress(self, task: asyncio.Task[Any], session_id: str, message: str, interval: float = 5.0) -> Any:
        while not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=interval)
            except asyncio.TimeoutError:
                await self.send_progress(session_id, message)
        return await task

    # ====== 功能：向模拟或真实前端发送工具调用。 ======
    async def send_tool_call(self, session_id: str, call_id: str, arguments: dict[str, Any], timeout: float) -> Any:
        ws, project_id = self._connections[session_id]
        tool_arguments = dict(arguments)
        tool_name = str(tool_arguments.pop("__tool_name__", "") or "")
        if not tool_name:
            tool_name = self._tool_name_from_pending_context(call_id)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        self._session_pending_tools[session_id] = self._session_pending_tools.get(session_id, 0) + 1
        await self.send_progress(session_id, f"正在调用 {tool_name}...")
        print(f"tool_call_sent call_id={call_id} tool={tool_name} session={session_id} projectID={project_id} pending_count={len(self._pending)}")
        log_debug_event("frontend_tool.sent", {"tool": tool_name, "call_id": call_id, "session_id": session_id, "project_id": project_id, "arguments": tool_arguments, "timeout": timeout})
        await ws.send(json.dumps(tool_call_message(session_id, project_id, call_id, tool_name, tool_arguments), ensure_ascii=False))
        task = asyncio.create_task(asyncio.wait_for(future, timeout=timeout))
        try:
            result = await self._await_with_progress(task, session_id, f"{tool_name} 执行中...", interval=5.0)
            await self.send_progress(session_id, f"{tool_name} 已完成，继续处理...")
            log_debug_event("frontend_tool.result", {"tool": tool_name, "call_id": call_id, "session_id": session_id, "project_id": project_id, "result": result})
            return result
        except Exception as exc:
            log_debug_event("frontend_tool.error", {"tool": tool_name, "call_id": call_id, "session_id": session_id, "project_id": project_id, "error": str(exc)})
            self._pending.pop(call_id, None)
            raise
        finally:
            self._session_pending_tools[session_id] = max(0, self._session_pending_tools.get(session_id, 0) - 1)

    # ====== 功能：为前端未提供 sessionId 的消息复用连接内会话。 ======
    def _resolve_session_id(self, websocket: Any, project_id: str, session_id: str) -> str:
        if session_id:
            return session_id
        key = (id(websocket), project_id)
        if key not in self._connection_sessions:
            self._connection_sessions[key] = str(uuid.uuid4())
        return self._connection_sessions[key]

    # ====== 功能：生成工具结果日志中可读但不刷屏的摘要。 ======
    def _result_summary(self, result: Any) -> str:
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        return str(text).replace("\n", " ")[:240]

    # ====== 功能：执行 _tool_name_from_pending_context 的核心逻辑。 ======
    def _tool_name_from_pending_context(self, call_id: str) -> str:
        # Defensive fallback; FrontendTool normally passes __tool_name__.
        return getattr(self, "_active_frontend_tool_names", {}).get(call_id, "")

    # ====== 功能：后台执行一轮 LangGraph 调用并发送最终结果。 ======
    async def _run_agent_turn(
        self,
        websocket: Any,
        session_id: str,
        project_id: str,
        content: str,
        turn_msg_id: str,
        *,
        entry_module: str = "",
        entry_action: str = "",
        entry_payload: dict[str, Any] | None = None,
    ) -> None:
        try:
            await self.send_progress(session_id, "思考中...")
            task = asyncio.create_task(
                self.agent.ainvoke(
                    session_id,
                    project_id,
                    content,
                    entry_module=entry_module,
                    entry_action=entry_action,
                    entry_payload=entry_payload or {},
                )
            )
            result = await self._await_with_progress(task, session_id, "正在分析任务...", interval=5.0)
            fields: dict[str, Any] = {}
            if result.get("markdown_report"):
                fields["markdownReport"] = result.get("markdown_report")
            if result.get("report_payload"):
                fields["reportPayload"] = result.get("report_payload")
                if isinstance(result.get("report_payload"), dict):
                    for key in ("routingResult", "importLinesFilePath", "routedLayoutTxtFilePath", "report", "workDir"):
                        value = result["report_payload"].get(key)
                        if value not in (None, "", [], {}):
                            fields[key] = value
            if result.get("selection"):
                fields["selection"] = result.get("selection")
            fanout_params = result.get("fanout_params") or (result.get("intermediate_cache", {}) or {}).get("fanoutParams")
            if fanout_params and result.get("workflow_state") == "param_review":
                fields["fanoutParams"] = json.dumps(fanout_params, ensure_ascii=False)
            await websocket.send(json.dumps(agent_message(session_id, project_id, result.get("final_response", ""), isFinal=True, msgId=turn_msg_id, **fields), ensure_ascii=False))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await websocket.send(json.dumps(error_message(session_id, project_id, str(exc)), ensure_ascii=False))
        finally:
            self._active_message_ids.pop(session_id, None)
            self._active_turn_tasks.pop(session_id, None)
            self._last_progress.pop(session_id, None)

    # ====== 功能：处理一条前端用户消息并启动后台 Agent 任务。 ======
    async def _start_agent_turn(
        self,
        websocket: Any,
        session_id: str,
        project_id: str,
        content: str,
        *,
        entry_module: str = "",
        entry_action: str = "",
        entry_payload: dict[str, Any] | None = None,
    ) -> None:
        active = self._active_turn_tasks.get(session_id)
        if active and not active.done():
            self._connections[session_id] = (websocket, project_id)
            await websocket.send(json.dumps(agent_message(session_id, project_id, "上一条 PCB 任务仍在执行，请稍候。", isFinal=False, msgId=self._active_message_ids.get(session_id)), ensure_ascii=False))
            return
        turn_msg_id = str(uuid.uuid4())
        self._connections[session_id] = (websocket, project_id)
        self._websocket_sessions.setdefault(id(websocket), set()).add(session_id)
        self._active_message_ids[session_id] = turn_msg_id
        self._last_progress.pop(session_id, None)
        self._active_turn_tasks[session_id] = asyncio.create_task(
            self._run_agent_turn(
                websocket,
                session_id,
                project_id,
                content,
                turn_msg_id,
                entry_module=entry_module,
                entry_action=entry_action,
                entry_payload=entry_payload or {},
            )
        )

    # ====== 功能：WebSocket 断开时清理连接相关任务和缓存。 ======
    def _cleanup_websocket(self, websocket: Any) -> None:
        sessions = self._websocket_sessions.pop(id(websocket), set())
        for session_id in sessions:
            task = self._active_turn_tasks.pop(session_id, None)
            if task and not task.done():
                task.cancel()
            self._connections.pop(session_id, None)
            self._active_message_ids.pop(session_id, None)
            self._last_progress.pop(session_id, None)
            self._session_pending_tools.pop(session_id, None)
    # ====== 功能：执行 handler 的核心逻辑。 ======
    async def handler(self, websocket: Any) -> None:
        try:
            async for raw in websocket:
                data: dict[str, Any] = {}
                try:
                    data = json.loads(raw)
                    tool_result = parse_tool_result(data)
                    if tool_result:
                        call_id, result = tool_result
                        pending_keys = list(self._pending.keys())
                        future = self._pending.pop(call_id, None)
                        matched = bool(future and not future.done())
                        print(f"tool_result_received call_id={call_id} matched={matched} pending_keys={pending_keys} result_type={type(result).__name__} result_summary={self._result_summary(result)}")
                        if matched and future:
                            future.set_result(result)
                        else:
                            print(f"unmatched_tool_result call_id={call_id} pending_keys={pending_keys}")
                        continue

                    parsed = parse_user_message(data)
                    if parsed is None:
                        continue
                    raw_session_id, project_id, content = parsed
                    session_id = self._resolve_session_id(websocket, project_id, raw_session_id)
                    await self._start_agent_turn(
                        websocket,
                        session_id,
                        project_id,
                        content,
                        entry_module=getattr(parsed, "entry_module", ""),
                        entry_action=getattr(parsed, "entry_action", ""),
                        entry_payload=getattr(parsed, "entry_payload", {}),
                    )
                except Exception as exc:
                    raw_data = data if isinstance(data, dict) else {}
                    payload = raw_data.get("payload") if isinstance(raw_data.get("payload"), dict) else raw_data
                    session_id = str(payload.get("sessionId") or raw_data.get("sessionId") or "")
                    project_id = str(payload.get("projectid") or payload.get("projectId") or payload.get("projectID") or raw_data.get("projectid") or raw_data.get("projectId") or raw_data.get("projectID") or "")
                    await websocket.send(json.dumps(error_message(session_id, project_id, str(exc)), ensure_ascii=False))
        finally:
            self._cleanup_websocket(websocket)

    # ====== 功能：执行 serve 的核心逻辑。 ======
    async def serve(self) -> None:
        try:
            import websockets
        except Exception as exc:
            raise RuntimeError("websockets is required. Install requirements.txt before running the server.") from exc
        async with websockets.serve(self.handler, self.config.server.host, self.config.server.port):
            print(f"PCB LangGraph WebSocket listening on ws://{self.config.server.host}:{self.config.server.port}")
            await asyncio.Future()


# ====== 功能：命令行入口函数。 ======
def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=None)
    parser.add_argument("--rule-planner", action="store_true", help="Disable live pcb-model planning and use deterministic intent rules.")
    args = parser.parse_args()
    server = PCBWebSocketServer(args.config, use_model_planner=not args.rule_planner)
    asyncio.run(server.serve())


if __name__ == "__main__":
    main()
