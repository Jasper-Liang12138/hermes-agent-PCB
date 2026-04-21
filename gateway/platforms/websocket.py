"""WebSocket platform adapter for PCB intelligence (Qiyunfang protocol).

消息协议（双向）：
  用户消息:   {"sessionId":"...", "projectid":"...", "type":"message",      "body":{"role":"user",  "content":"..."}}
  工具调用:   {"type":"tool-calls",   "body":{"role":"agent", "content":{"id":"...", "name":"...", "arguments":{...}}}}
  工具结果:   {"type":"tool-results", "body":{"role":"tool",  "content":{"id":"...", "result":"..."}}}
  Agent回复:  {"sessionId":"...", "projectid":"...", "type":"message",      "body":{"role":"agent", "msgId":"...", "content":"...", "thinking":"...", "isFinal":true/false/null, [selection/fanoutParams/routingResult]}}
  错误:       {"sessionId":"...", "projectid":"...", "type":"error",        "body":{"role":"agent", "code":50001, "message":"..."}}

结构化字段传递机制：
  Agent 在文本响应中嵌入特殊标记：
    ##PCB_FIELDS##
    {"selection": [...], "fanoutParams": {...}, "routingResult": "..."}
    ##PCB_FIELDS_END##
  WebSocketAdapter.send() 解析并剥离这些标记，将字段放入 body。

流式输出：
  框架调用 edit_message() 推送增量 token，适配器按 isFinal 字段区分中间帧和终帧。
  流式时 isFinal=false，最后一帧 isFinal=true。

思考模式：
  框架把 reasoning 以 ##THINKING## 标记嵌入文本，适配器提取后放入 thinking 字段。
"""
import asyncio
import json
import os
import re
import time
import uuid
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from aiohttp import web
import aiohttp

from gateway.platforms.base import BasePlatformAdapter, MessageEvent, SendResult
from gateway.config import PlatformConfig, Platform
from gateway.session import SessionSource

logger = logging.getLogger(__name__)

# 结构化字段标记
_PCB_FIELDS_PATTERN = re.compile(
    r"\s*##PCB_FIELDS##\s*([\s\S]*?)\s*##PCB_FIELDS_END##\s*",
    re.MULTILINE,
)

# 思考内容标记（框架 show_reasoning 开启时嵌入文本的前缀格式）
_REASONING_PATTERN = re.compile(
    r"^💭 \*\*Reasoning:\*\*\n```\n([\s\S]*?)\n```\n\n",
    re.MULTILINE,
)


_ROUTE_MODE_CHAT = "chat"
_ROUTE_MODE_PCB = "pcb"

_FLOW_IDLE = "idle"
_FLOW_WAIT_SELECTION = "wait_selection"
_FLOW_WAIT_CONFIRM = "wait_confirm"
_FLOW_ROUTING = "routing"

# 高精度触发：必须同时命中动作词 + PCB 领域词，才进入 PCB 主链路。
_PCB_ACTION_RE = re.compile(
    r"(帮我|请|执行|开始|启动|做|进行|重布|重新布|重走|route|reroute|start|run)",
    re.IGNORECASE,
)
_PCB_DOMAIN_RE = re.compile(
    r"(pcb|bga|扇出|逃逸|布线|走线|选中元件|projectdata|getprojectdata|getselectedelements|route)",
    re.IGNORECASE,
)
_SELECTION_RE = re.compile(r"(选择\s*U?\d+|选\s*U?\d+|^U\d+$)", re.IGNORECASE)
_CONFIRM_RE = re.compile(r"(确认|继续|执行|开始布线|开始|go|yes|ok)", re.IGNORECASE)
_CANCEL_RE = re.compile(r"(取消|退出|中止|停止|cancel|abort|exit)", re.IGNORECASE)
_CHAT_ONLY_RE = re.compile(
    r"(不要.*(布线|route|getprojectdata|getselectedelements)|只聊|闲聊|解释|介绍|笑话|今天星期几)",
    re.IGNORECASE,
)


@dataclass
class _RouteDecision:
    mode: str
    immediate_reply: Optional[str] = None
    reason: str = ""


