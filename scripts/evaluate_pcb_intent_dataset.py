"""Evaluate PCB/SWSD intent classification against a JSONL label set."""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from gateway.config import PlatformConfig
from gateway.platforms.websocket import WebSocketAdapter
from agent.swsd.intent_field import estimate_intent_field
from agent.swsd.intent_policy import apply_swsd2_policy, apply_swsd3_policy, apply_swsd4_policy
from agent.swsd.skill_grounding import retrieve_skill_memory


DEFAULT_DATASET_PATH = Path(r"F:\doctor\hermes-agent\邮件\intent_training_500.jsonl")
REQUIRED_FIELDS = {
    "id",
    "text",
    "intent",
    "route_mode",
    "flow_state",
    "category",
    "bootstrap_get_project",
    "output",
}
GROUP_KEYS = ("split", "category", "intent", "flow_state")
PROMPT_STYLES = ("lean", "full")
DEFAULT_INTENT_STOP = ["\n\nThinking Process", "\nThinking Process", "```", "</json>"]


def default_dataset_path() -> Path:
    return Path(os.getenv("PCB_INTENT_EVAL_DATASET", "") or DEFAULT_DATASET_PATH)


def load_dataset(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8-sig") as fh:
        for line_no, line in enumerate(fh, 1):
            raw = line.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no}: invalid JSONL: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_no}: row must be an object")
            missing = sorted(REQUIRED_FIELDS - set(row))
            if missing:
                raise ValueError(f"{path}:{line_no}: missing required fields: {', '.join(missing)}")
            rows.append(row)
    return rows


def validate_dataset(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    failures: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        row_id = str(row.get("id") or f"line-{index}")
        try:
            output = json.loads(str(row.get("output") or "{}"))
        except json.JSONDecodeError as exc:
            failures.append({"id": row_id, "type": "bad_output_json", "message": str(exc)})
            continue
        checks = {
            "intent": row.get("intent"),
            "route_mode": row.get("route_mode"),
            "should_call_get_project_data": row.get("bootstrap_get_project"),
        }
        for key, expected in checks.items():
            if output.get(key) != expected:
                failures.append(
                    {
                        "id": row_id,
                        "type": "output_mismatch",
                        "field": key,
                        "expected": expected,
                        "actual": output.get(key),
                    }
                )
    return failures


def make_adapter(*, llm_enabled: bool = False) -> WebSocketAdapter:
    return WebSocketAdapter(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": 0,
                "route_intent_llm_enabled": llm_enabled,
                "fanout_param_llm_enabled": False,
                "trace_pcb_messages": False,
                "swsd_enabled": False,
            },
        )
    )


def prepare_adapter_session(adapter: WebSocketAdapter, session_id: str, row: dict[str, Any]) -> None:
    flow_state = str(row.get("flow_state") or "idle")
    adapter._session_flow_states[session_id] = flow_state
    adapter._session_modes[session_id] = "pcb" if flow_state != "idle" else "chat"
    adapter._session_mode_lock_until[session_id] = 0.0
    if flow_state == "wait_selection":
        adapter._session_selection_labels[session_id] = ("U7", "U23", "U27", "U42", "U128", "U256", "FPGA1")
    elif flow_state == "wait_router_type":
        adapter._session_selected_targets[session_id] = "U27"
        adapter._session_selection_labels[session_id] = ("U27",)
    elif flow_state == "wait_confirm":
        adapter._session_selected_targets[session_id] = "U27"
        adapter._session_router_types[session_id] = "135"
        adapter._session_fanout_params[session_id] = {"selectedBGA": "U27", "routerType": "135"}


