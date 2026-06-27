from types import SimpleNamespace

from agent.swsd.action_candidates import ActionCandidate, IntentCandidateSet
from agent.swsd.decision_policy import SWSDDecision
from agent.swsd.pcb_intent_agent_loop import IntentAgentLoopInput, IntentAgentLoopResult
from agent.swsd.workflow_controller import SWSDTurnDecision, SWSDTurnEvent, WebSocketWorkflowController, WorkflowActionPlan
from tools import pcb_model_runtime


class _DummyBridge:
    flow_idle = "idle"


class _DummyAdapter:
    _swsd_intent_model = None

    def __init__(self):
        self.mode = "chat"
        self._session_flow_states = {}

    def _reset_flow(self, session_id):
        pass

    def _set_flow_state(self, session_id, state):
        self._session_flow_states[session_id] = state

    def _set_session_mode(self, session_id, mode, lock_seconds=None):
        self.mode = mode

    def _session_mode(self, session_id):
        return self.mode


def _make_controller() -> WebSocketWorkflowController:
    return WebSocketWorkflowController(
        _DummyAdapter(),
        bridge=_DummyBridge(),
        escape_flow_id="pcb_escape_flow",
        reroute_flow_id="pcb_reroute_flow",
        route_mode_pcb="pcb",
        flow_wait_router_type="wait_router_type",
        flow_routing="routing",
        flow_reroute="reroute",
        intent_pcb_followup="pcb_followup",
        intent_pcb_reroute_selected="pcb_reroute_selected",
        intent_pcb_select_target="pcb_select_target",
        intent_pcb_confirm_route="pcb_confirm_route",
        confirm_re=None,
    )


def test_build_intent_loop_input_collects_workflow_context(monkeypatch):
    controller = _make_controller()
    event = SWSDTurnEvent(
        session_id="sess-1",
        project_id="proj-1",
        raw_user_text="?????",
        body={"role": "user", "content": "?????"},
        turn_options={"trace": True},
        inbound_fields={"routingResult": {"status": "ok"}},
        body_fanout_params={"LineWidth": 10},
    )
    hints = SimpleNamespace(as_dict=lambda: {"memory": ["m1"]})

    monkeypatch.setattr(controller, "active_workflow_state", lambda session_id: ("pcb_escape_flow", "review"))
    monkeypatch.setattr(
        controller,
        "_generate_state_action_candidates",
        lambda event, workflow_id, workflow_state: (
            ActionCandidate("rollback_checkpoint", 0.96, {"target_step": "previous"}, "rollback", "intent_model"),
        ),
    )
    monkeypatch.setattr(controller, "_resolve_experience_hints", lambda *args: hints)
    monkeypatch.setattr(controller, "_experience_action_candidates", lambda *args: ())
    monkeypatch.setattr(controller, "_explicit_protocol_action", lambda *args: "")
    monkeypatch.setattr(controller, "_allowed_actions", lambda *args: ["rollback_checkpoint", "chat"])

    loop_input = controller.build_intent_loop_input(event)

    assert isinstance(loop_input, IntentAgentLoopInput)
    assert loop_input.workflow_id == "pcb_escape_flow"
    assert loop_input.workflow_state == "review"
    assert loop_input.allowed_actions == ("rollback_checkpoint", "chat")
    assert loop_input.explicit_fields["body"]["content"] == "?????"
    assert loop_input.hints == {"memory": ["m1"]}
    assert loop_input.fallback_candidates[0].action == "rollback_checkpoint"


def test_plan_turn_normalizes_loop_result_into_workflow_action_plan(monkeypatch):
    controller = _make_controller()
    event = SWSDTurnEvent(session_id="sess-2", project_id="proj-2", raw_user_text="?????")
    candidate_set = IntentCandidateSet(
        workflow="pcb_escape_flow",
        current_state="review",
        candidate_actions=(
            ActionCandidate("rollback_checkpoint", 0.96, {"target_step": "previous"}, "rollback", "intent_model"),
        ),
        model_source="intent_model",
    )
    loop_result = IntentAgentLoopResult(
        candidate_set=candidate_set,
        policy=SWSDDecision(
            action="rollback_checkpoint",
            confidence=0.96,
            accepted_candidates=(candidate_set.candidate_actions[0],),
            reason="candidate_accepted",
        ),
        accepted=True,
        final_action="rollback_checkpoint",
        stage="confidence",
        votes=(True, True, True, True, True, True),
    )

    monkeypatch.setattr(controller, "build_intent_loop_input", lambda _event: IntentAgentLoopInput(
        user_text="?????",
        workflow_id="pcb_escape_flow",
        workflow_state="review",
        allowed_actions=("rollback_checkpoint", "chat"),
        session_id="sess-2",
        project_id="proj-2",
    ))
    monkeypatch.setattr(controller, "run_intent_loop", lambda _input: loop_result)

    plan = controller.plan_turn(event)

    assert plan.workflow_id == "pcb_escape_flow"
    assert plan.workflow_state == "review"
    assert plan.action == "rollback_checkpoint"
    assert plan.phase == "jump"
    assert plan.accepted is True
    assert plan.entities == {"target_step": "previous"}
    assert plan.stage == "confidence"
    assert plan.debug["policy_action"] == "rollback_checkpoint"


