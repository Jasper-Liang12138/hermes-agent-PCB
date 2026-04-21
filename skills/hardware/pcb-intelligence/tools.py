"""PCB Intelligence Tools - BGA Fanout Routing"""
import json
import asyncio
import subprocess
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
# Tool 3: route (CLI Tool - Router.exe)
# ============================================================================

def route(projectData: str, userData: Dict[str, Any]) -> Dict[str, Any]:
    """
    执行 BGA 扇出布线算法。

    调用北科大提供的规则布线器（router.exe），完成 BGA 逃逸布线计算。

    工作流程：
    1. 写入输入文件：版图信息.txt, order_input.txt, constraint.txt
    2. 执行 router.exe
    3. 读取输出文件：routing_input.txt, data.txt
    4. 返回布线结果和报告

    Args:
        projectData: PCB 数据（S 表达式字符串）
        userData: 扇出参数，包含：
            - fanoutParams: 扇出参数对象
            - selectedBGA: 选中的 BGA 名称

    Returns:
        {
            "routingResult": "...",  # 布线结果（S 表达式）
            "report": "..."          # 布线报告
        }
    """
    # 获取环境变量配置
    router_cmd = os.getenv("ROUTER_CMD", "router.exe")
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

        # Step 2: 执行布线器
        result = subprocess.run(
            [router_cmd],
            cwd=work_dir,
            capture_output=True,
            text=True,
            timeout=300  # 5 分钟超时
        )

        if result.returncode != 0:
            return {
                "routingResult": "",
                "report": f"布线器执行失败: {result.stderr}"
            }

        # Step 3: 读取输出文件

        # routing_input.txt - 布线结果
        routing_result_file = work_dir / "routing_input.txt"
        if not routing_result_file.exists():
            return {
                "routingResult": "",
                "report": "布线器未生成结果文件 routing_input.txt"
            }
        routing_result = routing_result_file.read_text(encoding="utf-8")

        # data.txt - 布线报告
        report_file = work_dir / "data.txt"
        if report_file.exists():
            report = report_file.read_text(encoding="utf-8")
        else:
            report = "布线完成（无详细报告）"

        return {
            "routingResult": routing_result,
            "report": report
        }

    except subprocess.TimeoutExpired:
        return {
            "routingResult": "",
            "report": "布线器执行超时（超过 5 分钟）"
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
        "description": "执行 BGA 扇出布线算法，生成布线结果和报告",
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
                            "description": "扇出参数：逃逸层分配、逃逸顺序等"
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
                    "required": ["fanoutParams", "selectedBGA"]
                }
            },
            "required": ["projectData", "userData"]
        }
    }
]
