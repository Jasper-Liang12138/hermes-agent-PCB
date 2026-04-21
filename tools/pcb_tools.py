"""PCB Intelligence Tools - BGA Fanout Routing

Registers PCB-specific tools for BGA fanout routing with Qiyunfang PCB client.

工具调用链路：
  Hermes Agent (executor thread)
       ↓ registry.dispatch() → tool handler (同步)
       ↓ run_coroutine_threadsafe → main event loop
  WebSocketAdapter.send_tool_call()
       ↓ WebSocket message (tool-calls)
  Qiyunfang PCB Client
       ↓ WebSocket message (tool-results)
  WebSocketAdapter._handle_tool_results() → Future.set_result()
       ↑ tool handler 等待结果返回
"""
import json
import asyncio
import subprocess
import os
import uuid
import logging
from concurrent.futures import Future as ThreadFuture
from typing import Dict, Any, Optional
from pathlib import Path

from tools.registry import registry

logger = logging.getLogger(__name__)

_ROUTE_MODE_CHAT = "chat"
_ROUTE_MODE_PCB = "pcb"


# ============================================================================
# WebSocket Transport Singleton
# 保存 adapter 引用、主 event loop 引用、当前活跃 session_id
# ============================================================================

class WebSocketTransportSingleton:
    """
    全局单例，连接 PCB 工具与 WebSocket 适配器。

    - _websocket_adapter: WebSocketAdapter 实例，由 connect() 时注入
    - _main_loop: 主 asyncio event loop，用于 run_coroutine_threadsafe
    - current_session_id: 当前活跃的 WebSocket session（最近一次收到消息的 session）
    """

    _instance = None
    _websocket_adapter = None
    _main_loop: Optional[asyncio.AbstractEventLoop] = None
    current_session_id: Optional[str] = None
    _cached_project_data: Dict[str, str] = {}  # session_id -> getProjectData 结果缓存
    _session_modes: Dict[str, str] = {}  # session_id -> chat/pcb

    @classmethod
    def get_instance(cls) -> "WebSocketTransportSingleton":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_adapter(self, adapter, loop: asyncio.AbstractEventLoop):
        """由 WebSocketAdapter.connect() 调用，注入 adapter 和主 loop。"""
        self._websocket_adapter = adapter
        self._main_loop = loop
        logger.info("PCB transport: adapter and main loop registered")

    def get_adapter(self):
        return self._websocket_adapter

    def get_main_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        return self._main_loop

    def set_session_mode(self, session_id: str, mode: str) -> None:
        if not session_id:
            return
        normalized = _ROUTE_MODE_PCB if mode == _ROUTE_MODE_PCB else _ROUTE_MODE_CHAT
        self._session_modes[session_id] = normalized

    def get_session_mode(self, session_id: Optional[str]) -> str:
        if not session_id:
            return _ROUTE_MODE_CHAT
        return self._session_modes.get(session_id, _ROUTE_MODE_CHAT)

    def is_pcb_mode(self, session_id: Optional[str]) -> bool:
        return self.get_session_mode(session_id) == _ROUTE_MODE_PCB

    def clear_session(self, session_id: str) -> None:
        self._session_modes.pop(session_id, None)
        self._cached_project_data.pop(session_id, None)
        if self.current_session_id == session_id:
            self.current_session_id = None

    def cache_project_data(self, data: str) -> None:
        """保存 getProjectData 返回的版图数据，供 route 工具直接使用。"""
        session_id = self.current_session_id
        if not session_id:
            return
        self._cached_project_data[session_id] = data

    def get_cached_project_data(self) -> Optional[str]:
        session_id = self.current_session_id
        if not session_id:
            return None
        return self._cached_project_data.get(session_id)

    def call_tool_sync(self, tool_name: str, arguments: Dict[str, Any], timeout: float = 30.0) -> Any:
        """
        在 executor 线程中同步调用 WebSocket 工具，等待结果。

        使用 run_coroutine_threadsafe 将协程调度到主 event loop，
        阻塞当前 executor 线程直到结果返回。
        """
        adapter = self._websocket_adapter
        if not adapter:
            raise RuntimeError("WebSocket adapter not initialized. Is the websocket gateway running?")

        loop = self._main_loop
        if not loop or not loop.is_running():
            raise RuntimeError("Main event loop not available")

        session_id = self.current_session_id
        if not session_id:
            raise RuntimeError("No active WebSocket session. Is the PCB client connected?")
        if not self.is_pcb_mode(session_id):
            raise RuntimeError(
                f"Tool '{tool_name}' blocked: session '{session_id}' is in chat mode"
            )

        call_id = f"call_{uuid.uuid4().hex[:8]}"

        # 在主 loop 中调度异步调用，阻塞等待结果
        future = asyncio.run_coroutine_threadsafe(
            adapter.send_tool_call(
                session_id=session_id,
                call_id=call_id,
                tool_name=tool_name,
                arguments=arguments,
                timeout=timeout,
            ),
            loop,
        )

        return future.result(timeout=timeout + 5.0)  # 留 5s 余量


