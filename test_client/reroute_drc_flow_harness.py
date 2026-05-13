from __future__ import annotations

import argparse
import asyncio
import json
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gateway.config import PlatformConfig
from gateway.platforms.websocket import WebSocketAdapter
from test_client.reroute_mock_client import run_client
from tools import pcb_tools


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        return int(sock.getsockname()[1])


def _append_jsonl(path: Path, event: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **event,
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _fake_generate_factory(log_file: Path):
    original_generate = pcb_tools._generate_reroute_with_model
    crossing_patch = (
        "(segment (start 100 100) (end 110 100) (width 0.2) (layer F.Cu) (net 13))\n"
        "(segment (start 105 95) (end 105 105) (width 0.2) (layer F.Cu) (net 17))"
    )
    valid_patch = '(segment (start 100 100) (end 110 100) (width 0.2) (layer "F.Cu") (net 13))'

    def _fake_generate(**kwargs):
        feedback = list(kwargs.get("drc_feedback") or [])
        iteration_history = list(kwargs.get("drc_iteration_history") or [])
        patch = valid_patch if feedback else crossing_patch
        try:
            from tools import pcb_chunking_tool as chunking

            context_result = chunking._build_board_context(kwargs.get("dropped_board_data") or "", token_counter=None)
            prompts = pcb_tools._build_reroute_generation_prompts(
                nets=kwargs.get("nets") or [],
                dropped_board_path=kwargs.get("dropped_board_path") or "",
                dropped_objects=kwargs.get("dropped_objects") or [],
                local_context=kwargs.get("local_context") or {},
                constraints=kwargs.get("constraints") or {},
                original_board_path=kwargs.get("original_board_path") or "",
                context_text=context_result.get("contextText") or "",
                context_stats=context_result.get("stats") or {},
                drc_feedback=feedback,
                drc_iteration_history=iteration_history,
                selected_trace_ids=kwargs.get("selected_trace_ids") or [],
            )
            _append_jsonl(
                log_file,
                {
                    "side": "server",
                    "direction": "internal",
                    "label": "reroute_generation_prompt",
                    "payload": {
                        "iteration": 2 if feedback else 1,
                        "feedback": feedback,
                        "iterationHistory": iteration_history,
                        "systemPrompt": prompts["system"],
                        "userPrompt": prompts["user"],
                    },
                },
            )
        except Exception as exc:
            _append_jsonl(
                log_file,
                {
                    "side": "server",
                    "direction": "internal",
                    "label": "reroute_generation_prompt_error",
                    "payload": {"error": str(exc)},
                },
            )
        payload = pcb_tools._build_fallback_reroute_payload(
            **{
                key: value
                for key, value in kwargs.items()
                if key not in {"drc_feedback", "drc_iteration_history"}
            }
        )
        payload["kicadPatch"] = patch
        payload["explanation"] = (
            "MOCK 模型第 2 轮根据 DRC 反馈生成可回填 KiCad patch。"
            if feedback
            else "MOCK 模型第 1 轮生成会触发 DRC 交叉错误的 KiCad patch。"
        )
        _append_jsonl(
            log_file,
            {
                "side": "server",
                "direction": "internal",
                "label": "mock_model_generate",
                "payload": {
                    "feedback": feedback,
                    "iterationHistory": iteration_history,
                    "patch": patch,
                },
            },
        )
        return payload

    return original_generate, _fake_generate


async def _run_flow(args: argparse.Namespace) -> int:
    port = args.port or _free_port()
    log_file = Path(args.log_file).expanduser().resolve()
    if log_file.exists() and not args.append_log:
        log_file.unlink()

    adapter = WebSocketAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": args.host,
                "port": port,
                "route_intent_llm_enabled": False,
            },
        )
    )
    original_generate, fake_generate = _fake_generate_factory(log_file)
    pcb_tools._generate_reroute_with_model = fake_generate

    async def handler(event):
        session_id = event.source.chat_id
        pcb_tools._transport.set_session_mode(session_id, "pcb")
        _append_jsonl(
            log_file,
            {
                "side": "server",
                "direction": "recv",
                "label": "user_message",
                "payload": {
                    "sessionId": session_id,
                    "text": event.text,
                },
            },
        )

        drop_result = await asyncio.to_thread(
            pcb_tools.drop_net,
            event.text,
            args.project_id,
            session_id,
        )
        _append_jsonl(
            log_file,
            {
                "side": "server",
                "direction": "internal",
                "label": "drop_net_result",
                "payload": json.loads(drop_result),
            },
        )

        reroute_result = await asyncio.to_thread(
            pcb_tools.reroute,
            json.dumps({"maxDrcIterations": args.max_drc_iterations}, ensure_ascii=False),
            session_id,
        )
        reroute_payload = json.loads(reroute_result)
        attempts = ((reroute_payload.get("rerouteResult") or {}).get("drcAttempts") or [])
        _append_jsonl(
            log_file,
            {
                "side": "server",
                "direction": "internal",
                "label": "drc_attempts_parsed",
                "payload": [
                    {
                        "iteration": attempt.get("iteration"),
                        "passed": attempt.get("passed"),
                        "fillDetail": attempt.get("fillDetail"),
                        "rawDrcResult": attempt.get("drcResult"),
                        "parsedFailureSummary": attempt.get("failureSummary"),
                    }
                    for attempt in attempts
                ],
            },
        )
        _append_jsonl(
            log_file,
            {
                "side": "server",
                "direction": "internal",
                "label": "reroute_result",
                "payload": reroute_payload,
            },
        )

        fields = {
            "rerouteResult": reroute_payload.get("rerouteResult"),
            "routedBoardDataFilePath": reroute_payload.get("routedBoardDataFilePath"),
            "checkReport": reroute_payload.get("checkReport"),
            "explanation": reroute_payload.get("explanation"),
        }
        response = (
            "MOCK DRC 迭代重布线流程已完成。\n\n"
            "##PCB_FIELDS##\n"
            f"{json.dumps(fields, ensure_ascii=False)}\n"
            "##PCB_FIELDS_END##"
        )
        _append_jsonl(
            log_file,
            {
                "side": "server",
                "direction": "send",
                "label": "final_message_fields",
                "payload": fields,
            },
        )
        return response

    adapter.set_message_handler(handler)
    await adapter.connect()
    try:
        code = await run_client(
            host=args.host,
            port=port,
            session_id=args.session_id,
            project_id=args.project_id,
            board_file=Path(args.board_file).expanduser(),
            prompt=args.prompt,
            timeout_s=args.timeout,
            connect_retries=args.connect_retries,
            connect_retry_delay_s=args.connect_retry_delay,
            log_file=log_file,
            expect_drc_iterations=args.expect_drc_iterations,
            expect_drc_passed=True,
        )
    finally:
        pcb_tools._generate_reroute_with_model = original_generate
        await adapter.disconnect()
    return code


def main() -> None:
    default_board = Path(__file__).resolve().with_name("mock_reroute_board.kicad_pcb")
    parser = argparse.ArgumentParser(description="Run deterministic reroute DRC iteration flow over WebSocket.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--session-id", default="reroute-drc-flow-mock")
    parser.add_argument("--project-id", default="proj-reroute-drc-flow")
    parser.add_argument("--board-file", default=str(default_board))
    parser.add_argument("--prompt", default="")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--connect-retries", type=int, default=20)
    parser.add_argument("--connect-retry-delay", type=float, default=0.2)
    parser.add_argument("--max-drc-iterations", type=int, default=5)
    parser.add_argument("--expect-drc-iterations", type=int, default=2)
    parser.add_argument("--log-file", default="test_client/reroute_drc_mock_flow.jsonl")
    parser.add_argument("--append-log", action="store_true")
    args = parser.parse_args()

    board_path = str(Path(args.board_file).expanduser().resolve())
    if not args.prompt:
        args.prompt = f"请帮我重布线 net13，版图数据文件地址为 {board_path}"

    raise SystemExit(asyncio.run(_run_flow(args)))


if __name__ == "__main__":
    main()
