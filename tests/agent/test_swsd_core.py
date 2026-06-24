from __future__ import annotations

from agent.swsd.context_packager import build_context_package
from agent.swsd.graph import ActionType
from agent.swsd.intent import classify_intent_with_planning_model
from agent.swsd.intent_policy import apply_swsd2_policy, apply_swsd3_policy
from agent.swsd.registry import get_workflow
from agent.swsd.transition import transition_for
from tools import pcb_model_runtime


def test_builtin_workflows_validate_and_transition():
    escape = get_workflow("pcb_escape_flow")
    reroute = get_workflow("pcb_reroute_flow")

    assert escape is not None
    assert reroute is not None
    assert escape.next_transition("select_bga", "select_target").to_state == "layer_assign_escape_order"
    assert reroute.next_transition("drc_loop", "drc_failed").action_type == ActionType.FALLBACK


def test_transition_policy_handles_jump_and_cancel():
    jump = transition_for("pcb_escape_flow", "layer_assign_escape_order", "change_target")
    cancel = transition_for("pcb_escape_flow", "routing", "cancel")

    assert jump.action_type == ActionType.USER_JUMP
    assert jump.to_state == "select_bga"
    assert cancel.action_type == ActionType.CANCEL
    assert cancel.to_state == "idle"


def test_context_packager_omits_raw_board_data():
    context = build_context_package(
        session_id="sess",
        workflow_state={
            "workflow_id": "pcb_escape_flow",
            "current_state": "routing",
            "state_payload": {
                "selectedBGA": "U22",
                "projectData": "(layout " + ("x" * 5000),
            },
        },
        checkpoints=[{"checkpoint_id": "c1", "state": "select_bga", "label": "BGA analysis"}],
        events=[{"event_type": "state_update", "from_state": "select_bga", "to_state": "routing"}],
    )

    assert "SWSD Workflow Context" in context
    assert "U22" in context
    assert "omitted" in context
    assert "xxxxx" not in context


def test_swsd_intent_llm_uses_tool_planning_stage(monkeypatch):
    captured = {}

    def fake_chat_completion_text(**kwargs):
        captured.update(kwargs)
        return '{"intent":"confirm_route","confidence":0.91}', {"stage": kwargs["stage"]}

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)

    result = classify_intent_with_planning_model("确认", current_state="layer_assign_escape_order", workflow_id="pcb_escape_flow")

    assert result["intent"] == "confirm_route"
    assert captured["stage"] == pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT


def test_swsd2_policy_normalizes_cancel_route_mode():
    result = apply_swsd2_policy(text="帮我 cancel。", flow_state="idle", candidate={"intent": "cancel", "route_mode": "pcb"})

    assert result.intent == "cancel"
    assert result.route_mode == "chat"


def test_swsd2_policy_keeps_unclear_inside_confirm_workflow():
    result = apply_swsd2_policy(text="嗯（不要调用工具）", flow_state="wait_confirm", candidate={"intent": "chat", "route_mode": "chat"})

    assert result.intent == "unclear"
    assert result.route_mode == "pcb"


def test_swsd2_policy_state_constrained_actions():
    assert apply_swsd2_policy(text="go", flow_state="wait_confirm").intent == "pcb_confirm_route"
    assert apply_swsd2_policy(text="arc + RL", flow_state="wait_router_type").intent == "pcb_followup"
    assert apply_swsd2_policy(text="选择 U27", flow_state="wait_selection").intent == "pcb_select_target"
    assert apply_swsd2_policy(text="嗯，只要扇出，不要reroute。", flow_state="idle").intent == "pcb_entry"
    assert apply_swsd2_policy(text="帮忙？", flow_state="idle").intent == "unclear"


def test_swsd3_execution_guard_blocks_idle_analysis_and_consultation():
    analysis = apply_swsd3_policy(
        text="帮我分析 reroute 利弊（仅说明）",
        flow_state="idle",
        candidate={"intent": "pcb_reroute_selected", "route_mode": "pcb"},
    )
    steps = apply_swsd3_policy(
        text="拆线重布一般分几步？",
        flow_state="idle",
        candidate={"intent": "pcb_reroute_selected", "route_mode": "pcb"},
    )
    consult = apply_swsd3_policy(
        text="什么是 BGA fanout？",
        flow_state="idle",
        candidate={"intent": "pcb_entry", "route_mode": "pcb"},
    )

    assert analysis.intent == "chat"
    assert analysis.route_mode == "chat"
    assert analysis.execution_intent in {"ANALYZE", "CONSULT"}
    assert analysis.allow_workflow_entry is False
    assert steps.intent == "chat"
    assert steps.execution_intent == "CONSULT"
    assert consult.intent == "chat"
    assert consult.execution_intent == "CONSULT"


def test_swsd3_execution_guard_allows_explicit_idle_execution():
    fanout = apply_swsd3_policy(text="对 U23 做 BGA fanout", flow_state="idle")
    reroute = apply_swsd3_policy(text="重新布线", flow_state="idle")

    assert fanout.intent == "pcb_entry"
    assert fanout.route_mode == "pcb"
    assert fanout.execution_intent == "EXECUTE"
    assert reroute.intent == "pcb_reroute_selected"
    assert reroute.route_mode == "pcb"
    assert reroute.execution_intent == "EXECUTE"


def test_swsd3_refines_selection_and_confirm_boundaries():
    selected = apply_swsd3_policy(text="U55", flow_state="wait_selection")
    weak = apply_swsd3_policy(
        text="好的",
        flow_state="wait_confirm",
        candidate={"intent": "pcb_confirm_route", "route_mode": "pcb"},
    )
    strong = apply_swsd3_policy(text="开始执行", flow_state="wait_confirm")

    assert selected.intent == "pcb_select_target"
    assert selected.reason == "swsd3_state_select_target_entity"
    assert weak.intent == "unclear"
    assert weak.route_mode == "pcb"
    assert weak.reason == "swsd3_weak_confirm_unclear"
    assert strong.intent == "pcb_confirm_route"
    assert strong.reason == "swsd3_strong_confirm_route"