def build_eval_prompt(
    adapter: WebSocketAdapter,
    *,
    session_id: str,
    user_text: str,
    project_id: str,
    prompt_style: str = "lean",
) -> list[dict[str, str]]:
    if prompt_style == "full":
        return adapter._build_route_intent_prompt(
            session_id=session_id,
            user_text=user_text,
            project_id=project_id,
        )

    flow_state = str(adapter._session_flow_states.get(session_id) or "idle")
    session_mode = str(adapter._session_modes.get(session_id) or "chat")
    selection_labels = list(adapter._session_selection_labels.get(session_id) or ())
    selected_target = str(adapter._session_selected_targets.get(session_id) or "")
    router_type = str(adapter._session_router_types.get(session_id) or "")

    system_prompt = (
        "你是 PCB Agent 的意图分类器。"
        "不要解释，不要输出推理，不要输出 markdown，不要输出代码块。"
        "直接输出且只输出一个 JSON 对象。"
        'JSON keys: "intent", "route_mode", "should_call_get_project_data", "reason_code". '
        'intent must be one of ["chat","pcb_entry","pcb_select_target","pcb_followup",'
        '"pcb_confirm_route","pcb_modify_params","pcb_reroute_selected","cancel","unclear"]. '
        'route_mode must be "chat" or "pcb". '
        "核心规则："
        "概念解释/区别比较且没有执行要求=>chat；"
        "开始 BGA/PCB 逃逸/扇出/布线=>pcb_entry；"
        "删除已选走线并重布/reroute/ripup=>pcb_reroute_selected；"
        "取消/停止/退出当前流程=>cancel；"
        "wait_selection 中选择 U7/U23/FPGA1 之类器件=>pcb_select_target；"
        "wait_router_type 中 arc/135/RL/北科大=>pcb_followup；"
        "wait_confirm 中 开始/执行/go/继续=>pcb_confirm_route；"
        "wait_confirm 中 含糊确认词 嗯/好的/随便/再想想=>unclear 且 route_mode=pcb；"
        "流程中不清楚但仍在 PCB 上下文=>unclear 且 route_mode=pcb；"
        "cancel 时 route_mode=chat；"
        "仅当 intent=pcb_entry 时 should_call_get_project_data=true，否则 false。"
    )
    user_prompt = (
        f"session_mode={session_mode}\n"
        f"flow_state={flow_state}\n"
        f"has_project_id={'yes' if bool(project_id) else 'no'}\n"
        f"selection_labels={json.dumps(selection_labels, ensure_ascii=False)}\n"
        f"selected_target={selected_target or '-'}\n"
        f"router_type={router_type or '-'}\n"
        f"user_text={json.dumps(str(user_text or ''), ensure_ascii=False)}"
    )
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]


def expected_from_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "intent": row.get("intent"),
        "route_mode": row.get("route_mode"),
        "bootstrap_get_project": bool(row.get("bootstrap_get_project")),
    }


def result_matches(expected: dict[str, Any], actual: dict[str, Any]) -> bool:
    return (
        expected.get("intent") == actual.get("intent")
        and expected.get("route_mode") == actual.get("route_mode")
        and bool(expected.get("bootstrap_get_project")) == bool(actual.get("bootstrap_get_project"))
    )


def evaluate_rule(rows: list[dict[str, Any]]) -> dict[str, Any]:
    adapter = make_adapter(llm_enabled=False)
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        session_id = f"intent-rule-{index}"
        prepare_adapter_session(adapter, session_id, row)
        decision = adapter._decide_route(session_id, str(row.get("text") or ""))
        actual = {
            "intent": decision.intent,
            "route_mode": decision.mode,
            "bootstrap_get_project": bool(decision.bootstrap_get_project),
            "reason": decision.reason,
        }
        expected = expected_from_row(row)
        results.append(build_case_result(row, expected, actual, raw_output="", error=""))
    return summarize_results(results, evaluator="rule")


def parse_route_intent_strict(adapter: WebSocketAdapter, raw_output: str):
    data = adapter._try_parse_route_intent_dict(raw_output)
    if data:
        parsed = adapter._intent_from_dict(data, source="jsonish")
        if parsed:
            return parsed
    data = adapter._try_parse_route_intent_kv(raw_output)
    if data:
        parsed = adapter._intent_from_dict(data, source="kv")
        if parsed:
            return parsed
    return None