_transport = WebSocketTransportSingleton.get_instance()


def _session_mode_error(tool_name: str) -> str:
    session_id = _transport.current_session_id
    mode = _transport.get_session_mode(session_id)
    return (
        f"工具 {tool_name} 被拒绝：当前会话处于 {mode} 模式。"
        f"请先明确进入 PCB 布线流程后再调用。session={session_id or 'none'}"
    )


# ============================================================================
# Tool 1: getProjectData
# ============================================================================

def get_project_data(projectID: str) -> str:
    """
    获取 PCB 项目数据（S 表达式格式）。

    通过 WebSocket 代理调用启云方 PCB 客户端的 PdslExport.ExportDbData 接口。
    Agent 拿到数据后分析其中的 BGA 元件，生成选择列表。

    Args:
        projectID: PCB 项目的 UUID

    Returns:
        PCB 数据的 S 表达式字符串
    """
    if not _transport.is_pcb_mode(_transport.current_session_id):
        msg = _session_mode_error("getProjectData")
        logger.warning(msg)
        return json.dumps({"error": msg}, ensure_ascii=False)

    try:
        logger.info("getProjectData start: projectID=%s", projectID)
        result = _transport.call_tool_sync(
            tool_name="getProjectData",
            arguments={"projectID": projectID},
            timeout=30.0,
        )
        data = result if isinstance(result, str) else json.dumps(result)
        _transport.cache_project_data(data)  # 缓存供 route 工具使用
        logger.info("getProjectData success: %d chars", len(data))
        return data
    except Exception as e:
        logger.error("getProjectData failed: %s", e)
        return json.dumps({"error": str(e)})


registry.register(
    name="getProjectData",
    toolset="pcb",
    schema={
        "name": "getProjectData",
        "description": (
            "获取 PCB 项目数据（S 表达式格式）。"
            "若 pcb_extract_bga 工具可用，获取数据后立即调用它提取 BGA 列表；"
            "否则直接分析数据识别 BGA 元件。"
            "最终通过 ##PCB_FIELDS## 标记将 selection 返回给用户选择。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "projectID": {
                    "type": "string",
                    "description": "PCB 项目的 UUID，从用户消息的 projectid 字段获取",
                }
            },
            "required": ["projectID"],
        },
    },
    handler=lambda args, **kwargs: get_project_data(args.get("projectID", "")),
    check_fn=lambda: _transport.get_adapter() is not None,
)


# ============================================================================
# Tool 2: GetSelectedElements
# ============================================================================

def get_selected_elements(projectID: str) -> str:
    """
    获取用户在 PCB 中框选的元素 ID 列表。

    通过 WebSocket 代理调用启云方 PCB 客户端的 PdslSelect.GetSelectedElements 接口。
    用于拆线重步场景：用户框选了走线，Agent 获取 ID 后执行重步布线。

    Args:
        projectID: PCB 项目的 UUID

    Returns:
        JSON 字符串: {"ids": ["wire_001", "wire_002", ...]}
    """
    if not _transport.is_pcb_mode(_transport.current_session_id):
        msg = _session_mode_error("GetSelectedElements")
        logger.warning(msg)
        return json.dumps({"error": msg}, ensure_ascii=False)

    try:
        logger.info("GetSelectedElements start: projectID=%s", projectID)
        result = _transport.call_tool_sync(
            tool_name="GetSelectedElements",
            arguments={"projectID": projectID},
            timeout=30.0,
        )
        data = result if isinstance(result, str) else json.dumps(result)
        logger.info("GetSelectedElements success: %d chars", len(data))
        return data
    except Exception as e:
        logger.error(f"GetSelectedElements failed: {e}")
        return json.dumps({"error": str(e)})


registry.register(
    name="GetSelectedElements",
    toolset="pcb",
    schema={
        "name": "GetSelectedElements",
        "description": (
            "获取用户在 PCB 中框选的元素 ID 列表，用于拆线重步功能。"
            "若返回的 ids 为空，提示用户先在 PCB 中框选需要重步的走线（<40 Pin）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "projectID": {
                    "type": "string",
                    "description": "PCB 项目的 UUID",
                }
            },
            "required": ["projectID"],
        },
    },
    handler=lambda args, **kwargs: get_selected_elements(args.get("projectID", "")),
    check_fn=lambda: _transport.get_adapter() is not None,
)


# ============================================================================
# Tool 3: route
# ============================================================================

