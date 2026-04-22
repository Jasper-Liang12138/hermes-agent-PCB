from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from dataclasses import dataclass
from typing import Any

import aiohttp


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


@dataclass
class PendingToolCall:
    call_id: str
    tool_name: str
    arguments: Any


class ClientState:
    def __init__(self) -> None:
        self.pending_tool: PendingToolCall | None = None
        self.stop_requested = False
        self.connected = False


def _print_prompt_hint(state: ClientState) -> None:
    if state.pending_tool:
        print(
            "\n工具结果 > 直接回车使用默认值；输入 json:{...} 使用 JSON；"
            "输入 skip 跳过本次工具回包。"
        )
    else:
        print("\n你 > 输入消息并回车；/quit 退出；/help 查看命令。")


def _set_pending_tool_call(state: ClientState, msg: dict[str, Any]) -> None:
    content = msg.get("body", {}).get("content", {})
    call_id = content.get("id", "")
    tool_name = content.get("name", "")
    arguments = content.get("arguments", {})

    print("\n[recv tool-calls]")
    print(_pretty(msg))
    print(f"\n工具名: {tool_name}")
    print(f"参数: {_pretty(arguments)}")
    state.pending_tool = PendingToolCall(call_id=call_id, tool_name=tool_name, arguments=arguments)
    _print_prompt_hint(state)


async def _send_tool_result(
    ws: aiohttp.ClientWebSocketResponse,
    state: ClientState,
    raw: str,
) -> None:
    pending = state.pending_tool
    if not pending:
        return

    if raw.strip().lower() == "skip":
        print("已跳过当前 tool-results 回包。")
        state.pending_tool = None
        return

    if not raw.strip():
        result = _default_tool_result(pending.tool_name)
    elif raw.startswith("json:"):
        result = json.loads(raw[len("json:"):].strip())
    else:
        result = raw

    reply = _build_tool_result(pending.call_id, result)
    await ws.send_str(json.dumps(reply, ensure_ascii=False))
    state.pending_tool = None
    print("\n[send tool-results]")
    print(_pretty(reply))
    _print_prompt_hint(state)


async def _recv_loop(
    ws: aiohttp.ClientWebSocketResponse,
    session_id: str,
    project_id: str,
    state: ClientState,
) -> None:
    async for raw_msg in ws:
        if raw_msg.type == aiohttp.WSMsgType.TEXT:
            try:
                msg = json.loads(raw_msg.data)
            except json.JSONDecodeError:
                print("\n[recv raw]")
                print(raw_msg.data)
                continue

            msg_type = msg.get("type", "")
            if msg_type == "tool-calls":
                _set_pending_tool_call(state, msg)
                continue

            print(f"\n[recv {msg_type or 'unknown'}]")
            print(_pretty(msg))
            _print_prompt_hint(state)

        elif raw_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
            print("\n[连接已关闭] 请确认 start-gateway.bat 窗口仍在运行。")
            break
    print("\n[连接已断开] 客户端会自动重连，并继续等待服务端回复。")


async def _stdin_loop(state: ClientState, queue: asyncio.Queue[str]) -> None:
    _print_prompt_hint(state)
    while not state.stop_requested:
        try:
            raw_user = await _ainput("> ")
        except (EOFError, KeyboardInterrupt):
            print("\n正在退出...")
            state.stop_requested = True
            await queue.put("/quit")
            return
        await queue.put(raw_user)


async def _send_loop(
    ws: aiohttp.ClientWebSocketResponse,
    session_id: str,
    project_id: str,
    state: ClientState,
    queue: asyncio.Queue[str],
) -> None:
    while not ws.closed and not state.stop_requested:
        raw_user = await queue.get()

        line = raw_user.strip()
        if line.lower() in {"/quit", "/exit", "quit", "exit"}:
            print("正在退出...")
            state.stop_requested = True
            await ws.close()
            return

        if line.lower() == "/help":
            print(
                "\n命令:\n"
                "  /quit  退出客户端\n"
                "  /help  查看帮助\n"
                "普通对话直接输入一句话即可。收到 tool-calls 后，下一次输入会作为 tool-results.result。"
            )
            _print_prompt_hint(state)
            continue

        if state.pending_tool:
            await _send_tool_result(ws, state, raw_user)
            continue

        if not line:
            continue

        outgoing = _build_user_message(session_id, project_id, raw_user)
        await ws.send_str(json.dumps(outgoing, ensure_ascii=False))
        print("\n[send message]")
        print(_pretty(outgoing))
        print("\n已发送，正在等待服务端回复。你也可以继续输入下一条消息。")


