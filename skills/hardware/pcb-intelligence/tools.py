"""PCB Intelligence Tools - BGA Fanout Routing"""
import json
import asyncio
import os
from typing import Dict, Any, Optional
from pathlib import Path


# ============================================================================
# WebSocket Transport (for PCB tool proxy)
# ============================================================================

class WebSocketTransport:
    """Manages WebSocket communication with Qiyunfang PCB client."""

    def __init__(self):
        self._pending_calls: Dict[str, asyncio.Future] = {}
        self._websocket = None  # Will be injected by gateway

    def set_websocket(self, ws):
        """Set the active WebSocket connection."""
        self._websocket = ws

    async def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> Any:
        """
        Call a PCB tool via WebSocket and wait for result.

        Args:
            tool_name: Tool name (getProjectData, GetSelectedElements)
            arguments: Tool arguments

        Returns:
            Tool result from PCB client
        """
        if not self._websocket:
            raise RuntimeError("WebSocket not connected")

        # Generate unique call ID
        import uuid
        call_id = f"call_{uuid.uuid4().hex[:8]}"

        # Create future for this call
        future = asyncio.Future()
        self._pending_calls[call_id] = future

        # Send tool-call message
        message = {
            "type": "tool-calls",
            "body": {
                "role": "agent",
                "content": {
                    "id": call_id,
                    "name": tool_name,
                    "arguments": arguments
                }
            }
        }

        await self._websocket.send_json(message)

        # Wait for result (with timeout)
        try:
            result = await asyncio.wait_for(future, timeout=30.0)
            return result
        except asyncio.TimeoutError:
            self._pending_calls.pop(call_id, None)
            raise TimeoutError(f"Tool call {tool_name} timed out after 30s")

    def resolve_tool_call(self, call_id: str, result: Any):
        """Resolve a pending tool call with its result."""
        future = self._pending_calls.pop(call_id, None)
        if future and not future.done():
            future.set_result(result)


# Global transport instance (will be initialized by gateway)
_transport: Optional[WebSocketTransport] = None


def get_transport() -> WebSocketTransport:
    """Get the global WebSocket transport instance."""
    global _transport
    if _transport is None:
        _transport = WebSocketTransport()
    return _transport


# ============================================================================
# Tool 1: getProjectData (WebSocket Proxy)
# ============================================================================

async def getProjectData(projectID: str) -> str:
    """
    获取 PCB 项目数据（S 表达式格式）。

    通过 WebSocket 代理调用启云方 PCB 客户端的 PdslExport.ExportDbData 接口。

    Args:
        projectID: 项目 UUID

    Returns:
        PCB 数据的 S 表达式字符串
    """
    transport = get_transport()
    result = await transport.call_tool("getProjectData", {"projectID": projectID})
    return result


# ============================================================================
# Tool 2: GetSelectedElements (WebSocket Proxy)
# ============================================================================

async def GetSelectedElements(projectID: str) -> Dict[str, Any]:
    """
    获取用户在 PCB 中选中的元素 ID 列表。

    通过 WebSocket 代理调用启云方 PCB 客户端的 PdslSelect.GetSelectedElements 接口。

    Args:
        projectID: 项目 UUID

    Returns:
        {"ids": ["wire_001", "wire_002", ...]}
    """
    transport = get_transport()
    result = await transport.call_tool("GetSelectedElements", {"projectID": projectID})
    return result


# ============================================================================
# Tool 3: route (historical local tool wrapper)
# ============================================================================

