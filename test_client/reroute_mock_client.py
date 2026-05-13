from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def _append_jsonl(path: Path | None, event: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _pretty(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2)


def _send_json(label: str, payload: dict[str, Any], log_file: Path | None = None) -> str:
    print(f"\n[send {label}]")
    print(_pretty(payload))
    _append_jsonl(log_file, {"side": "client", "direction": "send", "label": label, "payload": payload})
    return json.dumps(payload, ensure_ascii=False)


def _recv_json(label: str, payload: dict[str, Any], log_file: Path | None = None) -> None:
    print(f"\n[recv {label}]")
    print(_pretty(payload))
    _append_jsonl(log_file, {"side": "client", "direction": "recv", "label": label, "payload": payload})


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


def _default_prompt(board_path: str, selected_trace_ids: list[str]) -> str:
    trace_text = ", ".join(selected_trace_ids)
    return f"Please reroute the selected PCB traces ({trace_text}); board file path: {board_path}"


def _mock_tool_result(tool_name: str, arguments: dict[str, Any], board_path: str, selected_trace_ids: list[str]) -> Any:
    if tool_name in {"getSelectedElements", "GetSelectedElements"}:
        if arguments.get("PFindType") != "TRACES":
            return {"ids": [], "error": f"unexpected PFindType: {arguments.get('PFindType')!r}"}
        return {"ids": selected_trace_ids}
    if tool_name == "deleteTracesById":
        ids = arguments.get("ids") if isinstance(arguments, dict) else None
        if ids == selected_trace_ids:
            return "已成功删除"
        return {"success": False, "message": "delete ids did not match selected trace ids", "ids": ids}
    if tool_name == "getProjectData":
        return Path(board_path).read_text(encoding="utf-8")
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
    log_file: Path | None = None,
    expect_drc_iterations: int = 0,
    expect_drc_passed: bool | None = None,
    selected_trace_ids: list[str] | None = None,
) -> int:
    board_path = str(board_file.resolve())
    selected_trace_ids = selected_trace_ids or ["2386476278", "3424247826"]
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
            await ws.send_str(_send_json("message", first, log_file))

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
                    _recv_json(msg_type, msg, log_file)

                    if msg_type == "tool-calls":
                        content = msg.get("body", {}).get("content", {})
                        call_id = str(content.get("id") or "")
                        tool_name = str(content.get("name") or "")
                        arguments = content.get("arguments") or {}
                        if not isinstance(arguments, dict):
                            arguments = {}
                        result = _mock_tool_result(tool_name, arguments, board_path, selected_trace_ids)
                        reply = _tool_result(session_id, project_id, call_id, result)
                        await ws.send_str(_send_json("tool-results", reply, log_file))
                        saw_tool_call = True
                        continue

                    if msg_type == "message":
                        body = msg.get("body", {})
                        if "rerouteResult" in body:
                            saw_reroute_result = True
                            reroute_result = body.get("rerouteResult") or {}
                            drc_iterations = int(reroute_result.get("drcIterations") or 0)
                            drc_passed = reroute_result.get("drcPassed")
                            if expect_drc_iterations and drc_iterations != expect_drc_iterations:
                                print(
                                    f"\n[error] DRC 迭代次数不符合预期: "
                                    f"expected={expect_drc_iterations}, actual={drc_iterations}"
                                )
                                return 3
                            if expect_drc_passed is not None and bool(drc_passed) is not expect_drc_passed:
                                print(
                                    f"\n[error] DRC 通过状态不符合预期: "
                                    f"expected={expect_drc_passed}, actual={drc_passed}"
                                )
                                return 3
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
    default_board = Path(__file__).resolve().with_name("mock_reroute_board.kicad_pcb")
    default_session = f"reroute-mock-{uuid.uuid4().hex[:8]}"

    parser = argparse.ArgumentParser(description="PCB reroute WebSocket MOCK 客户端")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--session-id", default=default_session)
    parser.add_argument("--project-id", default="proj-reroute-mock")
    parser.add_argument("--board-file", default=str(default_board))
    parser.add_argument("--selected-trace-ids", default="2386476278,3424247826", help="Comma-separated selected trace ids returned by getSelectedElements")
    parser.add_argument("--prompt", default="", help="覆盖默认首条 prompt")
    parser.add_argument("--timeout", type=float, default=180.0, help="等待最终结果秒数；<=0 表示不超时")
    parser.add_argument("--connect-retries", type=int, default=30)
    parser.add_argument("--connect-retry-delay", type=float, default=1.0)
    parser.add_argument("--log-file", default="", help="将客户端收发 JSON 追加写入 JSONL 文件")
    parser.add_argument("--expect-drc-iterations", type=int, default=0)
    parser.add_argument("--expect-drc-passed", choices=("true", "false", "any"), default="any")
    args = parser.parse_args()

    board_file = Path(args.board_file).expanduser()
    selected_trace_ids = [item.strip() for item in str(args.selected_trace_ids).split(",") if item.strip()]
    prompt = args.prompt.strip() or _default_prompt(str(board_file.resolve()), selected_trace_ids)
    log_file = Path(args.log_file).expanduser() if args.log_file.strip() else None
    expect_drc_passed = None if args.expect_drc_passed == "any" else args.expect_drc_passed == "true"

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
                log_file=log_file,
                expect_drc_iterations=args.expect_drc_iterations,
                expect_drc_passed=expect_drc_passed,
                selected_trace_ids=selected_trace_ids,
            )
        )
    )


if __name__ == "__main__":
    main()
