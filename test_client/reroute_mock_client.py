from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import aiohttp


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _pretty(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _send_json(label: str, payload: dict[str, Any]) -> str:
    print(f"\n[send {label}]")
    print(_pretty(payload))
    return json.dumps(payload, ensure_ascii=False)


def _recv_json(label: str, payload: dict[str, Any]) -> None:
    print(f"\n[recv {label}]")
    print(_pretty(payload))


def _user_message(session_id: str, project_id: str, content: str, board_path: str) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "projectid": project_id,
        "type": "message",
        "body": {
            "role": "user",
            "content": content,
            "boardDataFilePath": board_path,
        },
    }


def _tool_result(session_id: str, project_id: str, call_id: str, result: Any) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "projectid": project_id,
        "type": "tool-results",
        "body": {
            "role": "tool",
            "content": {
                "id": call_id,
                "result": result,
            },
        },
    }


def _default_prompt(board_path: str, nets: list[str]) -> str:
    net_text = "、".join(nets)
    return f"请帮我重布线 {net_text}，版图数据文件地址为 {board_path}"


def _mock_tool_result(tool_name: str, arguments: dict[str, Any], board_path: str) -> Any:
    if tool_name == "drop_net_mock":
        nets = arguments.get("nets") if isinstance(arguments, dict) else None
        if not isinstance(nets, list):
            nets = []
        return {
            "droppedBoardDataFilePath": board_path,
            "droppedObjects": [
                {"net": str(net), "mockRemoved": True}
                for net in nets
            ],
            "localContext": {
                "source": "reroute_mock_client",
                "boardDataFilePath": board_path,
                "note": "MOCK 客户端暂时把原版图文件作为拆线后版图返回",
            },
        }
    if tool_name == "getProjectData":
        return board_path
    if tool_name == "route":
        return {
            "routingResult": "(mock-route-result)",
            "report": "mock route finished",
        }
    return {
        "mockResult": True,
        "toolName": tool_name,
        "boardDataFilePath": board_path,
    }


async def run_client(
    *,
    host: str,
    port: int,
    session_id: str,
    project_id: str,
    board_file: Path,
    prompt: str,
    timeout_s: float,
    connect_retries: int,
    connect_retry_delay_s: float,
) -> int:
    board_path = str(board_file.resolve())
    if not board_file.is_file():
        raise FileNotFoundError(f"mock board file not found: {board_file}")

    url = f"ws://{host}:{port}"
    print(f"连接地址: {url}")
    print(f"sessionId: {session_id}")
    print(f"projectid: {project_id}")
    print(f"boardFile: {board_path}")

    timeout = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        ws = None
        last_error: Exception | None = None
        for attempt in range(1, max(1, connect_retries) + 1):
            try:
                ws = await session.ws_connect(url, heartbeat=None, autoping=False)
                break
            except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
                last_error = exc
                print(f"[connect retry] {attempt}/{connect_retries}: {exc!r}")
                if attempt < connect_retries:
                    await asyncio.sleep(connect_retry_delay_s)
        if ws is None:
            print(f"\n[error] WebSocket 连接失败: {last_error!r}")
            return 1

        async with ws:
            first = _user_message(session_id, project_id, prompt, board_path)
            await ws.send_str(_send_json("message", first))

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout_s if timeout_s > 0 else None
            saw_tool_call = False
            saw_reroute_result = False

            while True:
                wait_timeout = None if deadline is None else max(0.1, deadline - loop.time())
                if deadline is not None and loop.time() >= deadline:
                    print("\n[timeout] 未在限定时间内等到最终 rerouteResult。")
                    return 2 if not saw_reroute_result else 0

                try:
                    raw = await asyncio.wait_for(ws.receive(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    print("\n[timeout] 未在限定时间内等到最终 rerouteResult。")
                    return 2 if not saw_reroute_result else 0
                if raw.type == aiohttp.WSMsgType.TEXT:
                    msg = json.loads(raw.data)
                    if not isinstance(msg, dict):
                        print(f"\n[error] 收到的 WebSocket 文本不是 JSON 对象: {msg!r}")
                        return 1
                    msg_type = str(msg.get("type") or "unknown")
                    _recv_json(msg_type, msg)

                    if msg_type == "tool-calls":
                        content = msg.get("body", {}).get("content", {})
                        call_id = str(content.get("id") or "")
                        tool_name = str(content.get("name") or "")
                        arguments = content.get("arguments") or {}
                        if not isinstance(arguments, dict):
                            arguments = {}
                        result = _mock_tool_result(tool_name, arguments, board_path)
                        reply = _tool_result(session_id, project_id, call_id, result)
                        await ws.send_str(_send_json("tool-results", reply))
                        saw_tool_call = True
                        continue

                    if msg_type == "message":
                        body = msg.get("body", {})
                        if "rerouteResult" in body:
                            saw_reroute_result = True
                            print("\n[done] 收到 rerouteResult，重布线流程闭环完成。")
                            return 0
                        if "routingResult" in body:
                            print("\n[done] 收到 routingResult。")
                            return 0

                    if msg_type == "error":
                        print("\n[error] 服务端返回 error，流程终止。")
                        return 1

                elif raw.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE):
                    print("\n[closed] WebSocket 已关闭。")
                    return 1
                elif raw.type == aiohttp.WSMsgType.ERROR:
                    print(f"\n[error] WebSocket 异常: {ws.exception()!r}")
                    return 1

            return 0 if saw_tool_call else 1


def main() -> None:
    default_board = Path(__file__).resolve().with_name("mock_reroute_board.s_expr")
    default_session = f"reroute-mock-{uuid.uuid4().hex[:8]}"

    parser = argparse.ArgumentParser(description="PCB reroute WebSocket MOCK 客户端")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--session-id", default=default_session)
    parser.add_argument("--project-id", default="proj-reroute-mock")
    parser.add_argument("--board-file", default=str(default_board))
    parser.add_argument("--nets", default="net13", help="逗号分隔的 net 列表，例如 net13,net17")
    parser.add_argument("--prompt", default="", help="覆盖默认首条 prompt")
    parser.add_argument("--timeout", type=float, default=180.0, help="等待最终结果秒数；<=0 表示不超时")
    parser.add_argument("--connect-retries", type=int, default=30)
    parser.add_argument("--connect-retry-delay", type=float, default=1.0)
    args = parser.parse_args()

    board_file = Path(args.board_file).expanduser()
    nets = [item.strip() for item in str(args.nets).split(",") if item.strip()]
    prompt = args.prompt.strip() or _default_prompt(str(board_file.resolve()), nets)

    raise SystemExit(
        asyncio.run(
            run_client(
                host=args.host,
                port=args.port,
                session_id=args.session_id,
                project_id=args.project_id,
                board_file=board_file,
                prompt=prompt,
                timeout_s=args.timeout,
                connect_retries=args.connect_retries,
                connect_retry_delay_s=args.connect_retry_delay,
            )
        )
    )


if __name__ == "__main__":
    main()
