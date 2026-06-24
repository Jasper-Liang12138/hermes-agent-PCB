from agent.swsd.fanout_chain.fanout_param_loop import run_fanout_param_loop
from agent.swsd.state_manager import WorkflowStateManager
from agent.swsd.workflow_controller import SWSDTurnEvent, WebSocketWorkflowController, WorkflowActionPlan


class _Bridge:
    flow_idle = "idle"
    flow_wait_selection = "wait_selection"

    def escape_payload(self, session_id, extra=None):
        payload = {"legacyFlowState": "idle", "fanoutParams": {}}
        if extra:
            payload.update(extra)
        return payload


class _Adapter:
    _swsd_intent_model = None

    def __init__(self):
        self._swsd_state = WorkflowStateManager(persist=False)
        self._session_flow_states = {}
        self._session_requested_bga_targets = {}
        self._session_selected_targets = {}
        self._session_fanout_params = {}
        self.mode = "chat"
        self.flow = "idle"

    def _reset_flow(self, session_id):
        self.flow = "idle"

    def _set_session_mode(self, session_id, mode, lock_seconds=None):
        self.mode = mode

    def _set_flow_state(self, session_id, state):
        self.flow = state
        self._session_flow_states[session_id] = state

    def _session_mode(self, session_id):
        return self.mode


def _controller(adapter):
    return WebSocketWorkflowController(
        adapter,
        bridge=_Bridge(),
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


def test_fanout_param_loop_supports_multiple_bgas_and_constraints():
    plan = run_fanout_param_loop("route U5 and U7, line width 3mil line spacing 3mil", max_rounds=1)

    assert [target.normalized for target in plan.target_bgas] == ["U5", "U7"]
    assert plan.constraints.normalized == {"LineWidth": 3, "LineSpacing": 3}
    assert plan.jump_to == "layer_assign_escape_order"
    assert plan.skip_select_bga is True


def test_execute_fanout_chain_records_get_project_request_and_bootstraps():
    adapter = _Adapter()
    controller = _controller(adapter)
    event = SWSDTurnEvent(session_id="s", raw_user_text="route U5 line width 3mil")
    plan = WorkflowActionPlan(
        workflow_id="pcb_escape_flow",
        workflow_state="idle",
        allowed_actions=("pcb_entry", "chat"),
        action="pcb_entry",
        phase="execute",
        reason="confidence",
        accepted=True,
    )

    result = controller.dispatch_plan(event, plan)

    assert result.decision.bootstrap_get_project is True
    assert result.decision.reason == "fanout_get_project_data"
    state = adapter._swsd_state.load("s", "pcb_escape_flow")
    assert state["current_state"] == "select_bga"
    payload = state["state_payload"]
    assert payload["step_id"] == "get_project_data"
    assert payload["projectData"]["status"] == "requested"
    assert payload["targetBGAs"] == ["U5"]
    assert adapter._session_requested_bga_targets["s"] == "U5"