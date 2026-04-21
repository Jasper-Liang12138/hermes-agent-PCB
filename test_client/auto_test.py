"""
自动化完整布线流程测试
全程无需手动输入，自动处理 tool-calls 回包和多轮对话。

流程：
  Round 1: 发送布线请求 → 自动回复 getProjectData mock 数据 → 收到 selection
  Round 2: 自动发送"选择 U27" → 收到 fanoutParams
  Round 3: 自动发送"确认" → 自动回复 route mock 数据 → 收到 routingResult
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from typing import Any

import websockets


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
SESSION_ID   = f"auto-test-{uuid.uuid4().hex[:8]}"
PROJECT_ID   = "proj-autotest-001"


def _pretty(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _user_msg(content: str) -> dict:
    return {
        "sessionId": SESSION_ID,
        "projectid": PROJECT_ID,
        "type": "message",
        "body": {"role": "user", "content": content},
    }


def _tool_result(call_id: str, result: Any) -> dict:
    return {
        "type": "tool-results",
        "body": {"role": "tool", "content": {"id": call_id, "result": result}},
    }


# ── mock 工具返回值 ──────────────────────────────────────────────────────────

MOCK_PROJECT_DATA = """(pcb_data (version 20221018) (project "proj-autotest-001")
  (component (name "U27") (package "BGA-256") (pins 256) (pitch "1.0mm"))
  (component (name "U35") (package "BGA-484") (pins 484) (pitch "0.8mm"))
  (stackup
    (layer (name "SIG01") (type "signal"))
    (layer (name "SIG02") (type "signal"))
    (layer (name "SIG03") (type "signal"))
    (layer (name "SIG04") (type "signal")))
  (net (name "GND") (pins "U27-A1" "U27-B1"))
  (net (name "VCC") (pins "U27-A2" "U27-B2"))
  (net (name "DDR_D0") (pins "U27-C1" "U35-A1")))"""

MOCK_ROUTE_RESULT = {
    "routingResult": (
        "(routes (route (net \"GND\") (layer \"SIG03\")"
        " (path (line (start 4008.9 13888.9) (end 3999.18 13883.3) (width 3)))))"
    ),
    "report": "布线连通率: 100%\n总线长: 1234.5 mil\n通孔数量: 42",
}

TOOL_MOCK: dict[str, Any] = {
    "getProjectData":     MOCK_PROJECT_DATA,
    "GetSelectedElements": {"ids": []},
    "route":              MOCK_ROUTE_RESULT,
}


# ── 状态机 ───────────────────────────────────────────────────────────────────

class TestState:
    """跟踪测试进度，决定下一步自动操作。"""

    def __init__(self):
        self.got_selection     = False
        self.got_fanout_params = False
        self.got_routing_result = False
        self.round_count       = 0
        self.final_msg_ids: set[str] = set()


async def run_test(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    url = f"ws://{host}:{port}"
    print(f"\n{'='*60}")
    print(f"  自动化布线流程测试")
    print(f"  sessionId : {SESSION_ID}")
    print(f"  projectid : {PROJECT_ID}")
    print(f"  服务地址  : {url}")
    print(f"{'='*60}\n")

    state = TestState()

    try:
        async with websockets.connect(url, open_timeout=10) as ws:
            # ── Round 1：发送布线请求 ──────────────────────────────────────
            first = _user_msg("帮我进行BGA逃逸布线")
            await ws.send(json.dumps(first, ensure_ascii=False))
            print("[→ SEND] 帮我进行BGA逃逸布线\n")

            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    print(f"[RAW] {raw}\n")
                    continue

                msg_type = msg.get("type", "unknown")
                body     = msg.get("body", {})

                if msg_type == "tool-calls":
                    # ── 自动回复工具调用 ─────────────────────────────────
                    content   = body.get("content", {})
                    call_id   = content.get("id", "")
                    tool_name = content.get("name", "")
                    args      = content.get("arguments", {})

                    print(f"[← TOOL-CALLS] {tool_name}")
                    print(f"  args: {json.dumps(args, ensure_ascii=False)}")

                    result = TOOL_MOCK.get(tool_name, f"mock:{tool_name}")
                    reply  = _tool_result(call_id, result)
                    await ws.send(json.dumps(reply, ensure_ascii=False))
                    print(f"[→ TOOL-RESULTS] {tool_name} → (mock data)\n")

                elif msg_type == "message":
                    role     = body.get("role", "")
                    content  = body.get("content", "")
                    is_final = body.get("isFinal")
                    msg_id   = body.get("msgId", "")

                    # 流式中间帧只打省略号
                    if is_final is False:
                        print(f"[← STREAM] {content[:60]}{'...' if len(content)>60 else ''}")
                        continue

                    # 完整帧
                    print(f"\n[← MESSAGE] role={role}  isFinal={is_final}")
                    if content:
                        print(f"  content: {content[:300]}{'...' if len(content)>300 else ''}")

                    # 打印结构化字段
                    for field in ("selection", "fanoutParams", "routingResult", "thinking"):
                        if field in body:
                            val = body[field]
                            val_str = json.dumps(val, ensure_ascii=False) if not isinstance(val, str) else val
                            print(f"  {field}: {val_str[:200]}{'...' if len(val_str)>200 else ''}")

                    print()

                    # ── 根据结构化字段决定下一步 ──────────────────────────
                    if "selection" in body and not state.got_selection:
                        state.got_selection = True
                        next_msg = _user_msg("选择 U27")
                        await ws.send(json.dumps(next_msg, ensure_ascii=False))
                        print("[→ SEND] 选择 U27\n")

                    elif "fanoutParams" in body and not state.got_fanout_params:
                        state.got_fanout_params = True
                        next_msg = _user_msg("确认")
                        await ws.send(json.dumps(next_msg, ensure_ascii=False))
                        print("[→ SEND] 确认\n")

                    elif "routingResult" in body and not state.got_routing_result:
                        state.got_routing_result = True
                        print("\n" + "="*60)
                        print("  ✅ 布线流程完成！")
                        print("="*60)
                        return

                    # 收到没有结构化字段的普通消息（等待下一轮）
                    state.round_count += 1
                    if state.round_count > 20:
                        print("[WARN] 超过 20 轮未完成，退出")
                        return

                elif msg_type == "error":
                    print(f"\n[← ERROR] code={body.get('code')} msg={body.get('message')}")
                    print(f"  details: {body.get('details', '')}")
                    print("\n测试因错误终止。")
                    return

    except ConnectionRefusedError:
        print(f"[ERROR] 无法连接到 {url}，请确认服务端已启动（python gateway/run.py）")
        sys.exit(1)
    except Exception as e:
        print(f"[ERROR] {type(e).__name__}: {e}")
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    asyncio.run(run_test(args.host, args.port))
