from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts import evaluate_pcb_intent_dataset as eval_script


DATASET_PATH = eval_script.default_dataset_path()


def test_external_intent_dataset_schema_if_available():
    if not DATASET_PATH.exists():
        pytest.skip(f"external intent dataset not found: {DATASET_PATH}")

    rows = eval_script.load_dataset(DATASET_PATH)
    failures = eval_script.validate_dataset(rows)

    assert len(rows) == 500
    assert failures == []


def test_rule_evaluator_summarizes_matches_and_failures():
    rows = [
        {
            "id": "ok-chat",
            "text": "BGA 和 QFP 有什么区别？",
            "intent": "chat",
            "route_mode": "chat",
            "flow_state": "idle",
            "category": "chat_consultation",
            "bootstrap_get_project": False,
            "split": "eval",
            "output": json.dumps(
                {
                    "intent": "chat",
                    "route_mode": "chat",
                    "confidence": 0.9,
                    "should_call_get_project_data": False,
                }
            ),
        },
        {
            "id": "ok-pcb",
            "text": "请对 U27 做 BGA 逃逸布线",
            "intent": "pcb_entry",
            "route_mode": "pcb",
            "flow_state": "idle",
            "category": "pcb_entry_fanout",
            "bootstrap_get_project": True,
            "split": "eval",
            "output": json.dumps(
                {
                    "intent": "pcb_entry",
                    "route_mode": "pcb",
                    "confidence": 0.9,
                    "should_call_get_project_data": True,
                }
            ),
        },
    ]

    report = eval_script.evaluate_rule(rows)

    assert report["evaluator"] == "rule"
    assert report["total"] == 2
    assert report["by_group"]["split"]["eval"]["total"] == 2
    assert "chat" in report["confusion_matrix"]
    assert len(report["results"]) == 2


def test_llm_evaluator_uses_tool_planning_chat_stage(monkeypatch):
    from tools import pcb_model_runtime

    captured: dict[str, object] = {}

    def fake_chat_completion_text(**kwargs):
        captured.update(kwargs)
        return (
            '{"intent":"pcb_entry","route_mode":"pcb","confidence":0.92,'
            '"should_call_get_project_data":true}',
            {"stage": kwargs["stage"]},
        )

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)

    rows = [
        {
            "id": "llm-pcb",
            "text": "帮我做 BGA 逃逸布线",
            "intent": "pcb_entry",
            "route_mode": "pcb",
            "flow_state": "idle",
            "category": "pcb_entry_fanout",
            "bootstrap_get_project": True,
            "split": "eval",
            "output": json.dumps(
                {
                    "intent": "pcb_entry",
                    "route_mode": "pcb",
                    "confidence": 0.9,
                    "should_call_get_project_data": True,
                }
            ),
        }
    ]

    report = eval_script.evaluate_llm(rows)

    assert report["passed"] == 1
    assert report["prompt_style"] == "lean"
    assert captured["stage"] == pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT
    assert captured["stage"] != pcb_model_runtime.STAGE_REROUTE
    assert captured["messages"][0]["role"] == "system"
    assert "只输出一个 JSON 对象" in captured["messages"][0]["content"]


def test_llm_strict_raw_does_not_accept_label_from_thinking_text(monkeypatch):
    from tools import pcb_model_runtime

    def fake_chat_completion_text(**kwargs):
        return (
            "Thinking Process:\nThe prompt mentions chat and route_mode many times, "
            "but no final JSON is emitted.",
            {"stage": kwargs["stage"]},
        )

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)

    rows = [
        {
            "id": "strict-chat",
            "text": "帮我做 BGA 逃逸布线",
            "intent": "pcb_entry",
            "route_mode": "pcb",
            "flow_state": "idle",
            "category": "pcb_entry_fanout",
            "bootstrap_get_project": True,
            "split": "eval",
            "output": json.dumps(
                {
                    "intent": "pcb_entry",
                    "route_mode": "pcb",
                    "should_call_get_project_data": True,
                }
            ),
        }
    ]

    report = eval_script.evaluate_llm(rows, strict_raw=True)

    assert report["passed"] == 0
    assert report["results"][0]["error"] == "unparsed_output"
    assert report["results"][0]["actual"]["intent"] == ""
    assert report["results"][0]["meta"]["parse_source"] == ""