def _llm_attempt_status(error: str, parsed_source: str, meta: dict[str, Any]) -> str:
    if error and error != "unparsed_output":
        return "timeout" if "TimeoutError" in error else "error"
    if parsed_source:
        prefix = "retry" if meta.get("attempt") == "retry" else "pass1"
        return f"{prefix} parsed:{parsed_source}"
    if meta.get("stream_pruned"):
        return "pass1 pruned retry512" if meta.get("attempt") == "pass1" else "retry pruned"
    return "unparsed"


def _empty_llm_actual() -> dict[str, Any]:
    return {
        "intent": "",
        "route_mode": "",
        "bootstrap_get_project": False,
        "reason": "",
        "source": "",
    }


def run_llm_attempt(
    adapter: WebSocketAdapter,
    pcb_model_runtime,
    *,
    session_id: str,
    row: dict[str, Any],
    timeout_s: float,
    max_tokens: int,
    prompt_style: str,
    stream_until_json: bool,
    strict_raw: bool,
    attempt_name: str,
) -> tuple[dict[str, Any], str, str, dict[str, Any]]:
    started = time.monotonic()
    raw_output = ""
    error = ""
    actual = _empty_llm_actual()
    meta: dict[str, Any] = {
        "attempt": attempt_name,
        "max_tokens": max_tokens,
        "stream_until_json": stream_until_json,
        "parse_source": "",
        "elapsed_s": 0.0,
    }
    try:
        raw_output, model_meta = pcb_model_runtime.chat_completion_text(
            stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
            messages=build_eval_prompt(
                adapter,
                session_id=session_id,
                user_text=str(row.get("text") or ""),
                project_id="intent-eval",
                prompt_style=prompt_style,
            ),
            temperature=0,
            top_p=1,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            stream_until_json=stream_until_json,
            stream_prune_token_budget=160,
            stop=DEFAULT_INTENT_STOP if stream_until_json else None,
        )
        if stream_until_json and not raw_output and int(model_meta.get("stream_chunks") or 0) == 0:
            fallback_started = time.monotonic()
            raw_output, fallback_meta = pcb_model_runtime.chat_completion_text(
                stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                messages=build_eval_prompt(
                    adapter,
                    session_id=session_id,
                    user_text=str(row.get("text") or ""),
                    project_id="intent-eval",
                    prompt_style=prompt_style,
                ),
                temperature=0,
                top_p=1,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                stream_until_json=False,
            )
            model_meta = {
                **model_meta,
                "stream_fallback_nonstream": True,
                "stream_fallback_elapsed_s": round(time.monotonic() - fallback_started, 3),
                "fallback_usage": fallback_meta.get("usage") or {},
                "fallback_response_id": fallback_meta.get("response_id"),
                "usage": fallback_meta.get("usage") or model_meta.get("usage") or {},
            }
        meta.update(
            {
                "model": model_meta.get("model"),
                "usage": model_meta.get("usage") or {},
                "response_id": model_meta.get("response_id"),
                "stream_pruned": bool(model_meta.get("stream_pruned")),
                "stream_finish_reason": model_meta.get("stream_finish_reason", ""),
                "stream_thinking_tokens": model_meta.get("stream_thinking_tokens", 0),
                "stream_chunks": model_meta.get("stream_chunks", 0),
                "stream_fallback_nonstream": bool(model_meta.get("stream_fallback_nonstream")),
                "stream_fallback_elapsed_s": model_meta.get("stream_fallback_elapsed_s", 0),
            }
        )
        parsed = (
            parse_route_intent_strict(adapter, raw_output)
            if strict_raw
            else adapter._parse_route_intent_output(raw_output)
        )
        if parsed is None:
            error = "unparsed_output"
        else:
            actual = {
                "intent": parsed.intent,
                "route_mode": parsed.route_mode,
                "bootstrap_get_project": bool(parsed.should_call_get_project_data),
                "reason": parsed.reason_code or parsed.source,
                "source": parsed.source,
            }
            meta["parse_source"] = parsed.source
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    meta["elapsed_s"] = round(time.monotonic() - started, 3)
    meta["status"] = _llm_attempt_status(error, str(meta.get("parse_source") or ""), meta)
    return actual, raw_output, error, meta


