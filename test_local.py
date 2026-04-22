"""
本地自动化测试脚本 — 模拟 test-client 交互流程
测试场景：
  1. 普通聊天（不应触发 tool-calls）
  2. 完整布线流程（帮我对 U27 进行 BGA 逃逸布线）
"""
import asyncio
import json
import uuid
import aiohttp

WS_URL = "ws://127.0.0.1:8765"
PROJECT_ID = "proj-test"

MOCK_TOOL_RESULTS = {
    "getProjectData": "(pcb_data (component (name U27) (package BGA256)))",
    "route": {"routingResult": "(routes (net N1) (status ok))", "report": "mock route finished"},
    "GetSelectedElements": [{"label": "U27", "detail": "BGA256"}],
}

def build_user_msg(session_id, content):
    return {"sessionId": session_id, "projectid": PROJECT_ID,
            "type": "message", "body": {"role": "user", "content": content}}

def build_tool_result(call_id, result):
    return {"type": "tool-results", "body": {"role": "tool",
            "content": {"id": call_id, "result": result}}}


async def run_session(name: str, first_msg: str, follow_ups: list[str] = []):
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    print(f"\n{'='*60}")
    print(f"[场景] {name}")
    print(f"[首条消息] {first_msg}")
    print('='*60)

    async with aiohttp.ClientSession() as http:
        async with http.ws_connect(WS_URL) as ws:
            await ws.send_str(json.dumps(build_user_msg(session_id, first_msg), ensure_ascii=False))
            follow_idx = 0
            tool_call_count = 0

            async for raw in ws:
                if raw.type == aiohttp.WSMsgType.TEXT:
                    msg = json.loads(raw.data)
                    msg_type = msg.get("type", "")

                    if msg_type == "tool-calls":
                        tool_call_count += 1
                        content = msg["body"]["content"]
                        tool_name = content.get("name", "")
                        call_id = content.get("id", "")
                        mock = MOCK_TOOL_RESULTS.get(tool_name, f"mock:{tool_name}")
                        print(f"  [tool-call #{tool_call_count}] {tool_name}({content.get('arguments',{})}) → 回传 mock 数据")
                        await ws.send_str(json.dumps(build_tool_result(call_id, mock), ensure_ascii=False))

                    elif msg_type == "message":
                        body = msg.get("body", {})
                        is_final = body.get("isFinal")
                        content_text = body.get("content", "")
                        print(f"  [Agent {'最终' if is_final else '流式'}] {content_text[:120]}")

                        if is_final is True or is_final is None:
                            if follow_idx < len(follow_ups):
                                next_msg = follow_ups[follow_idx]
                                follow_idx += 1
                                print(f"  [发送追问] {next_msg}")
                                await ws.send_str(json.dumps(build_user_msg(session_id, next_msg), ensure_ascii=False))
                            else:
                                print(f"  [会话结束] tool-calls共{tool_call_count}次")
                                await ws.close()
                                break

                    elif msg_type == "error":
                        print(f"  [ERROR] {msg}")
                        await ws.close()
                        break

                elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break


async def main():
    # 场景1：普通聊天，不应触发 tool-calls
    await run_session(
        "普通聊天（不应触发 tool-calls）",
        "BGA 和 QFP 有什么区别？",
    )

    await asyncio.sleep(1)

    # 场景2：完整布线流程
    await run_session(
        "完整布线流程",
        "帮我对 U27 进行 BGA 逃逸布线",
        follow_ups=["选择 U27", "确认"],
    )


if __name__ == "__main__":
    asyncio.run(main())