def test_llm_adaptive_retry_uses_retry_max_tokens(monkeypatch):
    from tools import pcb_model_runtime

    calls: list[dict[str, object]] = []

    def fake_chat_completion_text(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return ("Thinking Process:\nstill reasoning without JSON", {"stage": kwargs["stage"], "stream_pruned": True})
        return (
            '{"intent":"pcb_entry","route_mode":"pcb","should_call_get_project_data":true}',
            {"stage": kwargs["stage"], "stream_finish_reason": "structured"},
        )

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)

    rows = [
        {
            "id": "retry-pcb",
            "text": "帮我做 BGA 逃逸布线",
            "intent": "pcb_entry",
            "route_mode": "pcb",
            "flow_state": "idle",
            "category": "pcb_entry_fanout",
            "bootstrap_get_project": True,
            "split": "eval",
            "output": json.dumps(
                {
                    "intent": "pcb_entry",
                    "route_mode": "pcb",
                    "should_call_get_project_data": True,
                }
            ),
        }
    ]

    report = eval_script.evaluate_llm(
        rows,
        max_tokens=256,
        strict_raw=True,
        stream_until_json=True,
        adaptive_retry=True,
        retry_max_tokens=512,
    )

    assert report["passed"] == 1
    assert [call["max_tokens"] for call in calls] == [256, 512]
    assert all(call["stream_until_json"] is True for call in calls)
    assert report["results"][0]["meta"]["attempts"][0]["status"] == "pass1 pruned retry512"
    assert report["results"][0]["meta"]["attempts"][1]["status"] == "retry parsed:jsonish"


def test_llm_stream_empty_falls_back_to_nonstream(monkeypatch):
    from tools import pcb_model_runtime

    calls: list[dict[str, object]] = []

    def fake_chat_completion_text(**kwargs):
        calls.append(kwargs)
        if kwargs.get("stream_until_json"):
            return ("", {"stage": kwargs["stage"], "stream_chunks": 0, "stream_finish_reason": "done"})
        return (
            '{"intent":"chat","route_mode":"chat","should_call_get_project_data":false}',
            {"stage": kwargs["stage"], "usage": {"total_tokens": 12}},
        )

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)

    rows = [
        {
            "id": "fallback-chat",
            "text": "什么是阻抗匹配",
            "intent": "chat",
            "route_mode": "chat",
            "flow_state": "idle",
            "category": "chat_general",
            "bootstrap_get_project": False,
            "split": "eval",
            "output": json.dumps(
                {
                    "intent": "chat",
                    "route_mode": "chat",
                    "should_call_get_project_data": False,
                }
            ),
        }
    ]

    report = eval_script.evaluate_llm(rows, strict_raw=True, stream_until_json=True)

    assert report["passed"] == 1
    assert [call["stream_until_json"] for call in calls] == [True, False]
    assert report["results"][0]["meta"]["attempts"][0]["stream_fallback_nonstream"] is True


def test_build_eval_prompt_can_reuse_full_adapter_prompt():
    adapter = eval_script.make_adapter(llm_enabled=True)
    session_id = "prompt-full"
    row = {
        "flow_state": "wait_selection",
        "text": "选 U7",
    }
    eval_script.prepare_adapter_session(adapter, session_id, row)

    messages = eval_script.build_eval_prompt(
        adapter,
        session_id=session_id,
        user_text="选 U7",
        project_id="intent-eval",
        prompt_style="full",
    )

    assert len(messages) == 2
    assert messages[0]["role"] == "system"
    assert "同时尽量输出层级字段" in messages[0]["content"]


