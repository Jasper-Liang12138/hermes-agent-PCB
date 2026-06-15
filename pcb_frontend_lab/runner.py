"""Black-box virtual PCB frontend runner.

This tool connects to an already-running PCB Agent WebSocket service, sends
frontend-style user messages, answers tool-calls from JSONL fixtures, records
the full transcript, and evaluates only protocol-observable assertions.

It intentionally imports no Hermes Agent modules.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import aiohttp
except ImportError:  # pragma: no cover - depends on local environment
    aiohttp = None


INTERIM_STATUS_CONTENTS = {
    "已收到，正在处理...",
    "已收到，进入拆线重布 skill，正在处理...",
    "已收到，进入PCB 智能布线 skill，正在处理...",
    "正在导入版图，请稍候...",
}


@dataclass
class LabConfig:
    ws_url: str
    cases_path: Path
    out_path: Path
    timeout: float = 120.0
    session_prefix: str = "lab"
    stop_on_fail: bool = False
    verbose_frames: bool = False


@dataclass
class CaseRuntime:
    case: dict[str, Any]
    case_id: str
    session_id: str
    project_id: str
    tool_result_cursors: dict[str, int] = field(default_factory=dict)
    frames: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8-sig") as fh:
        for line_no, line in enumerate(fh, 1):
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc
            if not isinstance(data, dict):
                raise ValueError(f"{path}:{line_no}: each case must be a JSON object")
            if not data.get("id"):
                data["id"] = f"case_{line_no}"
            cases.append(data)
    return cases


def safe_file_stem(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_", "."} else "_" for ch in value)
    return cleaned.strip("._") or "case"


def make_user_message(session_id: str, project_id: str, content: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    body: dict[str, Any] = {"role": "user", "content": content}
    if options:
        body["options"] = options
    return {
        "sessionId": session_id,
        "projectid": project_id,
        "type": "message",
        "body": body,
    }


def make_tool_result(call_id: str, result: Any, session_id: str = "", project_id: str = "") -> dict[str, Any]:
    payload: dict[str, Any] = {
        "type": "tool-results",
        "body": {
            "role": "tool",
            "content": {
                "id": call_id,
                "result": result,
            },
        },
    }
    if session_id:
        payload["sessionId"] = session_id
    if project_id:
        payload["projectid"] = project_id
    return payload


def frame_summary(frame: dict[str, Any]) -> dict[str, Any]:
    body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
    content = body.get("content") if isinstance(body.get("content"), dict) else {}
    summary: dict[str, Any] = {
        "type": frame.get("type"),
        "sessionId": frame.get("sessionId"),
        "projectid": frame.get("projectid"),
    }
    if frame.get("type") == "message":
        summary["isFinal"] = body.get("isFinal")
        summary["body_keys"] = sorted(body.keys())
        text = str(body.get("content") or "")
        summary["content_preview"] = text[:160]
    elif frame.get("type") == "tool-calls":
        summary["tool"] = content.get("name")
        summary["call_id"] = content.get("id")
        summary["arguments"] = content.get("arguments")
    elif frame.get("type") == "error":
        summary["code"] = body.get("code")
        summary["message"] = body.get("message")
        summary["details"] = body.get("details")
    return summary


def is_final_message(frame: dict[str, Any]) -> bool:
    if frame.get("type") != "message":
        return False
    body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
    return body.get("isFinal") is True or body.get("isFinal") is None


def is_interim_status(frame: dict[str, Any]) -> bool:
    if frame.get("type") != "message":
        return False
    body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
    return body.get("isFinal") is False and body.get("content") in INTERIM_STATUS_CONTENTS


def tool_result_for_call(runtime: CaseRuntime, tool_name: str, call: dict[str, Any]) -> Any:
    specs = runtime.case.get("tool_results", {})
    if not isinstance(specs, dict):
        raise AssertionError("case.tool_results must be an object")

    call_index = len(runtime.tool_calls) - 1
    call_id = str(call.get("id") or "")
    candidates = [
        f"{tool_name}#{call_index + 1}",
        call_id,
        tool_name,
        "*",
    ]

    chosen_key = next((key for key in candidates if key and key in specs), None)
    if not chosen_key:
        raise AssertionError(f"no fixture tool_result for tool '{tool_name}' call id '{call_id}'")

    spec = specs[chosen_key]
    if isinstance(spec, list):
        cursor = runtime.tool_result_cursors.get(chosen_key, 0)
        if cursor >= len(spec):
            raise AssertionError(f"fixture list exhausted for tool_result key '{chosen_key}'")
        runtime.tool_result_cursors[chosen_key] = cursor + 1
        spec = spec[cursor]

    if isinstance(spec, dict) and set(spec).issubset({"result", "delay_seconds"}):
        return spec.get("result")
    return spec


async def send_json(ws: Any, payload: dict[str, Any], runtime: CaseRuntime, direction: str, verbose: bool) -> None:
    record = {
        "direction": direction,
        "time": time.time(),
        "frame": payload,
        "summary": frame_summary(payload),
    }
    runtime.frames.append(record)
    if verbose:
        print(json.dumps(record["summary"], ensure_ascii=False), flush=True)
    await ws.send_str(json.dumps(payload, ensure_ascii=False))


async def receive_json(ws: Any, runtime: CaseRuntime, timeout: float, verbose: bool) -> dict[str, Any]:
    msg = await asyncio.wait_for(ws.receive(), timeout=timeout)
    if msg.type == aiohttp.WSMsgType.TEXT:
        try:
            payload = json.loads(msg.data)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"received non-JSON text frame: {msg.data[:200]!r}") from exc
        if not isinstance(payload, dict):
            raise AssertionError(f"received JSON frame is not object: {payload!r}")
        record = {
            "direction": "agent_to_frontend",
            "time": time.time(),
            "frame": payload,
            "summary": frame_summary(payload),
        }
        runtime.frames.append(record)
        if verbose:
            print(json.dumps(record["summary"], ensure_ascii=False), flush=True)
        return payload
    if msg.type in {aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR}:
        raise ConnectionError(f"websocket closed while waiting for Agent frame: {msg.type}")
    raise AssertionError(f"unexpected websocket message type: {msg.type}")


async def drain_turn(ws: Any, runtime: CaseRuntime, config: LabConfig) -> None:
    deadline = time.monotonic() + config.timeout
    saw_terminal_frame = False
    while time.monotonic() < deadline:
        remaining = max(0.1, deadline - time.monotonic())
        frame = await receive_json(ws, runtime, remaining, config.verbose_frames)
        frame_type = frame.get("type")

        if frame_type == "tool-calls":
            body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
            content = body.get("content") if isinstance(body.get("content"), dict) else {}
            tool_name = str(content.get("name") or "")
            call_id = str(content.get("id") or "")
            if not tool_name or not call_id:
                raise AssertionError(f"malformed tool-calls frame: {frame}")
            runtime.tool_calls.append(content)
            result = tool_result_for_call(runtime, tool_name, content)
            reply = make_tool_result(call_id, result, runtime.session_id, runtime.project_id)
            await send_json(ws, reply, runtime, "frontend_to_agent", config.verbose_frames)
            continue

        if frame_type == "error":
            runtime.errors.append(frame)
            saw_terminal_frame = True
            break

        if frame_type == "message":
            if is_interim_status(frame):
                continue
            if is_final_message(frame):
                saw_terminal_frame = True
                break
            continue

    if not saw_terminal_frame:
        raise TimeoutError(f"case {runtime.case_id} turn timed out after {config.timeout}s")


async def run_case(case: dict[str, Any], config: LabConfig) -> dict[str, Any]:
    case_id = str(case.get("id") or f"case_{uuid.uuid4().hex[:8]}")
    session_id = str(case.get("sessionId") or f"{config.session_prefix}-{case_id}-{uuid.uuid4().hex[:8]}")
    project_id = str(case.get("projectid") or case.get("projectID") or case.get("projectId") or "pcb-lab-project")
    runtime = CaseRuntime(case=case, case_id=case_id, session_id=session_id, project_id=project_id)

    result: dict[str, Any] = {
        "id": case_id,
        "sessionId": session_id,
        "projectid": project_id,
        "passed": False,
        "failures": [],
        "actual": {},
        "transcript": [],
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(config.ws_url, heartbeat=None, autoping=False) as ws:
                turns = case.get("turns")
                if not isinstance(turns, list) or not turns:
                    raise AssertionError("case.turns must be a non-empty list")
                for turn in turns:
                    if isinstance(turn, dict):
                        content = str(turn.get("content") or "")
                        options = turn.get("options") if isinstance(turn.get("options"), dict) else None
                    else:
                        content = str(turn)
                        options = None
                    if not content:
                        raise AssertionError("turn content must not be empty")
                    await send_json(
                        ws,
                        make_user_message(session_id, project_id, content, options),
                        runtime,
                        "frontend_to_agent",
                        config.verbose_frames,
                    )
                    await drain_turn(ws, runtime, config)
    except Exception as exc:
        result["failures"].append({"type": "runner_error", "message": str(exc)})

    result["transcript"] = runtime.frames
    result["actual"] = collect_actual(runtime)
    result["failures"].extend(evaluate_case(case, runtime))
    result["passed"] = not result["failures"]
    return result


def collect_actual(runtime: CaseRuntime) -> dict[str, Any]:
    agent_frames = [item["frame"] for item in runtime.frames if item["direction"] == "agent_to_frontend"]
    message_frames = [frame for frame in agent_frames if frame.get("type") == "message"]
    tool_call_frames = [frame for frame in agent_frames if frame.get("type") == "tool-calls"]
    error_frames = [frame for frame in agent_frames if frame.get("type") == "error"]

    body_fields: set[str] = set()
    message_contents: list[str] = []
    for frame in message_frames:
        body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
        body_fields.update(body.keys())
        if body.get("content") is not None:
            message_contents.append(str(body.get("content")))

    tool_names: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for frame in tool_call_frames:
        body = frame.get("body") if isinstance(frame.get("body"), dict) else {}
        content = body.get("content") if isinstance(body.get("content"), dict) else {}
        name = str(content.get("name") or "")
        if name:
            tool_names.append(name)
        tool_calls.append(content)

    return {
        "tool_calls": tool_names,
        "body_fields": sorted(body_fields),
        "message_count": len(message_frames),
        "error_count": len(error_frames),
        "last_frame": frame_summary(agent_frames[-1]) if agent_frames else None,
        "last_message": message_contents[-1] if message_contents else "",
        "errors": [frame_summary(frame) for frame in error_frames],
        "tool_call_details": tool_calls,
    }


def evaluate_case(case: dict[str, Any], runtime: CaseRuntime) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    expect = case.get("expect", {})
    if not isinstance(expect, dict):
        return [{"type": "case_error", "message": "case.expect must be an object"}]

    actual = collect_actual(runtime)
    actual_tools = actual["tool_calls"]
    actual_fields = set(actual["body_fields"])
    actual_errors = actual["error_count"]

    if "tool_calls" in expect:
        expected_tools = list(expect.get("tool_calls") or [])
        if actual_tools != expected_tools:
            failures.append({
                "type": "tool_calls_mismatch",
                "expected": expected_tools,
                "actual": actual_tools,
            })

    if expect.get("no_tool_calls") and actual_tools:
        failures.append({
            "type": "unexpected_tool_call",
            "expected": [],
            "actual": actual_tools,
        })

    if expect.get("no_importLines") and "importLines" in actual_tools:
        failures.append({
            "type": "unexpected_importLines",
            "message": "importLines was called but expect.no_importLines is true",
            "actual": actual_tools,
        })

    for field_name in expect.get("body_fields") or []:
        if field_name not in actual_fields:
            failures.append({
                "type": "missing_body_field",
                "field": field_name,
                "actual_fields": sorted(actual_fields),
            })

    for field_name in expect.get("absent_body_fields") or []:
        if field_name in actual_fields:
            failures.append({
                "type": "unexpected_body_field",
                "field": field_name,
                "actual_fields": sorted(actual_fields),
            })

    if expect.get("error") is True and actual_errors == 0:
        failures.append({"type": "missing_error", "message": "expected at least one error frame"})
    if expect.get("error") is False and actual_errors > 0:
        failures.append({"type": "unexpected_error", "errors": actual["errors"]})
    if expect.get("no_error") and actual_errors > 0:
        failures.append({"type": "unexpected_error", "errors": actual["errors"]})

    for contains in expect.get("message_contains") or []:
        if contains not in actual.get("last_message", "") and not any(
            contains in str((item["frame"].get("body") or {}).get("content") or "")
            for item in runtime.frames
            if item["direction"] == "agent_to_frontend" and item["frame"].get("type") == "message"
        ):
            failures.append({
                "type": "message_text_missing",
                "expected_contains": contains,
                "last_message": actual.get("last_message", ""),
            })

    for spec in expect.get("tool_call_arguments") or []:
        if not isinstance(spec, dict):
            failures.append({"type": "case_error", "message": "expect.tool_call_arguments entries must be objects"})
            continue
        tool_name = spec.get("name")
        expected_args = spec.get("arguments")
        matching = [
            call for call in actual.get("tool_call_details", [])
            if call.get("name") == tool_name
        ]
        if not matching:
            failures.append({"type": "missing_tool_call", "tool": tool_name})
            continue
        if expected_args is not None and all(call.get("arguments") != expected_args for call in matching):
            failures.append({
                "type": "tool_arguments_mismatch",
                "tool": tool_name,
                "expected": expected_args,
                "actual": [call.get("arguments") for call in matching],
            })

    return failures


async def run_all(config: LabConfig) -> list[dict[str, Any]]:
    if aiohttp is None:
        raise SystemExit("aiohttp is required to run pcb_frontend_lab. Install the Hermes messaging extra or aiohttp.")

    cases = load_cases(config.cases_path)
    config.out_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_dir = config.out_path.parent / f"{config.out_path.stem}_transcripts"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    with config.out_path.open("w", encoding="utf-8") as out:
        for case in cases:
            result = await run_case(case, config)
            transcript_path = transcript_dir / f"{safe_file_stem(str(result['id']))}.json"
            transcript_path.write_text(
                json.dumps(result["transcript"], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            result["transcript_path"] = str(transcript_path)
            results.append(result)
            out.write(json.dumps(result, ensure_ascii=False) + "\n")
            out.flush()
            status = "PASS" if result["passed"] else "FAIL"
            failures = ", ".join(item.get("type", "failure") for item in result["failures"])
            print(f"[{status}] {result['id']}" + (f" - {failures}" if failures else ""))
            if config.stop_on_fail and not result["passed"]:
                break
    return results


def print_summary(results: list[dict[str, Any]], out_path: Path) -> None:
    total = len(results)
    passed = sum(1 for item in results if item.get("passed"))
    failed = total - passed
    by_type: dict[str, int] = {}
    for item in results:
        for failure in item.get("failures", []):
            by_type[failure.get("type", "failure")] = by_type.get(failure.get("type", "failure"), 0) + 1

    print()
    print(f"PCB frontend lab: {passed}/{total} passed, {failed} failed")
    if by_type:
        print("Failure types:")
        for key in sorted(by_type):
            print(f"  {key}: {by_type[key]}")
    print(f"Report: {out_path}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run black-box PCB frontend WebSocket lab cases.")
    parser.add_argument("--ws-url", required=True, help="Agent WebSocket URL, e.g. ws://127.0.0.1:7073")
    parser.add_argument("--cases", required=True, type=Path, help="JSONL case file")
    parser.add_argument("--out", required=True, type=Path, help="JSONL report path")
    parser.add_argument("--timeout", type=float, default=120.0, help="Per-turn timeout in seconds")
    parser.add_argument("--session-prefix", default="lab", help="Generated sessionId prefix")
    parser.add_argument("--stop-on-fail", action="store_true", help="Stop after the first failing case")
    parser.add_argument("--verbose-frames", action="store_true", help="Print each WebSocket frame summary")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = LabConfig(
        ws_url=args.ws_url,
        cases_path=args.cases,
        out_path=args.out,
        timeout=args.timeout,
        session_prefix=args.session_prefix,
        stop_on_fail=args.stop_on_fail,
        verbose_frames=args.verbose_frames,
    )
    try:
        results = asyncio.run(run_all(config))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"pcb_frontend_lab failed: {exc}", file=sys.stderr)
        return 2
    print_summary(results, config.out_path)
    return 1 if any(not item.get("passed") for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
