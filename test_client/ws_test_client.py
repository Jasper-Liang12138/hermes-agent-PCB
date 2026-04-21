from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from typing import Any

import websockets


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _pretty(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _build_user_message(session_id: str, project_id: str, content: str) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "projectid": project_id,
        "type": "message",
        "body": {
            "role": "user",
            "content": content,
        },
    }


def _build_tool_result(call_id: str, result: Any) -> dict[str, Any]:
    return {
        "type": "tool-results",
        "body": {
            "role": "tool",
            "content": {
                "id": call_id,
                "result": result,
            },
        },
    }


def _default_tool_result(tool_name: str) -> Any:
    if tool_name == "getProjectData":
        return "(pcb_data (component (name U27) (package BGA256)))"
    if tool_name == "route":
        return {
            "routingResult": "(routes (net N1) (status ok))",
            "report": "mock route finished",
        }
    if tool_name == "GetSelectedElements":
        return [{"label": "U27", "detail": "BGA256"}]
    return f"mock result for {tool_name}"


async def _ainput(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def _handle_tool_call(ws: websockets.WebSocketClientProtocol, msg: dict[str, Any]) -> None:
    content = msg.get("body", {}).get("content", {})
    call_id = content.get("id", "")
    tool_name = content.get("name", "")
    arguments = content.get("arguments", {})

    print("\n[recv tool-calls]")
    print(_pretty(msg))
    print(f"\n工具名: {tool_name}")
    print(f"参数: {_pretty(arguments)}")

    default_result = _default_tool_result(tool_name)
    raw = await _ainput(
        "\n请输入 tool-results.result。"
        "直接回车使用默认值；输入 `json:` 前缀可按 JSON 解析；输入 `skip` 跳过此次回包。\n> "
    )

    if raw.strip().lower() == "skip":
        print("已跳过当前 tool-results 回包。")
        return

    if not raw.strip():
        result = default_result
    elif raw.startswith("json:"):
        result = json.loads(raw[len("json:") :].strip())
    else:
        result = raw

    reply = _build_tool_result(call_id, result)
    await ws.send(json.dumps(reply, ensure_ascii=False))
    print("\n[send tool-results]")
    print(_pretty(reply))


async def _recv_loop(
    ws: websockets.WebSocketClientProtocol,
    session_id: str,
    project_id: str,
) -> None:
    async for raw in ws:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            print("\n[recv raw]")
            print(raw)
            continue

        msg_type = msg.get("type", "")
        if msg_type == "tool-calls":
            await _handle_tool_call(ws, msg)
            continue

        print(f"\n[recv {msg_type or 'unknown'}]")
        print(_pretty(msg))

        if msg_type in {"message", "error"}:
            raw_user = await _ainput(
                "\n如需继续发送用户消息，请输入内容；直接回车则继续等待服务端消息。\n> "
            )
            if raw_user.strip():
                outgoing = _build_user_message(session_id, project_id, raw_user)
                await ws.send(json.dumps(outgoing, ensure_ascii=False))
                print("\n[send message]")
                print(_pretty(outgoing))


async def main() -> None:
    parser = argparse.ArgumentParser(description="Agent WebSocket 测试客户端")
    parser.add_argument("--host", default=DEFAULT_HOST, help="WebSocket 主机，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="WebSocket 端口，默认 8765")
    parser.add_argument("--session-id", default=f"test-session-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--project-id", default="proj-test")
    parser.add_argument("--content", default="请帮我进行BGA逃逸")
    args = parser.parse_args()

    url = f"ws://{args.host}:{args.port}"
    print(f"连接地址: {url}")
    print(f"sessionId: {args.session_id}")
    print(f"projectid: {args.project_id}")

    async with websockets.connect(url) as ws:
        first = _build_user_message(args.session_id, args.project_id, args.content)
        await ws.send(json.dumps(first, ensure_ascii=False))
        print("\n[send first message]")
        print(_pretty(first))
        await _recv_loop(ws, args.session_id, args.project_id)


if __name__ == "__main__":
    asyncio.run(main())