def test_swsd2_evaluator_reuses_raw_llm_report(tmp_path):
    rows = [
        {
            "id": "cancel",
            "text": "帮我cancel。",
            "intent": "cancel",
            "route_mode": "chat",
            "flow_state": "idle",
            "category": "cancel",
            "bootstrap_get_project": False,
            "split": "eval",
            "output": json.dumps(
                {
                    "intent": "cancel",
                    "route_mode": "chat",
                    "confidence": 0.9,
                    "should_call_get_project_data": False,
                }
            ),
        },
        {
            "id": "invalid",
            "text": "嗯（不要调用工具）",
            "intent": "unclear",
            "route_mode": "pcb",
            "flow_state": "wait_confirm",
            "category": "flow_invalid",
            "bootstrap_get_project": False,
            "split": "eval",
            "output": json.dumps(
                {
                    "intent": "unclear",
                    "route_mode": "pcb",
                    "confidence": 0.9,
                    "should_call_get_project_data": False,
                }
            ),
        },
    ]
    raw_report = tmp_path / "llm_eval.json"
    raw_report.write_text(
        json.dumps(
            {
                "results": [
                    {"id": "cancel", "actual": {"intent": "cancel", "route_mode": "pcb", "bootstrap_get_project": False}},
                    {"id": "invalid", "actual": {"intent": "chat", "route_mode": "chat", "bootstrap_get_project": False}},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = eval_script.evaluate_swsd2(rows, raw_llm_report_path=raw_report)

    assert report["passed"] == 2
    assert report["results"][0]["actual"]["reason"] == "swsd2_cancel_normalized"
    assert report["results"][1]["actual"]["reason"] == "swsd2_invalid_confirm_turn"


def test_swsd3_evaluator_reuses_raw_llm_report_and_exports_guard_fields(tmp_path):
    rows = [
        {
            "id": "analysis",
            "text": "帮我分析 reroute 利弊（仅说明）",
            "intent": "chat",
            "route_mode": "chat",
            "flow_state": "idle",
            "category": "chat_analysis",
            "bootstrap_get_project": False,
            "split": "eval",
            "output": json.dumps(
                {
                    "intent": "chat",
                    "route_mode": "chat",
                    "confidence": 0.9,
                    "should_call_get_project_data": False,
                }
            ),
        },
        {
            "id": "select",
            "text": "U55",
            "intent": "pcb_select_target",
            "route_mode": "pcb",
            "flow_state": "wait_selection",
            "category": "flow_select",
            "bootstrap_get_project": False,
            "split": "eval",
            "output": json.dumps(
                {
                    "intent": "pcb_select_target",
                    "route_mode": "pcb",
                    "confidence": 0.9,
                    "should_call_get_project_data": False,
                }
            ),
        },
    ]
    raw_report = tmp_path / "llm_eval.json"
    raw_report.write_text(
        json.dumps(
            {
                "results": [
                    {"id": "analysis", "actual": {"intent": "pcb_reroute_selected", "route_mode": "pcb", "bootstrap_get_project": False}},
                    {"id": "select", "actual": {"intent": "unclear", "route_mode": "pcb", "bootstrap_get_project": False}},
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    report = eval_script.evaluate_swsd3(rows, raw_llm_report_path=raw_report)

    assert report["passed"] == 2
    assert report["results"][0]["actual"]["guard_reason"] in {"swsd3_guard_analysis_chat", "swsd3_guard_consult_chat"}
    assert report["results"][0]["actual"]["allow_workflow_entry"] is False
    assert report["results"][1]["actual"]["reason"] == "swsd3_state_select_target_entity"


def test_swsd4_evaluator_reuses_intent_field_from_raw_report(tmp_path, monkeypatch):
    rows = [
        {
            "id": "semantic-chat",
            "text": "换一种说法解释 BGA fanout",
            "intent": "chat",
            "route_mode": "chat",
            "flow_state": "idle",
            "category": "chat_consultation",
            "bootstrap_get_project": False,
            "split": "eval",
            "output": json.dumps(
                {
                    "intent": "chat",
                    "route_mode": "chat",
                    "confidence": 0.9,
                    "should_call_get_project_data": False,
                }
            ),
        }
    ]
    raw_report = tmp_path / "llm_eval.json"
    raw_report.write_text(
        json.dumps(
            {
                "results": [
                    {
                        "id": "semantic-chat",
                        "actual": {
                            "intent": "pcb_entry",
                            "route_mode": "pcb",
                            "bootstrap_get_project": True,
                            "intent_field": {
                                "chat": 0.72,
                                "analyze": 0.15,
                                "execute": 0.08,
                                "meta": 0.05,
                                "uncertainty": 0.1,
                            },
                        },
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(eval_script, "retrieve_skill_memory", lambda *_args, **_kwargs: [])

    report = eval_script.evaluate_swsd4(rows, raw_llm_report_path=raw_report)

    assert report["passed"] == 1
    assert report["results"][0]["actual"]["reason"] == "swsd4_discussion"
    assert report["results"][0]["actual"]["intent_field"]["chat"] == 0.72


def test_validate_dataset_reports_output_mismatch():
    rows = [
        {
            "id": "bad",
            "text": "hello",
            "intent": "chat",
            "route_mode": "chat",
            "flow_state": "idle",
            "category": "chat_general",
            "bootstrap_get_project": False,
            "output": json.dumps(
                {
                    "intent": "pcb_entry",
                    "route_mode": "pcb",
                    "should_call_get_project_data": True,
                }
            ),
        }
    ]

    failures = eval_script.validate_dataset(rows)

    assert {item["field"] for item in failures if item["type"] == "output_mismatch"} == {
        "intent",
        "route_mode",
        "should_call_get_project_data",
    }