class WebSocketAdapter(BasePlatformAdapter):
    """
    WebSocket 服务器，实现启云方 PCB 协议。

    配置示例（~/.hermes/config.yaml）:
        gateway:
          websocket:
            enabled: true
            host: "0.0.0.0"
            port: 8765

    思考模式（可选）:
        agent:
          reasoning_effort: "high"   # none/minimal/low/medium/high/xhigh
        display:
          show_reasoning: true

    流式输出（可选）:
        streaming:
          enabled: true
    """

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.WEBSOCKET)
        extra = config.extra or {}
        self._host = extra.get("host", "0.0.0.0")
        self._port = int(extra.get("port", 8765))
        self._app = web.Application()
        self._runner: Optional[web.AppRunner] = None

        # session_id -> (WebSocketResponse, project_id)
        self._connections: Dict[str, Tuple[web.WebSocketResponse, str]] = {}

        # call_id -> asyncio.Future，等待 tool-results 回来
        self._pending_tool_calls: Dict[str, asyncio.Future] = {}

        # call_id -> tool_name，用于 BOARD_DATA_USE_FILE_PATH 文件路径模式
        self._pending_tool_names: Dict[str, str] = {}

        # 流式输出：session_id -> 当前 msgId（同一次回复的多帧共享同一 msgId）
        self._stream_msg_ids: Dict[str, str] = {}
        self._session_tasks: Dict[str, set[asyncio.Task]] = {}
        self._stream_fields_fingerprint: Dict[str, str] = {}

        # 会话路由状态：用于“PCB主链路(FSM) + 普通聊天”双通道切换
        self._session_modes: Dict[str, str] = {}
        self._session_mode_lock_until: Dict[str, float] = {}
        self._session_flow_states: Dict[str, str] = {}
        self._route_lock_seconds = float(extra.get("route_lock_seconds", 90))

    # -------------------------------------------------------------------------
    # Gateway lifecycle
    # -------------------------------------------------------------------------

    async def connect(self) -> bool:
        """启动 WebSocket 服务器，并把自己注册到 PCB 工具单例。"""
        self._app.router.add_get("/", self._websocket_handler)
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._port)
        await site.start()
        logger.info("PCB WebSocket server listening on ws://%s:%d", self._host, self._port)

        # 把自己和当前 event loop 注册到工具单例，工具层通过它与 PCB 客户端通信
        try:
            from tools.pcb_tools import WebSocketTransportSingleton
            WebSocketTransportSingleton.get_instance().set_adapter(
                adapter=self,
                loop=asyncio.get_event_loop(),
            )
        except ImportError:
            logger.warning("pcb_tools not found; PCB tool proxy will be unavailable")

        return True

    async def disconnect(self) -> None:
        """停止 WebSocket 服务器。"""
        for ws, _ in list(self._connections.values()):
            await ws.close()
        self._connections.clear()
        self._pending_tool_calls.clear()
        self._pending_tool_names.clear()
        self._stream_msg_ids.clear()
        self._stream_fields_fingerprint.clear()
        self._session_modes.clear()
        self._session_mode_lock_until.clear()
        self._session_flow_states.clear()
        if self._runner:
            await self._runner.cleanup()

    # -------------------------------------------------------------------------
    # WebSocket 连接处理
    # -------------------------------------------------------------------------

    async def _websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        """处理来自启云方 PCB 客户端的 WebSocket 连接。"""
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        logger.info("PCB client connected: %s", request.remote)

        session_id = None
        try:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except json.JSONDecodeError:
                        logger.error("Invalid JSON: %s", msg.data[:200])
                        continue

                    msg_type = data.get("type")

                    if msg_type == "message":
                        session_id = data.get("sessionId") or f"ws_{id(ws)}"
                        project_id = data.get("projectid", "")
                        self._connections[session_id] = (ws, project_id)

                        try:
                            # 新 session 默认走聊天通道；后续由路由器切换到 PCB 通道
                            if session_id not in self._session_modes:
                                self._set_session_mode(session_id, _ROUTE_MODE_CHAT, lock_seconds=0.0)
                            if session_id not in self._session_flow_states:
                                self._set_flow_state(session_id, _FLOW_IDLE)

                            # 更新工具单例的当前 session_id
                            try:
                                from tools.pcb_tools import WebSocketTransportSingleton
                                WebSocketTransportSingleton.get_instance().current_session_id = session_id
                            except ImportError:
                                pass

                            # 每条新消息开启新的 msgId（流式时同一回复共用）
                            self._stream_msg_ids[session_id] = uuid.uuid4().hex[:12]
                            self._stream_fields_fingerprint.pop(session_id, None)
                            task = asyncio.create_task(
                                self._handle_user_message(ws, data, session_id, project_id)
                            )
                            self._session_tasks.setdefault(session_id, set()).add(task)
                            task.add_done_callback(
                                lambda t, sid=session_id: self._on_session_task_done(sid, t)
                            )
                        except Exception:
                            logger.exception("Error dispatching message for session %s", session_id)
                            await self._send_error(ws, "内部错误，请重试", session_id=session_id, project_id=project_id)

                    elif msg_type == "tool-results":
                        self._resolve_tool_result(data)

                    else:
                        logger.warning("Unknown message type: %s", msg_type)

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    logger.error("WebSocket error: %s", ws.exception())

        finally:
            if session_id:
                self._connections.pop(session_id, None)
                self._stream_msg_ids.pop(session_id, None)
                self._stream_fields_fingerprint.pop(session_id, None)
                self._session_modes.pop(session_id, None)
                self._session_mode_lock_until.pop(session_id, None)
                self._session_flow_states.pop(session_id, None)
                for task in list(self._session_tasks.pop(session_id, set())):
                    task.cancel()
                try:
                    from tools.pcb_tools import WebSocketTransportSingleton
                    WebSocketTransportSingleton.get_instance().clear_session(session_id)
                except ImportError:
                    pass
            logger.info("PCB client disconnected: %s", request.remote)

        return ws

    async def _handle_user_message(
        self,
        ws: web.WebSocketResponse,
        data: Dict[str, Any],
        session_id: str,
        project_id: str,
    ):
        """将用户消息转换为 Hermes MessageEvent，转发给 Agent。"""
        body = data.get("body", {})
        user_text = body.get("content", "")
        turn_options = body.get("options", {})
        if not isinstance(turn_options, dict):
            turn_options = {}

        decision = self._decide_route(session_id, user_text)
        if decision.immediate_reply:
            await self._send_router_reply(session_id, decision.immediate_reply)
            logger.info(
                "Router short-circuit: session=%s mode=%s reason=%s",
                session_id,
                decision.mode,
                decision.reason,
            )
            return

        auto_skill = "hardware/pcb-intelligence" if decision.mode == _ROUTE_MODE_PCB else None
        if decision.mode == _ROUTE_MODE_PCB:
            self._set_session_mode(session_id, _ROUTE_MODE_PCB)
        else:
            self._set_session_mode(session_id, _ROUTE_MODE_CHAT, lock_seconds=0.0)

        # 将 projectid 作为系统上下文注入到用户消息中，
        # 避免 agent 向用户询问 projectID（它已经在 WebSocket 消息里了）
        if project_id and user_text:
            user_text = f"[projectid: {project_id}]\n{user_text}"

        event = MessageEvent(
            text=user_text,
            source=SessionSource(
                platform=Platform.WEBSOCKET,
                chat_id=session_id,
                user_id=session_id,
                chat_type="dm",
                chat_name=f"WebSocket:{session_id}",
            ),
            raw_message={
                "projectid": project_id,
                "sessionId": session_id,
                "options": turn_options,
            },
            auto_skill=auto_skill,
        )

        if self._message_handler:
            response = await self._message_handler(event)
            if response:
                await self.send(
                    chat_id=session_id,
                    content=response,
                )

    def _on_session_task_done(self, session_id: str, task: asyncio.Task) -> None:
        """Drop completed per-session tasks and log unexpected failures."""
        tasks = self._session_tasks.get(session_id)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._session_tasks.pop(session_id, None)
        try:
            task.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("WebSocket session task failed: %s", session_id)

    def _resolve_tool_result(self, data: Dict[str, Any]):
        """收到 tool-results 时，解析 call_id，resolve 对应的 Future。

        当 BOARD_DATA_USE_FILE_PATH=1 时，getProjectData 返回的是文件路径字符串，
        此处自动读取文件内容，再 resolve Future，对上层工具透明。
        """
        content = data.get("body", {}).get("content", {})
        call_id = content.get("id")
        result = content.get("result")

        if not call_id:
            logger.warning("tool-results missing id: %s", data)
            return

        # 文件路径模式：getProjectData 返回文件路径，读取内容后再传给 Agent
        result = self._maybe_read_file_result(call_id, result)
        logger.info("Resolved tool result: call_id=%s", call_id)

        future = self._pending_tool_calls.pop(call_id, None)
        self._pending_tool_names.pop(call_id, None)
        if future and not future.done():
            future.set_result(result)
        else:
            logger.warning("No pending tool call for id: %s", call_id)

    def _maybe_read_file_result(self, call_id: str, result: Any) -> Any:
        """
        若 BOARD_DATA_USE_FILE_PATH=1 且本次调用是 getProjectData，
        则将 result（文件路径字符串）替换为文件内容。
        """
        use_file_path = os.environ.get("BOARD_DATA_USE_FILE_PATH", "").lower() in {"1", "true", "yes", "on"}
        if not use_file_path:
            return result

        tool_name = self._pending_tool_names.get(call_id)
        if tool_name != "getProjectData":
            return result

        if not isinstance(result, str):
            logger.warning("BOARD_DATA_USE_FILE_PATH=1 but result is not a string: %r", type(result))
            return result

        path = Path(result)
        if not path.is_file():
            logger.warning("BOARD_DATA_USE_FILE_PATH=1 but path is not a file: %s", result)
            return result

        try:
            content = path.read_text(encoding="utf-8")
            logger.info("Read getProjectData from file: %s (%d chars)", result, len(content))
            return content
        except OSError as e:
            logger.warning("Failed to read getProjectData file %s: %s", result, e)
            return result

    # -------------------------------------------------------------------------
    # 发送消息给 PCB 客户端
    # -------------------------------------------------------------------------

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        将 Agent 最终响应发回给 PCB 客户端（非流式，isFinal=null）。

        处理：
        1. 提取 ##THINKING## 块 → thinking 字段
        2. 提取 ##PCB_FIELDS## 块 → selection/fanoutParams/routingResult 字段
        3. 剩余文本 → content 字段
        """
        ws_info = self._connections.get(chat_id)
        if not ws_info:
            return SendResult(success=False, error=f"Session not found: {chat_id}")

        ws, project_id = ws_info
        if ws.closed:
            return SendResult(success=False, error="Connection closed")

        # 1. 提取思考内容（框架 show_reasoning 注入的前缀格式）
        thinking, content_no_thinking = self._extract_thinking(content)

        # 2. 提取 PCB 结构化字段
        clean_content, pcb_fields = self._extract_pcb_fields(content_no_thinking)

        msg_id = self._stream_msg_ids.get(chat_id, uuid.uuid4().hex[:12])

        body: Dict[str, Any] = {
            "msgId": msg_id,
            "role": "agent",
            "content": clean_content,
            "isFinal": None,  # 非流式
        }

        if thinking:
            body["thinking"] = thinking

        # 注入 PCB 结构化字段
        for key in ("selection", "fanoutParams", "routingResult"):
            if key in pcb_fields:
                body[key] = pcb_fields[key]

        self._update_route_state_from_fields(chat_id, pcb_fields)

        message = {
            "sessionId": chat_id,
            "projectid": project_id,
            "type": "message",
            "body": body,
        }

        try:
            await ws.send_json(message)
            logger.info(
                "Sent websocket message: session=%s msg_id=%s isFinal=%s keys=%s",
                chat_id,
                msg_id,
                body.get("isFinal"),
                sorted(body.keys()),
            )
            return SendResult(success=True, message_id=msg_id)
        except Exception as e:
            logger.error("Failed to send message to %s: %s", chat_id, e)
            return SendResult(success=False, error=str(e))

    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        is_final: bool = False,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        """
        流式输出接口，框架在 streaming 模式下逐帧调用此方法。

        isFinal=false 表示中间帧（还在生成），isFinal=true 表示最终帧。
        同一次回复共用同一个 msgId，客户端靠 msgId 识别是否为追加内容。
        """
        ws_info = self._connections.get(chat_id)
        if not ws_info:
            return SendResult(success=False, error=f"Session not found: {chat_id}")

        ws, project_id = ws_info
        if ws.closed:
            return SendResult(success=False, error="Connection closed")

        thinking, content_no_thinking = self._extract_thinking(content)
        clean_content, pcb_fields = self._extract_pcb_fields(content_no_thinking)

        # 流式场景里字段块常常先于 true 终帧出现；检测到完整字段后立即下发。
        outbound_is_final: Optional[bool] = is_final
        emitted_fields = pcb_fields
        if not is_final and pcb_fields:
            fp = self._pcb_fields_fingerprint(pcb_fields)
            if fp == self._stream_fields_fingerprint.get(chat_id):
                emitted_fields = {}
            else:
                self._stream_fields_fingerprint[chat_id] = fp
                # 兼容依赖“非 false 帧”消费结构化字段的客户端。
                outbound_is_final = None

        msg_id = self._stream_msg_ids.get(chat_id, message_id)

        body: Dict[str, Any] = {
            "msgId": msg_id,
            "role": "agent",
            "content": clean_content,
            "isFinal": outbound_is_final,
        }

        if thinking:
            body["thinking"] = thinking

        for key in ("selection", "fanoutParams", "routingResult"):
            if key in emitted_fields:
                body[key] = emitted_fields[key]

        if emitted_fields:
            self._update_route_state_from_fields(chat_id, emitted_fields)
        if is_final:
            self._stream_fields_fingerprint.pop(chat_id, None)

        message = {
            "sessionId": chat_id,
            "projectid": project_id,
            "type": "message",
            "body": body,
        }

        try:
            await ws.send_json(message)
            logger.info(
                "Sent websocket delta: session=%s msg_id=%s isFinal=%s keys=%s",
                chat_id,
                msg_id,
                body.get("isFinal"),
                sorted(body.keys()),
            )
            return SendResult(success=True, message_id=msg_id)
        except Exception as e:
            logger.error("Failed to send stream delta to %s: %s", chat_id, e)
            return SendResult(success=False, error=str(e))

    async def send_tool_call(
        self,
        session_id: str,
        call_id: str,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout: float = 30.0,
    ) -> Any:
        """
        向 PCB 客户端发送工具调用请求，等待结果返回。

        在主 event loop 中运行（由 pcb_tools.py 通过 run_coroutine_threadsafe 调度）。
        """
        ws_info = self._connections.get(session_id)
        if not ws_info:
            raise RuntimeError(f"Session not found: {session_id}")

        ws, _ = ws_info
        if ws.closed:
            raise RuntimeError("Connection closed")

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_tool_calls[call_id] = future
        self._pending_tool_names[call_id] = tool_name

        message = {
            "type": "tool-calls",
            "body": {
                "role": "agent",
                "content": {
                    "id": call_id,
                    "name": tool_name,
                    "arguments": arguments,
                },
            },
        }

        try:
            logger.info("Sending tool call: session=%s call_id=%s tool=%s", session_id, call_id, tool_name)
            await ws.send_json(message)
        except Exception as e:
            self._pending_tool_calls.pop(call_id, None)
            raise RuntimeError(f"Failed to send tool call: {e}") from e

        try:
            result = await asyncio.wait_for(future, timeout=timeout)
            logger.info("Tool call completed: call_id=%s tool=%s", call_id, tool_name)
            return result
        except asyncio.TimeoutError:
            self._pending_tool_calls.pop(call_id, None)
            self._pending_tool_names.pop(call_id, None)
            raise TimeoutError(f"Tool call '{tool_name}' timed out after {timeout}s")

    async def _send_error(
        self,
        ws: web.WebSocketResponse,
        message: str,
        code: int = 50001,
        session_id: str = "",
        project_id: str = "",
    ):
        """向 PCB 客户端发送错误消息。"""
        payload = {
            "sessionId": session_id,
            "projectid": project_id,
            "type": "error",
            "body": {
                "role": "agent",
                "code": code,
                "message": message,
            },
        }
        try:
            await ws.send_json(payload)
        except Exception as e:
            logger.error("Failed to send error message: %s", e)

    # -------------------------------------------------------------------------
    # 工具方法
    # -------------------------------------------------------------------------

    def _session_mode(self, session_id: str) -> str:
        return self._session_modes.get(session_id, _ROUTE_MODE_CHAT)

    def _is_mode_locked(self, session_id: str) -> bool:
        return time.time() < self._session_mode_lock_until.get(session_id, 0.0)

    def _set_session_mode(
        self,
        session_id: str,
        mode: str,
        lock_seconds: Optional[float] = None,
    ) -> None:
        self._session_modes[session_id] = mode
        ttl = self._route_lock_seconds if lock_seconds is None else max(0.0, lock_seconds)
        self._session_mode_lock_until[session_id] = time.time() + ttl if (mode == _ROUTE_MODE_PCB and ttl > 0) else 0.0
        self._sync_transport_mode(session_id, mode)

    def _set_flow_state(self, session_id: str, flow_state: str) -> None:
        self._session_flow_states[session_id] = flow_state

    def _reset_flow(self, session_id: str) -> None:
        self._set_flow_state(session_id, _FLOW_IDLE)

    @staticmethod
    def _is_strong_pcb_intent(text: str) -> bool:
        return bool(
            (_PCB_ACTION_RE.search(text) and _PCB_DOMAIN_RE.search(text))
            or _SELECTION_RE.search(text)
        )

    def _decide_route(self, session_id: str, user_text: str) -> _RouteDecision:
        text = (user_text or "").strip()
        if not text:
            return _RouteDecision(mode=_ROUTE_MODE_CHAT, reason="empty")

        flow_state = self._session_flow_states.get(session_id, _FLOW_IDLE)
        mode = self._session_mode(session_id)

        if _CHAT_ONLY_RE.search(text):
            self._reset_flow(session_id)
            return _RouteDecision(mode=_ROUTE_MODE_CHAT, reason="chat_only")

        if _CANCEL_RE.search(text):
            if flow_state != _FLOW_IDLE or mode == _ROUTE_MODE_PCB:
                self._reset_flow(session_id)
                self._set_session_mode(session_id, _ROUTE_MODE_CHAT, lock_seconds=0.0)
                return _RouteDecision(
                    mode=_ROUTE_MODE_CHAT,
                    immediate_reply="已退出 PCB 布线流程，我们回到普通聊天。",
                    reason="cancel_flow",
                )
            return _RouteDecision(mode=_ROUTE_MODE_CHAT, reason="cancel_chat")

        if flow_state == _FLOW_ROUTING:
            return _RouteDecision(
                mode=_ROUTE_MODE_PCB,
                immediate_reply="正在执行布线，请稍候结果返回。若要终止，请回复“取消”。",
                reason="routing_in_progress",
            )

        if flow_state == _FLOW_WAIT_SELECTION:
            if _SELECTION_RE.search(text):
                return _RouteDecision(mode=_ROUTE_MODE_PCB, reason="selection_step")
            if _CONFIRM_RE.search(text):
                return _RouteDecision(
                    mode=_ROUTE_MODE_PCB,
                    immediate_reply="当前还在选择阶段，请先回复器件，例如“选择 U27”，或回复“取消”。",
                    reason="confirm_before_selection",
                )
            return _RouteDecision(
                mode=_ROUTE_MODE_PCB,
                immediate_reply="请先选择目标器件（例如“选择 U27”），或回复“取消”退出。",
                reason="invalid_selection_turn",
            )

        if flow_state == _FLOW_WAIT_CONFIRM:
            if _CONFIRM_RE.search(text):
                self._set_flow_state(session_id, _FLOW_ROUTING)
                return _RouteDecision(mode=_ROUTE_MODE_PCB, reason="confirm_route")
            if _SELECTION_RE.search(text):
                return _RouteDecision(mode=_ROUTE_MODE_PCB, reason="reselect_before_confirm")
            return _RouteDecision(
                mode=_ROUTE_MODE_PCB,
                immediate_reply="请回复“确认”执行布线，或回复“取消”退出。",
                reason="invalid_confirm_turn",
            )

        if self._is_strong_pcb_intent(text):
            return _RouteDecision(mode=_ROUTE_MODE_PCB, reason="strong_pcb_intent")

        # PCB 会话锁定窗口：允许短指令（如“确认”“选 U27”）继续走 PCB，不被闲聊污染。
        if mode == _ROUTE_MODE_PCB and self._is_mode_locked(session_id):
            if _CONFIRM_RE.search(text) or _SELECTION_RE.search(text) or _PCB_DOMAIN_RE.search(text):
                return _RouteDecision(mode=_ROUTE_MODE_PCB, reason="pcb_mode_locked")

        return _RouteDecision(mode=_ROUTE_MODE_CHAT, reason="default_chat")

    async def _send_router_reply(self, session_id: str, message: str) -> None:
        await self.send(chat_id=session_id, content=message)

    def _update_route_state_from_fields(self, session_id: str, pcb_fields: Dict[str, Any]) -> None:
        if not pcb_fields:
            return

        if "routingResult" in pcb_fields:
            self._reset_flow(session_id)
            self._set_session_mode(session_id, _ROUTE_MODE_CHAT, lock_seconds=0.0)
            return

        if "fanoutParams" in pcb_fields:
            self._set_session_mode(session_id, _ROUTE_MODE_PCB)
            self._set_flow_state(session_id, _FLOW_WAIT_CONFIRM)
            return

        if "selection" in pcb_fields:
            self._set_session_mode(session_id, _ROUTE_MODE_PCB)
            self._set_flow_state(session_id, _FLOW_WAIT_SELECTION)

    @staticmethod
    def _pcb_fields_fingerprint(pcb_fields: Dict[str, Any]) -> str:
        try:
            return json.dumps(pcb_fields, ensure_ascii=False, sort_keys=True)
        except (TypeError, ValueError):
            return repr(pcb_fields)

    @staticmethod
    def _sync_transport_mode(session_id: str, mode: str) -> None:
        try:
            from tools.pcb_tools import WebSocketTransportSingleton
            WebSocketTransportSingleton.get_instance().set_session_mode(session_id, mode)
        except ImportError:
            pass

    @staticmethod
    def _extract_thinking(content: str) -> Tuple[Optional[str], str]:
        """
        提取框架注入的 reasoning 前缀。

        框架在 show_reasoning=true 时，把思考过程以如下格式拼到文本开头：
            💭 **Reasoning:**
            ```
            <thinking content>
            ```

        提取后放入协议的 thinking 字段，不展示给用户作为正文。
        """
        match = _REASONING_PATTERN.match(content)
        if match:
            thinking = match.group(1)
            rest = content[match.end():]
            return thinking, rest
        return None, content

    @staticmethod
    def _extract_pcb_fields(content: str) -> Tuple[str, Dict[str, Any]]:
        """
        从响应文本中提取 ##PCB_FIELDS## 标记内的结构化字段。

        Agent 系统提示词指示模型在需要返回结构化数据时输出：

            ##PCB_FIELDS##
            {"selection": [...], "fanoutParams": {...}, "routingResult": "..."}
            ##PCB_FIELDS_END##

        此方法将标记从文本中剥离，返回：
          - clean_content: 不含标记的纯文本
          - fields: 解析出的结构化字段 dict
        """
        fields: Dict[str, Any] = {}
        clean = content

        for match in _PCB_FIELDS_PATTERN.finditer(content):
            try:
                raw_payload = match.group(1).strip()
                if raw_payload.startswith("```"):
                    raw_payload = re.sub(r"^```(?:json)?\s*", "", raw_payload, flags=re.IGNORECASE).strip()
                    raw_payload = re.sub(r"\s*```$", "", raw_payload).strip()

                try:
                    data = json.loads(raw_payload)
                except json.JSONDecodeError:
                    # 兼容模型在字段块前后夹杂说明文字的情况，回退到首尾 JSON 对象截取。
                    start = raw_payload.find("{")
                    end = raw_payload.rfind("}")
                    if start >= 0 and end > start:
                        data = json.loads(raw_payload[start:end + 1])
                    else:
                        raise
                if isinstance(data, dict):
                    fields.update(data)
            except (json.JSONDecodeError, ValueError) as e:
                logger.warning("Failed to parse PCB_FIELDS: %s | content: %s", e, match.group(1)[:200])
            clean = clean.replace(match.group(0), "")

        return clean.strip(), fields

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        return {"name": chat_id, "type": "websocket", "chat_id": chat_id}