def evaluate_llm(
    rows: list[dict[str, Any]],
    *,
    timeout_s: float = 8.0,
    max_tokens: int = 256,
    progress_every: int = 10,
    partial_out_path: Path | None = None,
    prompt_style: str = "lean",
    strict_raw: bool = False,
    stream_until_json: bool = False,
    adaptive_retry: bool = False,
    retry_max_tokens: int = 512,
) -> dict[str, Any]:
    from tools import pcb_model_runtime

    adapter = make_adapter(llm_enabled=True)
    results: list[dict[str, Any]] = []
    started = time.monotonic()
    total = len(rows)
    for index, row in enumerate(rows, 1):
        session_id = f"intent-llm-{index}"
        prepare_adapter_session(adapter, session_id, row)
        expected = expected_from_row(row)
        attempts: list[dict[str, Any]] = []
        actual, raw_output, error, meta = run_llm_attempt(
            adapter,
            pcb_model_runtime,
            session_id=session_id,
            row=row,
            timeout_s=timeout_s,
            max_tokens=max_tokens,
            prompt_style=prompt_style,
            stream_until_json=stream_until_json,
            strict_raw=strict_raw,
            attempt_name="pass1",
        )
        attempts.append(meta)
        if adaptive_retry and error == "unparsed_output":
            actual, raw_output, error, retry_meta = run_llm_attempt(
                adapter,
                pcb_model_runtime,
                session_id=session_id,
                row=row,
                timeout_s=timeout_s,
                max_tokens=retry_max_tokens,
                prompt_style=prompt_style,
                stream_until_json=stream_until_json,
                strict_raw=strict_raw,
                attempt_name="retry",
            )
            attempts.append(retry_meta)
        result_meta = {
            "attempts": attempts,
            "elapsed_s": round(sum(float(item.get("elapsed_s") or 0) for item in attempts), 3),
            "parse_source": (attempts[-1].get("parse_source") if attempts else "") or "",
            "status": attempts[-1].get("status") if attempts else "",
            "raw_output_preview": raw_output[:200],
        }
        results.append(build_case_result(row, expected, actual, raw_output=raw_output, error=error, meta=result_meta))
        if progress_every > 0 and (index == 1 or index % progress_every == 0 or index == total):
            passed = sum(1 for item in results if item.get("passed"))
            failed = len(results) - passed
            elapsed = time.monotonic() - started
            last_error = error.splitlines()[0][:160] if error else ""
            status = result_meta.get("status") or ""
            print(
                f"[llm] {index}/{total} done, passed={passed}, failed={failed}, "
                f"accuracy={_accuracy(passed, len(results)):.2%}, elapsed={elapsed:.1f}s, status={status}"
                + (f", last_error={last_error}" if last_error else ""),
                flush=True,
            )
        if partial_out_path is not None and (index == 1 or index % max(1, progress_every) == 0 or index == total):
            partial = summarize_results(results, evaluator="llm_partial")
            partial["requested_total"] = total
            partial["prompt_style"] = prompt_style
            partial["strict_raw"] = strict_raw
            partial["stream_until_json"] = stream_until_json
            partial["adaptive_retry"] = adaptive_retry
            partial["retry_max_tokens"] = retry_max_tokens
            write_json(partial_out_path, partial)
    report = summarize_results(results, evaluator="llm")
    report["prompt_style"] = prompt_style
    report["strict_raw"] = strict_raw
    report["stream_until_json"] = stream_until_json
    report["adaptive_retry"] = adaptive_retry
    report["retry_max_tokens"] = retry_max_tokens
    return report


def _raw_result_by_id(report_path: Path) -> dict[str, dict[str, Any]]:
    if not report_path.exists():
        return {}
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return {str(item.get("id")): item for item in data.get("results", []) if isinstance(item, dict)}