def route(projectData: str, userData: Dict[str, Any]) -> Dict[str, Any]:
    """
    执行 BGA 扇出布线算法。

    当前运行时主实现位于 tools/pcb_tools.py，并通过 routerType 选择 arc 或 135 adapter。
    本文件保留为 skill 内历史工具定义参考，不作为当前 Windows 联调主入口。

    工作流程：
    1. 写入输入文件：版图信息.txt, order_input.txt, component_input.txt, constrain.txt（arc）
    2. 根据 routerType 执行 arc 或 135 Windows 布线器
    3. 读取输出文件：routing_input.txt, data.txt
    4. 返回布线结果文件路径和报告

    Args:
        projectData: PCB 数据（S 表达式字符串）
        userData: 扇出参数，包含：
            - fanoutParams: 扇出参数对象
            - selectedBGA: 选中的 BGA 名称

    Returns:
        {
            "routingResult": "...",  # 布线结果文件路径
            "report": "..."          # 布线报告
        }
    """
    # 兼容旧 skill 工具定义；当前主链路使用 tools/pcb_tools.py 的 route_bga。
    router_type = str(userData.get("routerType") or userData.get("fanoutParams", {}).get("routerType") or "").strip()
    if router_type not in {"arc", "135"}:
        return {
            "routingResult": "",
            "report": "缺少 routerType，请选择布线器：arc 或 135"
        }
    work_dir = Path(os.getenv("ROUTER_WORK_DIR", "."))

    # 确保工作目录存在
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Step 1: 写入输入文件

        # 版图信息.txt - PCB 数据
        board_data_file = work_dir / "版图信息.txt"
        board_data_file.write_text(projectData, encoding="utf-8")

        # order_input.txt - 扇出顺序和层分配
        fanout_params = userData.get("fanoutParams", {})
        order_input_file = work_dir / "order_input.txt"
        order_input_file.write_text(json.dumps(fanout_params, ensure_ascii=False), encoding="utf-8")

        # constraint.txt - 用户约束（可选）
        constraints = userData.get("constraints", {})
        if constraints:
            constraint_file = work_dir / "constraint.txt"
            constraint_file.write_text(json.dumps(constraints, ensure_ascii=False), encoding="utf-8")

        return {
            "routingResult": "",
            "report": "该 skill 内 tools.py 为历史实现；请使用 tools/pcb_tools.py 注册的 route 工具执行 arc/135 adapter。"
        }
    except Exception as e:
        return {
            "routingResult": "",
            "report": f"布线器执行异常: {str(e)}"
        }


# ============================================================================
# Hermes Agent Tool Definitions
# ============================================================================

TOOLS = [
    {
        "name": "getProjectData",
        "description": "获取 PCB 项目数据（S 表达式格式），用于分析 BGA 信息和生成布线参数",
        "parameters": {
            "type": "object",
            "properties": {
                "projectID": {
                    "type": "string",
                    "description": "PCB 项目的 UUID"
                }
            },
            "required": ["projectID"]
        }
    },
    {
        "name": "GetSelectedElements",
        "description": "获取用户在 PCB 中选中的元素 ID 列表，用于拆线重步功能",
        "parameters": {
            "type": "object",
            "properties": {
                "projectID": {
                    "type": "string",
                    "description": "PCB 项目的 UUID"
                }
            },
            "required": ["projectID"]
        }
    },
    {
        "name": "route",
        "description": "执行 BGA 扇出布线算法，生成布线结果文件路径和报告",
        "parameters": {
            "type": "object",
            "properties": {
                "projectData": {
                    "type": "string",
                    "description": "PCB 数据（S 表达式字符串），从 getProjectData 获取"
                },
                "userData": {
                    "type": "object",
                    "description": "扇出参数和用户约束",
                    "properties": {
                        "fanoutParams": {
                            "type": "object",
                            "description": "扇出参数：必须包含 routerType（arc 或 135）、逃逸层分配、逃逸顺序等"
                        },
                        "routerType": {
                            "type": "string",
                            "description": "布线器选择，只允许 arc 或 135"
                        },
                        "selectedBGA": {
                            "type": "string",
                            "description": "选中的 BGA 名称"
                        },
                        "constraints": {
                            "type": "object",
                            "description": "用户自定义约束（可选）"
                        }
                    },
                    "required": ["fanoutParams", "selectedBGA", "routerType"]
                }
            },
            "required": ["projectData", "userData"]
        }
    }
]
