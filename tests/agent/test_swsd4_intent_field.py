from __future__ import annotations

import json

from agent.swsd.decision_policy import WorkflowContext, decide_with_intent_field
from agent.swsd.intent_field import IntentFieldOutput, estimate_intent_field, parse_intent_field_output
from agent.swsd.intent_policy import ROUTE_MODE_CHAT, ROUTE_MODE_PCB
from agent.swsd.skill_grounding import retrieve_skill_memory
from tools import pcb_model_runtime


def test_intent_field_parser_normalizes_probability_distribution():
    parsed = parse_intent_field_output('{"chat":2,"analyze":1,"execute":1,"meta":0,"uncertainty":1.2}')

    assert parsed.chat == 0.5
    assert parsed.analyze == 0.25
    assert parsed.execute == 0.25
    assert parsed.meta == 0
    assert parsed.uncertainty == 1.0


def test_semantic_encoder_uses_tool_planning_stage(monkeypatch):
    captured = {}

    def fake_chat_completion_text(**kwargs):
        captured.update(kwargs)
        return json.dumps({"chat": 0.1, "analyze": 0.1, "execute": 0.75, "meta": 0.05, "uncertainty": 0.1}), {}

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)

    result = estimate_intent_field(user_text="请执行 BGA fanout", flow_state="idle", session_mode="chat")

    assert result.execute > 0.7
    assert captured["stage"] == pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT
    assert captured["stage"] != pcb_model_runtime.STAGE_REROUTE


def test_probabilistic_policy_execute_goes_to_pcb():
    decision = decide_with_intent_field(
        text="paraphrased execution request",
        session_mode=ROUTE_MODE_CHAT,
        candidate={"intent": "pcb_entry", "route_mode": "pcb"},
        intent_field=IntentFieldOutput(chat=0.05, analyze=0.1, execute=0.8, meta=0.05, uncertainty=0.1),
        workflow_context=WorkflowContext(workflow_state="idle", allowed_transitions=("pcb_entry",)),
    )

    assert decision.route_mode == ROUTE_MODE_PCB
    assert decision.intent == "pcb_entry"


def test_probabilistic_policy_discussion_does_not_use_keywords():
    decision = decide_with_intent_field(
        text="BGA fanout PCB reroute",
        session_mode=ROUTE_MODE_CHAT,
        candidate={"intent": "pcb_entry", "route_mode": "pcb"},
        intent_field=IntentFieldOutput(chat=0.7, analyze=0.1, execute=0.1, meta=0.1, uncertainty=0.1),
        workflow_context=WorkflowContext(workflow_state="idle", allowed_transitions=("pcb_entry",)),
    )

    assert decision.route_mode == ROUTE_MODE_CHAT
    assert decision.intent == "chat"


def test_probabilistic_policy_uncertainty_defers():
    decision = decide_with_intent_field(
        text="ambiguous",
        session_mode=ROUTE_MODE_CHAT,
        candidate={"intent": "pcb_entry", "route_mode": "pcb"},
        intent_field=IntentFieldOutput(chat=0.25, analyze=0.25, execute=0.25, meta=0.25, uncertainty=0.5),
        workflow_context=WorkflowContext(workflow_state="idle"),
    )

    assert decision.intent == "unclear"
    assert decision.route_mode == ROUTE_MODE_CHAT


def test_skill_grounding_returns_structured_items(tmp_path):
    skill = tmp_path / "SKILL.md"
    skill.write_text("PCB reroute operation strategy and failure mode.", encoding="utf-8")

    items = retrieve_skill_memory("PCB reroute", "idle", skill_paths=[skill])

    assert len(items) == 1
    assert items[0].principle
    assert items[0].operation
    assert items[0].failure_mode