def evaluate_swsd2(rows: list[dict[str, Any]], *, raw_llm_report_path: Path | None = None) -> dict[str, Any]:
    adapter = make_adapter(llm_enabled=False)
    raw_by_id = _raw_result_by_id(raw_llm_report_path) if raw_llm_report_path else {}
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        session_id = f"intent-swsd2-{index}"
        prepare_adapter_session(adapter, session_id, row)
        expected = expected_from_row(row)
        raw_item = raw_by_id.get(str(row.get("id"))) or {}
        candidate = raw_item.get("actual") or {}
        flow_state = str(row.get("flow_state") or "idle")
        session_mode = "pcb" if flow_state != "idle" else "chat"
        decision = apply_swsd2_policy(
            text=str(row.get("text") or ""),
            flow_state=flow_state,
            session_mode=session_mode,
            candidate=candidate,
        )
        actual = {
            "intent": decision.intent,
            "route_mode": decision.route_mode,
            "bootstrap_get_project": decision.should_call_get_project_data,
            "reason": decision.reason,
            "task_intent": decision.hierarchy.task_intent,
            "control_intent": decision.hierarchy.control_intent,
            "meta_intent": decision.hierarchy.meta_intent,
            "invalid_intent": decision.hierarchy.invalid_intent,
        }
        raw_output = str(raw_item.get("raw_output") or "")
        results.append(build_case_result(row, expected, actual, raw_output=raw_output, error=""))
    return summarize_results(results, evaluator="swsd2")


def evaluate_swsd3(rows: list[dict[str, Any]], *, raw_llm_report_path: Path | None = None) -> dict[str, Any]:
    adapter = make_adapter(llm_enabled=False)
    raw_by_id = _raw_result_by_id(raw_llm_report_path) if raw_llm_report_path else {}
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        session_id = f"intent-swsd3-{index}"
        prepare_adapter_session(adapter, session_id, row)
        expected = expected_from_row(row)
        raw_item = raw_by_id.get(str(row.get("id"))) or {}
        candidate = raw_item.get("actual") or {}
        flow_state = str(row.get("flow_state") or "idle")
        session_mode = "pcb" if flow_state != "idle" else "chat"
        decision = apply_swsd3_policy(
            text=str(row.get("text") or ""),
            flow_state=flow_state,
            session_mode=session_mode,
            candidate=candidate,
        )
        actual = {
            "intent": decision.intent,
            "route_mode": decision.route_mode,
            "bootstrap_get_project": decision.should_call_get_project_data,
            "reason": decision.reason,
            "task_intent": decision.hierarchy.task_intent,
            "control_intent": decision.hierarchy.control_intent,
            "meta_intent": decision.hierarchy.meta_intent,
            "invalid_intent": decision.hierarchy.invalid_intent,
            "execution_intent": decision.execution_intent,
            "guard_reason": decision.guard_reason,
            "allow_workflow_entry": decision.allow_workflow_entry,
        }
        raw_output = str(raw_item.get("raw_output") or "")
        results.append(build_case_result(row, expected, actual, raw_output=raw_output, error=""))
    return summarize_results(results, evaluator="swsd3")


def _intent_field_from_raw_item(raw_item: dict[str, Any]) -> dict[str, Any] | None:
    actual = raw_item.get("actual") if isinstance(raw_item, dict) else {}
    if not isinstance(actual, dict):
        return None
    field = actual.get("intent_field") or actual.get("intentField")
    return field if isinstance(field, dict) else None


