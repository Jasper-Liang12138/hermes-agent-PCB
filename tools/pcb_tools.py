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
import shlex
import uuid
import logging
import re
from concurrent.futures import Future as ThreadFuture
from typing import Dict, Any, Optional
from pathlib import Path

from tools.registry import registry

logger = logging.getLogger(__name__)

_ROUTE_MODE_CHAT = "chat"
_ROUTE_MODE_PCB = "pcb"


def _router_command_args(router_cmd: str) -> list[str]:
    """Build subprocess args without shell so Windows Unicode paths are safe."""
    router_cmd = router_cmd.strip() or "router.exe"
    expanded_cmd = os.path.expandvars(os.path.expanduser(router_cmd))
    if Path(expanded_cmd).exists():
        return [expanded_cmd]
    try:
        parts = shlex.split(router_cmd, posix=False)
    except ValueError:
        return [router_cmd]
    return [part.strip("\"") for part in parts] or ["router.exe"]


def _clean_component_refdes(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("label") or value.get("name") or value.get("refdes")
    if not isinstance(value, str):
        return None
    cleaned = value.strip().strip("`'\"").strip("，。,.!?！？:：;；")
    if not cleaned:
        return None
    match = re.search(r"[A-Za-z_][A-Za-z0-9_.-]*", cleaned)
    return match.group(0) if match else None


def _component_from_payload(*payloads: Any) -> Optional[str]:
    keys = (
        "selectedBGA",
        "selected_bga",
        "selectedBga",
        "targetRefdes",
        "target_refdes",
        "component",
        "refdes",
        "label",
    )
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        for key in keys:
            refdes = _clean_component_refdes(payload.get(key))
            if refdes:
                return refdes
    return None


def _component_from_session(session_id: Optional[str]) -> Optional[str]:
    try:
        adapter = _transport.get_adapter()
        selected_targets = getattr(adapter, "_session_selected_targets", {}) or {}
        return _clean_component_refdes(selected_targets.get(session_id))
    except Exception:
        return None


def _infer_component_from_project_data(project_data: str) -> Optional[str]:
    if not isinstance(project_data, str) or not project_data.strip():
        return None

    quoted_matches = list(re.finditer(r'\(component\s+"([^"]+)"', project_data))
    for index, match in enumerate(quoted_matches):
        label = match.group(1).strip()
        next_start = quoted_matches[index + 1].start() if index + 1 < len(quoted_matches) else len(project_data)
        block = project_data[match.start():next_start]
        if "bga" in block.lower():
            return label
    if quoted_matches:
        return quoted_matches[0].group(1).strip()

    named_matches = list(re.finditer(r'\(component\s+\(name\s+"([^"]+)"\)', project_data))
    for index, match in enumerate(named_matches):
        label = match.group(1).strip()
        next_start = named_matches[index + 1].start() if index + 1 < len(named_matches) else len(project_data)
        block = project_data[match.start():next_start]
        if "bga" in block.lower():
            return label
    if named_matches:
        return named_matches[0].group(1).strip()

    return None


def _resolve_component_refdes(user_data_obj: Any, route_params: Any, session_id: Optional[str], project_data: str) -> str:
    return (
        _component_from_payload(user_data_obj, route_params)
        or _component_from_session(session_id)
        or _infer_component_from_project_data(project_data)
        or "U1"
    )


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
    _cached_reroute_context: Dict[str, Dict[str, Any]] = {}  # session_id -> drop_net 结果缓存
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

    def resolve_session_id(self, session_id: Optional[str] = None) -> Optional[str]:
        candidate = str(session_id or "").strip()
        if candidate:
            if candidate in self._session_modes or candidate in self._cached_project_data:
                return candidate
            try:
                connections = getattr(self._websocket_adapter, "_connections", {}) or {}
                if candidate in connections:
                    return candidate
            except Exception:
                pass
        if self.current_session_id:
            return self.current_session_id
        return candidate or None

    def set_session_mode(self, session_id: str, mode: str) -> None:
        if not session_id:
            return
        normalized = _ROUTE_MODE_PCB if mode == _ROUTE_MODE_PCB else _ROUTE_MODE_CHAT
        self._session_modes[session_id] = normalized

    def get_session_mode(self, session_id: Optional[str]) -> str:
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return _ROUTE_MODE_CHAT
        return self._session_modes.get(session_id, _ROUTE_MODE_CHAT)

    def is_pcb_mode(self, session_id: Optional[str]) -> bool:
        return self.get_session_mode(session_id) == _ROUTE_MODE_PCB

    def clear_session(self, session_id: str) -> None:
        self._session_modes.pop(session_id, None)
        self._cached_project_data.pop(session_id, None)
        self._cached_reroute_context.pop(session_id, None)
        if self.current_session_id == session_id:
            self.current_session_id = None

    def cache_project_data(self, data: str, session_id: Optional[str] = None) -> None:
        """保存 getProjectData 返回的版图数据，供 route 工具直接使用。"""
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return
        self._cached_project_data[session_id] = data

    def get_cached_project_data(self, session_id: Optional[str] = None) -> Optional[str]:
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return None
        return self._cached_project_data.get(session_id)

    def cache_reroute_context(self, data: Dict[str, Any], session_id: Optional[str] = None) -> None:
        """保存 drop_net 返回的拆线上下文，供 reroute 工具使用。"""
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return
        self._cached_reroute_context[session_id] = data

    def get_cached_reroute_context(self, session_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        session_id = self.resolve_session_id(session_id)
        if not session_id:
            return None
        return self._cached_reroute_context.get(session_id)

    def call_tool_sync(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        timeout: float = 30.0,
        session_id: Optional[str] = None,
    ) -> Any:
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

        session_id = self.resolve_session_id(session_id)
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


def _session_mode_error(tool_name: str, session_id: Optional[str] = None) -> str:
    session_id = _transport.resolve_session_id(session_id)
    mode = _transport.get_session_mode(session_id)
    return (
        f"工具 {tool_name} 被拒绝：当前会话处于 {mode} 模式。"
        f"请先明确进入 PCB 布线流程后再调用。session={session_id or 'none'}"
    )


# ============================================================================
# Tool 1: getProjectData
# ============================================================================

def get_project_data(session_id: Optional[str] = None) -> str:
    """
    获取 PCB 项目数据（S 表达式格式）。

    通过 WebSocket 代理调用启云方 PCB 客户端的 PdslExport.ExportDbData 接口。
    Agent 拿到数据后分析其中的 BGA 元件，生成选择列表。

    Returns:
        PCB 数据的 S 表达式字符串
    """
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("getProjectData", session_id)
        logger.warning(msg)
        return json.dumps({"error": msg}, ensure_ascii=False)

    try:
        logger.info("getProjectData start")
        result = _transport.call_tool_sync(
            tool_name="getProjectData",
            arguments={},
            timeout=30.0,
            session_id=session_id,
        )
        data = result if isinstance(result, str) else json.dumps(result)
        _transport.cache_project_data(data, session_id=session_id)  # 缓存供 route 工具使用
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
                    "description": "兼容旧版保留字段；当前前端工具获取当前打开版图，无需传参",
                }
            },
            "required": [],
        },
    },
    handler=lambda args, **kwargs: get_project_data(session_id=kwargs.get("session_id")),
    check_fn=lambda: _transport.get_adapter() is not None,
)