def test_plan_turn_marks_feedback_as_fallback(monkeypatch):
    controller = _make_controller()
    event = SWSDTurnEvent(session_id="sess-3", project_id="proj-3", raw_user_text="???")
    loop_result = IntentAgentLoopResult(
        candidate_set=IntentCandidateSet("pcb_escape_flow", "review", ()),
        policy=SWSDDecision(action="", confidence=0.0, requires_confirmation=True, reason="no_candidate_accepted"),
        accepted=False,
        feedback_reply="??????? fanout ????????????",
        stage="feedback",
        rejection_feedback=("no_candidate_accepted",),
    )

    monkeypatch.setattr(controller, "build_intent_loop_input", lambda _event: IntentAgentLoopInput(
        user_text="???",
        workflow_id="pcb_escape_flow",
        workflow_state="review",
        allowed_actions=("modify_params", "modify_order_lines", "chat"),
        session_id="sess-3",
        project_id="proj-3",
    ))
    monkeypatch.setattr(controller, "run_intent_loop", lambda _input: loop_result)

    plan = controller.plan_turn(event)

    assert plan.action == "clarify"
    assert plan.phase == "fallback"
    assert plan.accepted is False
    assert plan.immediate_reply.startswith("???")


def test_chat_plan_uses_reroute_runtime_and_disables_pcb_tools(monkeypatch):
    controller = _make_controller()
    event = SWSDTurnEvent(session_id="sess-chat", project_id="proj-chat", raw_user_text="????????")
    plan = WorkflowActionPlan(
        workflow_id="pcb_escape_flow",
        workflow_state="review",
        allowed_actions=("chat", "rollback_checkpoint"),
        action="chat",
        phase="chat",
        reason="confidence",
        accepted=True,
    )
    captured = {}

    def fake_resolve_model_runtime(stage):
        captured["stage"] = stage
        return {
            "model": "reroute-chat-model",
            "base_url": "https://example.test/v1",
            "api_key": "secret",
        }

    monkeypatch.setattr(pcb_model_runtime, "resolve_model_runtime", fake_resolve_model_runtime)

    class FakeAgent:
        def __init__(self, **kwargs):
            captured["agent_kwargs"] = kwargs

        def run_conversation(self, user_message, system_message=None, task_id=None):
            captured["user_message"] = user_message
            captured["system_message"] = system_message
            captured["task_id"] = task_id
            return {"final_response": "workflow explanation is stable."}

    import run_agent

    monkeypatch.setattr(run_agent, "AIAgent", FakeAgent)

    result = controller.dispatch_plan(event, plan)

    assert isinstance(result.decision, SWSDTurnDecision)
    assert result.decision.mode == "pcb"
    assert result.decision.immediate_reply == "workflow explanation is stable."
    assert captured["stage"] == pcb_model_runtime.STAGE_REROUTE
    assert captured["agent_kwargs"]["model"] == "reroute-chat-model"
    assert captured["agent_kwargs"]["disabled_toolsets"] == ["pcb"]
    assert "workflow_inner_chat" in captured["system_message"]


def test_chat_plan_sanitizes_unreliable_output(monkeypatch):
    controller = _make_controller()
    event = SWSDTurnEvent(session_id="sess-chat", project_id="proj-chat", raw_user_text="hello")
    plan = WorkflowActionPlan(
        workflow_id="",
        workflow_state="idle",
        allowed_actions=("chat",),
        action="chat",
        phase="chat",
        reason="confidence",
        accepted=True,
    )

    monkeypatch.setattr(controller, "_run_chat_agent", lambda _event, _plan: "????????????????????")

    result = controller.dispatch_plan(event, plan)

    assert result.decision.mode == "chat"
    assert result.decision.immediate_reply == "我暂时没有生成可靠回复，请稍后再试。"


def test_execute_reroute_plan_dispatches_skeleton_decision():
    controller = _make_controller()
    event = SWSDTurnEvent(session_id="sess-exec", project_id="proj-exec", raw_user_text="????")
    plan = WorkflowActionPlan(
        workflow_id="pcb_reroute_flow",
        workflow_state="idle",
        allowed_actions=("reroute_entry", "chat"),
        action="reroute_entry",
        phase="execute",
        reason="confidence",
        accepted=True,
    )

    result = controller.dispatch_plan(event, plan)

    assert result.plan is plan
    assert result.decision.reason == "reroute_rip_up_request"
    assert result.decision.mode == "pcb"
    assert result.decision.tool_call["name"] == "deleteTracesForRerouting"


def test_action_plan_normalization_maps_reroute_entry_to_reroute_workflow():
    controller = _make_controller()
    candidate = ActionCandidate("reroute_entry", 0.96, {}, "explicit reroute", "intent_model")
    candidate_set = IntentCandidateSet(
        workflow="pcb_escape_flow",
        current_state="idle",
        candidate_actions=(candidate,),
        model_source="intent_model",
    )
    loop_input = IntentAgentLoopInput(
        user_text="reroute",
        workflow_id="pcb_escape_flow",
        workflow_state="idle",
        allowed_actions=("reroute_entry", "chat"),
    )
    loop_result = IntentAgentLoopResult(
        candidate_set=candidate_set,
        policy=SWSDDecision(action="reroute_entry", confidence=0.96, accepted_candidates=(candidate,), reason="candidate_accepted"),
        accepted=True,
        final_action="reroute_entry",
        stage="confidence",
        votes=(True, True, True, True, True, True),
    )

    plan = controller._workflow_action_plan_from_loop_result(loop_input, loop_result)

    assert plan.action == "reroute_entry"
    assert plan.phase == "execute"
    assert plan.workflow_id == "pcb_reroute_flow"
    assert plan.workflow_state == "idle"


def test_execute_chain_routes_reroute_action_before_escape_workflow():
    controller = _make_controller()
    plan = WorkflowActionPlan(
        workflow_id="pcb_escape_flow",
        workflow_state="idle",
        allowed_actions=("reroute_entry", "chat"),
        action="reroute_entry",
        phase="execute",
        reason="confidence",
        accepted=True,
    )

    assert controller._execute_chain_for_plan(plan) == "reroute"