def evaluate_swsd4(
    rows: list[dict[str, Any]],
    *,
    raw_llm_report_path: Path | None = None,
    call_encoder: bool = False,
) -> dict[str, Any]:
    adapter = make_adapter(llm_enabled=False)
    raw_by_id = _raw_result_by_id(raw_llm_report_path) if raw_llm_report_path else {}
    results: list[dict[str, Any]] = []
    for index, row in enumerate(rows, 1):
        session_id = f"intent-swsd4-{index}"
        prepare_adapter_session(adapter, session_id, row)
        expected = expected_from_row(row)
        raw_item = raw_by_id.get(str(row.get("id"))) or {}
        candidate = raw_item.get("actual") or {}
        flow_state = str(row.get("flow_state") or "idle")
        session_mode = "pcb" if flow_state != "idle" else "chat"
        field = _intent_field_from_raw_item(raw_item)
        encoder_error = ""
        if field is None and call_encoder:
            try:
                encoded = estimate_intent_field(
                    user_text=str(row.get("text") or ""),
                    flow_state=flow_state,
                    session_mode=session_mode,
                    candidate=candidate,
                )
                field = encoded.as_dict()
            except Exception as exc:
                encoder_error = f"{type(exc).__name__}: {exc}"
        grounding = retrieve_skill_memory(str(row.get("text") or ""), flow_state)
        decision = apply_swsd4_policy(
            text=str(row.get("text") or ""),
            flow_state=flow_state,
            session_mode=session_mode,
            candidate=candidate,
            intent_field=field,
            skill_grounding=grounding,
        )
        actual = {
            "intent": decision.intent,
            "route_mode": decision.route_mode,
            "bootstrap_get_project": decision.should_call_get_project_data,
            "reason": decision.reason,
            "intent_field": field or {},
            "skill_grounding_count": len(grounding),
            "tool_misuse_flag": bool(decision.route_mode == "pcb" and expected.get("route_mode") == "chat"),
        }
        raw_output = str(raw_item.get("raw_output") or "")
        meta = {"encoder_error": encoder_error, "status": "encoder_error_fallback" if encoder_error else ""}
        results.append(build_case_result(row, expected, actual, raw_output=raw_output, error="", meta=meta))
    return summarize_results(results, evaluator="swsd4")


def compare_policies(rows: list[dict[str, Any]], *, raw_llm_report_path: Path | None = None) -> dict[str, Any]:
    reports = {
        "swsd2": evaluate_swsd2(rows, raw_llm_report_path=raw_llm_report_path),
        "swsd3": evaluate_swsd3(rows, raw_llm_report_path=raw_llm_report_path),
        "swsd4": evaluate_swsd4(rows, raw_llm_report_path=raw_llm_report_path),
    }
    return {
        "evaluator": "compare-policies",
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            name: {"passed": report["passed"], "total": report["total"], "accuracy": report["accuracy"]}
            for name, report in reports.items()
        },
        "reports": reports,
    }