async def _send_resume(
    ws: aiohttp.ClientWebSocketResponse,
    session_id: str,
    project_id: str,
) -> None:
    await ws.send_str(json.dumps({
        "sessionId": session_id,
        "projectid": project_id,
        "type": "resume",
        "body": {"role": "user", "content": ""},
    }, ensure_ascii=False))


async def _heartbeat_loop(
    ws: aiohttp.ClientWebSocketResponse,
    session_id: str,
    project_id: str,
    state: ClientState,
) -> None:
    # heartbeat disabled
    try:
        while not ws.closed and not state.stop_requested:
            await asyncio.sleep(60)
    except asyncio.CancelledError:
        pass


async def main() -> None:
    parser = argparse.ArgumentParser(description="Agent WebSocket 测试客户端")
    parser.add_argument("--host", default=DEFAULT_HOST, help="WebSocket 主机，默认 127.0.0.1")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="WebSocket 端口，默认 8765")
    parser.add_argument("--session-id", default=f"test-session-{uuid.uuid4().hex[:8]}")
    parser.add_argument("--project-id", default="proj-test")
    parser.add_argument("--content", default="", help="连接后自动发送的第一条消息；不填则等待手动输入")
    args = parser.parse_args()

    url = f"ws://{args.host}:{args.port}"
    print(f"连接地址: {url}")
    print(f"sessionId: {args.session_id}")
    print(f"projectid: {args.project_id}")

    state = ClientState()
    input_queue: asyncio.Queue[str] = asyncio.Queue()
    stdin_task = asyncio.create_task(_stdin_loop(state, input_queue))
    first_content_sent = False
    attempt = 0

    try:
        while not state.stop_requested:
            attempt += 1
            if attempt > 15:
                print("[!] 连接失败次数过多，已停止重试。")
                state.stop_requested = True
                break

            try:
                async with aiohttp.ClientSession() as session:
                    async with session.ws_connect(url) as ws:
                        attempt = 0
                        state.connected = True
                        print("\n[已连接]")
                        await _send_resume(ws, args.session_id, args.project_id)

                        if args.content.strip() and not first_content_sent:
                            first = _build_user_message(args.session_id, args.project_id, args.content)
                            await ws.send_str(json.dumps(first, ensure_ascii=False))
                            first_content_sent = True
                            print("\n[send first message]")
                            print(_pretty(first))

                        recv_task = asyncio.create_task(_recv_loop(ws, args.session_id, args.project_id, state))
                        send_task = asyncio.create_task(
                            _send_loop(ws, args.session_id, args.project_id, state, input_queue)
                        )
                        heartbeat_task = asyncio.create_task(
                            _heartbeat_loop(ws, args.session_id, args.project_id, state)
                        )
                        done, pending = await asyncio.wait(
                            {recv_task, send_task, heartbeat_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        for task in pending:
                            task.cancel()
                        for task in done:
                            task.result()

                state.connected = False
                if not state.stop_requested:
                    print("[自动重连] 2秒后重连，session 不变。")
                    await asyncio.sleep(2)
            except aiohttp.ClientConnectorError:
                state.connected = False
                if state.stop_requested:
                    break
                print(f"[!] 连接失败（{attempt}/15），2秒后重试...")
                await asyncio.sleep(2)
            except (aiohttp.ClientError, ConnectionResetError) as exc:
                state.connected = False
                if state.stop_requested:
                    break
                print(f"[自动重连] 连接异常：{exc!r}，2秒后重连。")
                await asyncio.sleep(2)
    finally:
        state.stop_requested = True
        stdin_task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