# ============================================================================
# Tool 2: GetSelectedElements
# ============================================================================

def get_selected_elements(projectID: str, session_id: Optional[str] = None) -> str:
    """
    获取用户在 PCB 中框选的元素 ID 列表。

    通过 WebSocket 代理调用启云方 PCB 客户端的 PdslSelect.GetSelectedElements 接口。
    用于拆线重步场景：用户框选了走线，Agent 获取 ID 后执行重步布线。

    Args:
        projectID: PCB 项目的 UUID

    Returns:
        JSON 字符串: {"ids": ["wire_001", "wire_002", ...]}
    """
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("GetSelectedElements", session_id)
        logger.warning(msg)
        return json.dumps({"error": msg}, ensure_ascii=False)

    try:
        logger.info("GetSelectedElements start: projectID=%s", projectID)
        result = _transport.call_tool_sync(
            tool_name="GetSelectedElements",
            arguments={"projectID": projectID},
            timeout=30.0,
            session_id=session_id,
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
    handler=lambda args, **kwargs: get_selected_elements(
        args.get("projectID", ""),
        session_id=kwargs.get("session_id"),
    ),
    check_fn=lambda: _transport.get_adapter() is not None,
)


# ============================================================================
# Tool 3: route
# ============================================================================

def route_bga(userData: str, session_id: Optional[str] = None) -> str:
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
              "selectedBGA": "U27",
              "constraints": {"LineWidth": 4, "LineSpacing": 3}
            }
            orderLines 必填；selectedBGA 建议传入；constraints 可选。

    Returns:
        JSON 字符串: {"routingResult": "...", "report": "..."}
    """
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("route", session_id)
        logger.warning(msg)
        return json.dumps({"routingResult": "", "report": msg}, ensure_ascii=False)

    # 解析 userData
    try:
        user_data_obj = json.loads(userData) if isinstance(userData, str) else userData
    except json.JSONDecodeError:
        return json.dumps({"routingResult": "", "report": f"无效的 userData JSON: {userData[:200]}"})

    # 从 session 缓存取版图数据
    project_data = _transport.get_cached_project_data(session_id=session_id)
    if not project_data:
        return json.dumps({"routingResult": "", "report": "缺少版图数据，请先调用 getProjectData"})

    router_cmd = os.getenv("ROUTER_CMD", "router.exe")
    work_dir = Path(os.getenv("ROUTER_WORK_DIR", "."))
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        logger.info("route local start: session=%s router=%s work_dir=%s", session_id, router_cmd, work_dir)
        # Step 1: 写入版图数据
        (work_dir / "版图信息.txt").write_text(project_data, encoding="utf-8")

        route_params = user_data_obj.get("fanoutParams") if isinstance(user_data_obj.get("fanoutParams"), dict) else user_data_obj

        # Step 2: 写入 order_input.txt — 每行格式：{线网名} {层名} {布线顺序}，最后一行是器件位号
        order_lines = route_params.get("orderLines", [])
        if not order_lines:
            return json.dumps({"routingResult": "", "report": "userData.orderLines 为空，无法布线"})
        logger.info("route local start: %d order lines", len(order_lines))
        component_refdes = _resolve_component_refdes(user_data_obj, route_params, session_id, project_data)
        order_text = "\n".join(
            f"{item['net']} {item['layer']} {item['order']}"
            for item in order_lines
        )
        order_text = f"{order_text}\n\n{component_refdes}"
        (work_dir / "order_input.txt").write_text(order_text, encoding="utf-8")
        logger.info("Wrote order_input.txt: %d lines, component=%s", len(order_lines), component_refdes)

        # Step 3: 写入 constraint.txt（可选）— 格式：LineWidth {n}\nLineSpacing {n}
        constraints = route_params.get("constraints") or user_data_obj.get("constraints")
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
        router_args = _router_command_args(router_cmd)
        router_args.extend(["--component", component_refdes])
        logger.info("Executing router: %s in %s", router_args, work_dir)
        proc = subprocess.run(
            router_args,
            cwd=work_dir,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )

        if proc.returncode != 0:
            output = "\n".join(part for part in [proc.stdout.strip(), proc.stderr.strip()] if part)
            return json.dumps({
                "routingResult": "",
                "report": f"布线器执行失败 (exit {proc.returncode}):\n{output[:1000]}",
            }, ensure_ascii=False)

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
                        '"selectedBGA": "U27", "constraints": {"LineWidth": 4, "LineSpacing": 3}}\n'
                        "orderLines 必填，selectedBGA 建议传入并会写到 order_input.txt 最后一行，constraints 可选。"
                    ),
                },
            },
            "required": ["userData"],
        },
    },
    handler=lambda args, **kwargs: route_bga(
        args.get("userData", ""),
        session_id=kwargs.get("session_id"),
    ),
    check_fn=lambda: True,
)


_NET_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])(?:net|NET)[A-Za-z0-9_.+\-/]*")


def extract_reroute_nets(user_text: str) -> list[str]:
    """Extract net names from a natural-language reroute request."""
    text = str(user_text or "")
    found: list[str] = []

    for match in _NET_TOKEN_RE.finditer(text):
        found.append(match.group(0))

    for quoted in re.findall(r"[`'\"“”‘’]([^`'\"“”‘’]{1,80})[`'\"“”‘’]", text):
        candidate = quoted.strip()
        if _NET_TOKEN_RE.fullmatch(candidate):
            found.append(candidate)

    seen: set[str] = set()
    nets: list[str] = []
    for raw in found:
        net = raw.strip().strip("，。,.!?！？:：;；、")
        if not net:
            continue
        key = net.casefold()
        if key in seen:
            continue
        seen.add(key)
        nets.append(net)
    return nets


def _json_object(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {"droppedBoardData": value}
    return {"result": value}


def _first_text_value(payload: Dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _read_board_file(path_text: str) -> tuple[str, str]:
    """Read a PCB board text file returned by the EDA mock tool."""
    if not path_text:
        return "", ""
    path = Path(path_text).expanduser()
    if not path.is_file():
        logger.warning("drop_net returned board data path that is not a file: %s", path_text)
        return "", path_text
    try:
        return path.read_text(encoding="utf-8"), str(path)
    except OSError as exc:
        logger.warning("Failed reading dropped board data file %s: %s", path_text, exc)
        return "", str(path)


def _resolve_dropped_board_data(drop_result: Dict[str, Any]) -> tuple[str, str]:
    """Resolve dropped board text from direct content or a returned file path."""
    direct = _first_text_value(
        drop_result,
        ("droppedBoardData", "boardData", "projectData"),
    )
    file_path = _first_text_value(
        drop_result,
        (
            "droppedBoardDataFilePath",
            "droppedBoardFilePath",
            "boardDataFilePath",
            "projectDataFilePath",
            "filePath",
            "path",
        ),
    )
    if direct:
        return direct, file_path
    if file_path:
        return _read_board_file(file_path)
    return "", ""


def drop_net(userText: str, projectID: str = "", session_id: Optional[str] = None) -> str:
    """
    从用户文本提取需要删除的 net，并通过 WebSocket 请求 EDA 执行 MOCK 拆线。

    EDA 侧负责真正删除走线，并返回拆线后的版图数据与被拆对象信息。
    """
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("drop_net", session_id)
        logger.warning(msg)
        return json.dumps({"selectedNets": [], "error": msg}, ensure_ascii=False)

    nets = extract_reroute_nets(userText)
    if not nets:
        return json.dumps(
            {
                "selectedNets": [],
                "error": "未从用户文本中识别到需要拆线的 net，请明确写出如 net13、net17。",
            },
            ensure_ascii=False,
        )

    try:
        logger.info("drop_net start: session=%s projectID=%s nets=%s", session_id, projectID, nets)
        result = _transport.call_tool_sync(
            tool_name="drop_net_mock",
            arguments={"projectID": projectID, "nets": nets, "userText": userText},
            timeout=60.0,
            session_id=session_id,
        )
        drop_result = _json_object(result)
        dropped_board_data, dropped_board_path = _resolve_dropped_board_data(drop_result)
        payload = {
            "selectedNets": nets,
            "dropResult": drop_result,
            "droppedBoardData": dropped_board_data,
            "droppedBoardDataFilePath": dropped_board_path,
            "droppedObjects": drop_result.get("droppedObjects") or drop_result.get("removedObjects") or [],
            "localContext": drop_result.get("localContext") or {},
        }
        _transport.cache_reroute_context(payload, session_id=session_id)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        logger.error("drop_net failed: %s", e)
        return json.dumps({"selectedNets": nets, "error": str(e)}, ensure_ascii=False)


registry.register(
    name="drop_net",
    toolset="pcb",
    schema={
        "name": "drop_net",
        "description": (
            "从用户文本中提取要拆除的 net，并请求 EDA 客户端执行 MOCK 局部拆线。"
            "不要调用 GetSelectedElements；本工具只依赖用户文本中的 net 名称。"
            "客户端可返回 droppedBoardData 或 droppedBoardDataFilePath；若返回文件路径，本工具会读取文件内容并缓存。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "userText": {
                    "type": "string",
                    "description": "用户原始拆线重布请求，例如：请帮我把 BGA U2 的 net13、net17 拆线后重新布线",
                },
                "projectID": {
                    "type": "string",
                    "description": "PCB 项目的 UUID，可从用户消息的 projectid 字段传入；缺省也可由前端使用当前工程。",
                },
            },
            "required": ["userText"],
        },
    },
    handler=lambda args, **kwargs: drop_net(
        args.get("userText", ""),
        projectID=args.get("projectID", ""),
        session_id=kwargs.get("session_id"),
    ),
    check_fn=lambda: _transport.get_adapter() is not None,
)


def _build_fallback_reroute_payload(
    *,
    nets: list[str],
    dropped_board_data: str,
    dropped_board_path: str,
    dropped_objects: Any,
    local_context: Any,
    constraints: Any,
    check_report: Dict[str, Any],
    explanation_suffix: str = "",
) -> Dict[str, Any]:
    reroute_result = {
        "type": "local_reroute",
        "mode": "selected_nets_after_drop",
        "selectedNets": nets,
        "operations": [
            {
                "action": "reroute_net",
                "net": net,
                "scope": "local",
                "preserveOtherNets": True,
            }
            for net in nets
        ],
        "constraints": constraints,
        "droppedObjects": dropped_objects,
        "localContext": local_context,
        "droppedBoardDataFilePath": dropped_board_path,
        "droppedBoardDataChars": len(dropped_board_data or ""),
    }
    explanation = (
        "已基于用户文本中的 net 名称和 EDA 拆线结果生成局部重布结果包；"
        "本结果限定在 selectedNets 范围内，其他网络默认保护。"
    )
    if explanation_suffix:
        explanation = f"{explanation}{explanation_suffix}"
    return {
        "rerouteResult": reroute_result,
        "checkReport": check_report,
        "explanation": explanation,
    }


def _normalize_reroute_model_payload(
    model_payload: Dict[str, Any],
    *,
    fallback_payload: Dict[str, Any],
    context_stats: Dict[str, Any] | None,
) -> Dict[str, Any]:
    result = dict(fallback_payload)
    if isinstance(model_payload.get("rerouteResult"), dict):
        merged_result = dict(result["rerouteResult"])
        merged_result.update(model_payload["rerouteResult"])
        if context_stats:
            merged_result.setdefault("contextStats", context_stats)
        result["rerouteResult"] = merged_result
    if isinstance(model_payload.get("checkReport"), dict):
        result["checkReport"] = model_payload["checkReport"]
    if isinstance(model_payload.get("explanation"), str) and model_payload["explanation"].strip():
        result["explanation"] = model_payload["explanation"].strip()
    return result


def _generate_reroute_with_model(
    *,
    nets: list[str],
    dropped_board_data: str,
    dropped_board_path: str,
    dropped_objects: Any,
    local_context: Any,
    constraints: Any,
    check_report: Dict[str, Any],
) -> Dict[str, Any]:
    """Use the PCB chunking service and configured LLM to generate reroute output."""
    fallback_payload = _build_fallback_reroute_payload(
        nets=nets,
        dropped_board_data=dropped_board_data,
        dropped_board_path=dropped_board_path,
        dropped_objects=dropped_objects,
        local_context=local_context,
        constraints=constraints,
        check_report=check_report,
    )
    if not dropped_board_data:
        return fallback_payload

    try:
        from tools import pcb_chunking_tool as chunking

        runtime = chunking._resolve_model_runtime_config()
        adapter = chunking._OpenAICompatibleChatAdapter(
            base_url=runtime["base_url"],
            model=runtime["model"],
            api_key=runtime["api_key"],
            timeout_s=300,
        )
        token_counter = adapter.get_token_counter()
        context_result = chunking._build_board_context(dropped_board_data, token_counter=token_counter)
        context_text = context_result["contextText"]
        context_stats = context_result.get("stats") or {}

        system_prompt = (
            "你是一名 PCB 局部拆线重布助手。只输出 JSON，不要输出 Markdown、解释性段落或代码块。\n"
            "目标：基于拆线后的版图分块上下文、待重布 net、被拆对象和局部上下文，生成局部重布结果包。\n"
            "不要编造不可确认的几何细节；如果无法给出真实线段，输出可执行意图级 operations。"
        )
        user_prompt = (
            "请生成如下 JSON 结构：\n"
            "{\n"
            '  "rerouteResult": {"type": "local_reroute", "mode": "selected_nets_after_drop", "selectedNets": [], "operations": []},\n'
            '  "checkReport": {"passed": true, "checks": []},\n'
            '  "explanation": "简短中文说明"\n'
            "}\n\n"
            f"selectedNets:\n{json.dumps(nets, ensure_ascii=False, indent=2)}\n\n"
            f"constraints:\n{json.dumps(constraints, ensure_ascii=False, indent=2)}\n\n"
            f"droppedObjects:\n{json.dumps(dropped_objects, ensure_ascii=False, indent=2)}\n\n"
            f"localContext:\n{json.dumps(local_context, ensure_ascii=False, indent=2)}\n\n"
            f"droppedBoardDataFilePath: {dropped_board_path or ''}\n\n"
            f"chunkStats:\n{json.dumps(context_stats, ensure_ascii=False, indent=2)}\n\n"
            f"拆线后版图分块上下文:\n{context_text}\n"
        )
        prompt_bundle = chunking._PromptBundle(system=system_prompt, user=user_prompt)
        generation_config = chunking._GenerationConfig(max_new_tokens=1600, temperature=0.1)
        raw_text, _model_meta = adapter.generate(prompt_bundle, generation_config)
        model_payload = chunking._extract_first_json_object(raw_text)
        return _normalize_reroute_model_payload(
            model_payload,
            fallback_payload=fallback_payload,
            context_stats=context_stats,
        )
    except Exception as exc:
        logger.warning("reroute model generation failed; using fallback payload: %s", exc)
        return _build_fallback_reroute_payload(
            nets=nets,
            dropped_board_data=dropped_board_data,
            dropped_board_path=dropped_board_path,
            dropped_objects=dropped_objects,
            local_context=local_context,
            constraints=constraints,
            check_report=check_report,
            explanation_suffix=f"（模型重布生成不可用，已回退到结构化结果包：{exc}）",
        )


def reroute(userData: str = "", session_id: Optional[str] = None) -> str:
    """
    基于 drop_net 缓存的拆线上下文生成精简局部重布结果包。

    第一版不调用外部全局 router，只输出 EDA 可继续落地/展示的局部重布请求。
    """
    session_id = _transport.resolve_session_id(session_id)
    if not _transport.is_pcb_mode(session_id):
        msg = _session_mode_error("reroute", session_id)
        logger.warning(msg)
        return json.dumps({"rerouteResult": None, "checkReport": {"passed": False, "errors": [msg]}}, ensure_ascii=False)

    try:
        user_data_obj = json.loads(userData) if isinstance(userData, str) and userData.strip() else {}
        if not isinstance(user_data_obj, dict):
            user_data_obj = {}
    except json.JSONDecodeError:
        return json.dumps(
            {
                "rerouteResult": None,
                "checkReport": {"passed": False, "errors": [f"无效的 userData JSON: {userData[:200]}"]},
            },
            ensure_ascii=False,
        )

    cached = _transport.get_cached_reroute_context(session_id=session_id) or {}
    nets = (
        user_data_obj.get("selectedNets")
        or user_data_obj.get("nets")
        or cached.get("selectedNets")
        or extract_reroute_nets(user_data_obj.get("userText", ""))
    )
    nets = [str(net).strip() for net in nets if str(net).strip()] if isinstance(nets, list) else []

    if not nets:
        return json.dumps(
            {
                "rerouteResult": None,
                "checkReport": {"passed": False, "errors": ["缺少 selectedNets，无法生成局部重布结果。"]},
            },
            ensure_ascii=False,
        )

    dropped_board_data = (
        user_data_obj.get("droppedBoardData")
        or cached.get("droppedBoardData")
        or _transport.get_cached_project_data(session_id=session_id)
        or ""
    )
    dropped_board_path = (
        user_data_obj.get("droppedBoardDataFilePath")
        or cached.get("droppedBoardDataFilePath")
        or ""
    )
    if not dropped_board_data and dropped_board_path:
        dropped_board_data, dropped_board_path = _read_board_file(str(dropped_board_path))
    dropped_objects = user_data_obj.get("droppedObjects") or cached.get("droppedObjects") or []
    local_context = user_data_obj.get("localContext") or cached.get("localContext") or {}
    constraints = user_data_obj.get("constraints") or {}

    check_report = {
        "passed": True,
        "checks": [
            {"name": "net_extraction", "passed": bool(nets), "detail": f"识别到 {len(nets)} 个待重布 net"},
            {"name": "dropped_board_data", "passed": bool(dropped_board_data), "detail": "已获得拆线后版图数据" if dropped_board_data else "未获得拆线后版图数据，按上下文请求生成"},
            {"name": "connectivity_scope", "passed": True, "detail": "仅对 selectedNets 生成局部重布请求，不触碰其他网络"},
        ],
    }
    if not dropped_board_data:
        check_report["passed"] = False

    payload = _generate_reroute_with_model(
        nets=nets,
        dropped_board_data=dropped_board_data,
        dropped_board_path=str(dropped_board_path or ""),
        dropped_objects=dropped_objects,
        local_context=local_context,
        constraints=constraints,
        check_report=check_report,
    )
    return json.dumps(payload, ensure_ascii=False)


registry.register(
    name="reroute",
    toolset="pcb",
    schema={
        "name": "reroute",
        "description": (
            "基于 drop_net 的拆线后上下文生成局部拆线重布结果包。"
            "用于局部重布流程，不调用全局 BGA fanout router。"
            "如存在拆线后版图文本，会复用 PCB 分块模块构造长上下文并调用配置的 LLM 生成结果；失败时回退到结构化结果包。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "userData": {
                    "type": "string",
                    "description": (
                        "可选 JSON 字符串，可包含 selectedNets、droppedBoardData、droppedObjects、"
                        "droppedBoardDataFilePath、localContext、constraints；为空时使用 drop_net 缓存。"
                    ),
                },
            },
            "required": [],
        },
    },
    handler=lambda args, **kwargs: reroute(
        args.get("userData", ""),
        session_id=kwargs.get("session_id"),
    ),
    check_fn=lambda: True,
)


logger.info("PCB tools registered: getProjectData, GetSelectedElements, route, drop_net, reroute")