def build_case_result(
    row: dict[str, Any],
    expected: dict[str, Any],
    actual: dict[str, Any],
    *,
    raw_output: str,
    error: str,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    passed = not error and result_matches(expected, actual)
    return {
        "id": row.get("id"),
        "text": row.get("text"),
        "split": row.get("split", ""),
        "category": row.get("category", ""),
        "flow_state": row.get("flow_state", ""),
        "expected": expected,
        "actual": actual,
        "passed": passed,
        "error": error,
        "raw_output": raw_output,
        "raw_output_preview": (meta or {}).get("raw_output_preview", raw_output[:200]),
        "meta": meta or {},
    }


def _accuracy(passed: int, total: int) -> float:
    return round((passed / total), 6) if total else 0.0


def grouped_accuracy(results: list[dict[str, Any]], key: str) -> dict[str, dict[str, Any]]:
    totals: dict[str, Counter] = defaultdict(Counter)
    for item in results:
        value = str(item.get(key) or "")
        totals[value]["total"] += 1
        if item.get("passed"):
            totals[value]["passed"] += 1
    return {
        value: {
            "passed": counts["passed"],
            "total": counts["total"],
            "accuracy": _accuracy(counts["passed"], counts["total"]),
        }
        for value, counts in sorted(totals.items())
    }


def confusion_matrix(results: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    matrix: dict[str, Counter] = defaultdict(Counter)
    for item in results:
        expected = str((item.get("expected") or {}).get("intent") or "")
        actual = str((item.get("actual") or {}).get("intent") or "")
        matrix[expected][actual] += 1
    return {expected: dict(actuals) for expected, actuals in sorted(matrix.items())}


def summarize_results(results: list[dict[str, Any]], *, evaluator: str) -> dict[str, Any]:
    total = len(results)
    passed = sum(1 for item in results if item.get("passed"))
    failures = [item for item in results if not item.get("passed")]
    elapsed_values = [
        float((item.get("meta") or {}).get("elapsed_s") or 0)
        for item in results
        if (item.get("meta") or {}).get("elapsed_s") is not None
    ]
    sorted_elapsed = sorted(value for value in elapsed_values if value >= 0)
    p95_index = int(round((len(sorted_elapsed) - 1) * 0.95)) if sorted_elapsed else 0
    parse_sources = Counter(str((item.get("meta") or {}).get("parse_source") or "") for item in results)
    statuses = Counter(str((item.get("meta") or {}).get("status") or "") for item in results)
    return {
        "evaluator": evaluator,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "accuracy": _accuracy(passed, total),
        "by_group": {key: grouped_accuracy(results, key) for key in GROUP_KEYS},
        "confusion_matrix": confusion_matrix(results),
        "parse_sources": dict(parse_sources),
        "statuses": dict(statuses),
        "timing": {
            "avg_elapsed_s": round(sum(sorted_elapsed) / len(sorted_elapsed), 3) if sorted_elapsed else 0,
            "p95_elapsed_s": round(sorted_elapsed[p95_index], 3) if sorted_elapsed else 0,
        },
        "failure_samples": failures[:50],
        "results": results,
    }


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_summary_md(path: Path, reports: dict[str, dict[str, Any]], dataset_path: Path, validation_failures: list[dict[str, Any]]) -> None:
    lines = [
        "# PCB Intent Dataset Evaluation",
        "",
        f"Dataset: `{dataset_path}`",
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        "## Overall",
        "",
        "| Evaluator | Passed | Total | Accuracy |",
        "| --- | ---: | ---: | ---: |",
    ]
    for name, report in reports.items():
        lines.append(f"| {name} | {report['passed']} | {report['total']} | {report['accuracy']:.2%} |")
    lines.extend(["", "## Validation", ""])
    if validation_failures:
        lines.append(f"- Dataset validation failures: {len(validation_failures)}")
    else:
        lines.append("- Dataset validation failures: 0")
    for name, report in reports.items():
        if report.get("parse_sources") or report.get("statuses"):
            lines.extend(["", f"## {name} Diagnostics", ""])
            timing = report.get("timing") or {}
            lines.append(f"- Avg elapsed: {float(timing.get('avg_elapsed_s') or 0):.3f}s")
            lines.append(f"- P95 elapsed: {float(timing.get('p95_elapsed_s') or 0):.3f}s")
            if report.get("parse_sources"):
                lines.append("- Parse sources: " + json.dumps(report.get("parse_sources"), ensure_ascii=False))
            if report.get("statuses"):
                lines.append("- Statuses: " + json.dumps(report.get("statuses"), ensure_ascii=False))
        lines.extend(["", f"## {name} By Category", "", "| Category | Passed | Total | Accuracy |", "| --- | ---: | ---: | ---: |"])
        for category, stats in report["by_group"]["category"].items():
            lines.append(f"| {category or '-'} | {stats['passed']} | {stats['total']} | {stats['accuracy']:.2%} |")
        lines.extend(["", f"## {name} Failure Samples", ""])
        samples = report.get("failure_samples") or []
        if not samples:
            lines.append("- None")
        for item in samples[:15]:
            lines.append(
                "- "
                + json.dumps(
                    {
                        "id": item.get("id"),
                        "text": item.get("text"),
                        "expected": item.get("expected"),
                        "actual": item.get("actual"),
                        "error": item.get("error"),
                    },
                    ensure_ascii=False,
                )
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate PCB/SWSD intent classification over a JSONL label set.")
    parser.add_argument("--dataset", type=Path, default=default_dataset_path(), help="Intent JSONL dataset path")
    parser.add_argument("--out-dir", type=Path, default=Path("reports/intent_eval"), help="Report output directory")
    parser.add_argument("--mode", choices=("rule", "llm", "swsd2", "swsd3", "swsd4", "compare-policies", "both", "all"), default="rule", help="Evaluator to run")
    parser.add_argument("--llm-timeout", type=float, default=8.0, help="Per-row LLM timeout in seconds")
    parser.add_argument("--llm-max-tokens", type=int, default=256, help="LLM max_tokens per row")
    parser.add_argument("--progress-every", type=int, default=10, help="Print and checkpoint LLM progress every N rows")
    parser.add_argument("--prompt-style", choices=PROMPT_STYLES, default="lean", help="Prompt profile for LLM intent eval")
    parser.add_argument("--strict-raw", action="store_true", help="Only accept JSON/KV raw LLM outputs; disable label_from_text fallback")
    parser.add_argument("--stream-until-json", action="store_true", help="Use streaming reasoning pruning and stop after JSON/KV")
    parser.add_argument("--adaptive-retry", action="store_true", help="Retry unparsed LLM rows with --retry-max-tokens")
    parser.add_argument("--retry-max-tokens", type=int, default=512, help="Adaptive retry max_tokens")
    parser.add_argument("--call-swsd4-encoder", action="store_true", help="Call the SWSD4 semantic encoder when raw reports lack intent_field")
    parser.add_argument("--limit", type=int, default=0, help="Optional row limit for smoke runs")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    rows = load_dataset(args.dataset)
    if args.limit and args.limit > 0:
        rows = rows[: args.limit]
    validation_failures = validate_dataset(rows)
    reports: dict[str, dict[str, Any]] = {}
    if args.mode in {"rule", "both", "all"}:
        reports["rule"] = evaluate_rule(rows)
        write_json(args.out_dir / "rule_eval.json", reports["rule"])
    if args.mode in {"llm", "both", "all"}:
        reports["llm"] = evaluate_llm(
            rows,
            timeout_s=args.llm_timeout,
            max_tokens=args.llm_max_tokens,
            progress_every=args.progress_every,
            partial_out_path=args.out_dir / "llm_eval.partial.json",
            prompt_style=args.prompt_style,
            strict_raw=args.strict_raw,
            stream_until_json=args.stream_until_json,
            adaptive_retry=args.adaptive_retry,
            retry_max_tokens=args.retry_max_tokens,
        )
        write_json(args.out_dir / "llm_eval.json", reports["llm"])
    if args.mode in {"swsd2", "all"}:
        reports["swsd2"] = evaluate_swsd2(rows, raw_llm_report_path=args.out_dir / "llm_eval.json")
        write_json(args.out_dir / "swsd2_eval.json", reports["swsd2"])
    if args.mode in {"swsd3", "all"}:
        reports["swsd3"] = evaluate_swsd3(rows, raw_llm_report_path=args.out_dir / "llm_eval.json")
        write_json(args.out_dir / "swsd3_eval.json", reports["swsd3"])
    if args.mode in {"swsd4", "all"}:
        reports["swsd4"] = evaluate_swsd4(
            rows,
            raw_llm_report_path=args.out_dir / "llm_eval.json",
            call_encoder=args.call_swsd4_encoder,
        )
        write_json(args.out_dir / "swsd4_eval.json", reports["swsd4"])
    if args.mode == "compare-policies":
        comparison = compare_policies(rows, raw_llm_report_path=args.out_dir / "llm_eval.json")
        write_json(args.out_dir / "policy_compare.json", comparison)
        reports.update(comparison["reports"])
    write_summary_md(args.out_dir / "summary.md", reports, args.dataset, validation_failures)
    print(f"Dataset rows: {len(rows)}")
    for name, report in reports.items():
        print(f"{name}: {report['passed']}/{report['total']} ({report['accuracy']:.2%})")
    print(f"Summary: {args.out_dir / 'summary.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