def route_bga(userData: str) -> str:
    """
    执行 BGA 扇出布线算法（北科大规则布线器）。

    工作流程：
      1. 从 session 缓存取版图数据（getProjectData 调用时自动保存）
      2. 写入输入文件：版图信息.txt, order_input.txt, constraint.txt（可选）
      3. 执行 router.exe
      4. 读取输出文件：routing_input.txt, data.txt
      5. 返回布线结果和报告

    Args:
        userData: 扇出参数 JSON 字符串，格式：
            {
              "orderLines": [{"net": "GND", "layer": "SIG03", "order": 1}, ...],
              "constraints": {"LineWidth": 4, "LineSpacing": 3}
            }
            orderLines 必填；constraints 可选。

    Returns:
        JSON 字符串: {"routingResult": "...", "report": "..."}
    """
    if not _transport.is_pcb_mode(_transport.current_session_id):
        msg = _session_mode_error("route")
        logger.warning(msg)
        return json.dumps({"routingResult": "", "report": msg}, ensure_ascii=False)

    # 解析 userData
    try:
        user_data_obj = json.loads(userData) if isinstance(userData, str) else userData
    except json.JSONDecodeError:
        return json.dumps({"routingResult": "", "report": f"无效的 userData JSON: {userData[:200]}"})

    # 从 session 缓存取版图数据
    project_data = _transport.get_cached_project_data()
    if not project_data:
        return json.dumps({"routingResult": "", "report": "缺少版图数据，请先调用 getProjectData"})

    # 优先走 WebSocket 代理，便于与启云方 PCB 客户端 / test_client 交互联调。
    # 若当前没有活跃 WebSocket session，再回退到本地 router.exe。
    if _transport.get_adapter() is not None and _transport.current_session_id:
        try:
            logger.info("route proxy start via websocket")
            result = _transport.call_tool_sync(
                tool_name="route",
                arguments={"userData": userData},
                timeout=300.0,
            )
            data = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
            logger.info("route proxy success: %d chars", len(data))
            return data
        except Exception as e:
            logger.error("route proxy failed, falling back to local router: %s", e)

    router_cmd = os.getenv("ROUTER_CMD", "router.exe")
    work_dir = Path(os.getenv("ROUTER_WORK_DIR", "."))
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: 写入版图数据
        (work_dir / "版图信息.txt").write_text(project_data, encoding="utf-8")

        # Step 2: 写入 order_input.txt — 每行格式：{线网名} {层名} {布线顺序}
        order_lines = user_data_obj.get("orderLines", [])
        if not order_lines:
            return json.dumps({"routingResult": "", "report": "userData.orderLines 为空，无法布线"})
        logger.info("route local start: %d order lines", len(order_lines))
        order_text = "\n".join(
            f"{item['net']} {item['layer']} {item['order']}"
            for item in order_lines
        )
        (work_dir / "order_input.txt").write_text(order_text, encoding="utf-8")
        logger.info("Wrote order_input.txt: %d lines", len(order_lines))

        # Step 3: 写入 constraint.txt（可选）— 格式：LineWidth {n}\nLineSpacing {n}
        constraints = user_data_obj.get("constraints")
        constraint_path = work_dir / "constraint.txt"
        if constraints and isinstance(constraints, dict):
            line_width = constraints.get("LineWidth")
            line_spacing = constraints.get("LineSpacing")
            if line_width is not None and line_spacing is not None:
                constraint_path.write_text(
                    f"LineWidth {line_width}\nLineSpacing {line_spacing}", encoding="utf-8"
                )
        elif constraint_path.exists():
            constraint_path.unlink()

        # Step 4: 执行布线器
        logger.info("Executing router: %s in %s", router_cmd, work_dir)
        proc = subprocess.run(
            router_cmd,
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=True,
            timeout=300,
        )

        if proc.returncode != 0:
            return json.dumps({
                "routingResult": "",
                "report": f"布线器执行失败 (exit {proc.returncode}):\n{proc.stderr[:500]}",
            })

        # Step 5: 读取输出文件
        result_file = work_dir / "routing_input.txt"
        if not result_file.exists():
            return json.dumps({"routingResult": "", "report": "布线器未生成 routing_input.txt"})

        routing_result = result_file.read_text(encoding="utf-8")
        report_file = work_dir / "data.txt"
        report = report_file.read_text(encoding="utf-8") if report_file.exists() else "布线完成（无详细报告）"

        return json.dumps({"routingResult": routing_result, "report": report}, ensure_ascii=False)

    except subprocess.TimeoutExpired:
        return json.dumps({"routingResult": "", "report": "布线器执行超时（> 5 分钟）"})
    except Exception as e:
        logger.error("Router execution failed: %s", e, exc_info=True)
        return json.dumps({"routingResult": "", "report": f"布线器异常: {str(e)}"})


registry.register(
    name="route",
    toolset="pcb",
    schema={
        "name": "route",
        "description": (
            "执行 BGA 扇出布线算法，生成布线结果和报告。"
            "版图数据由系统自动从缓存获取，无需传入。"
            "执行完成后，用 ##PCB_FIELDS## 标记将 routingResult 返回给用户。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "userData": {
                    "type": "string",
                    "description": (
                        "扇出参数 JSON 字符串，格式：\n"
                        '{"orderLines": [{"net": "GND", "layer": "SIG03", "order": 1}, ...], '
                        '"constraints": {"LineWidth": 4, "LineSpacing": 3}}\n'
                        "orderLines 必填，constraints 可选。"
                    ),
                },
            },
            "required": ["userData"],
        },
    },
    handler=lambda args, **kwargs: route_bga(args.get("userData", "")),
    check_fn=lambda: True,
)


logger.info("PCB tools registered: getProjectData, GetSelectedElements, route")
