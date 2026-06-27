from pathlib import Path

from agent.swsd.jump_intent_loop import JumpIntentLoopInput, WorkflowJumpPlan, run_jump_intent_loop
from agent.swsd.jump_intent_loop.models import JumpConfirmationResult
from agent.swsd.state_manager import WorkflowStateManager
from agent.swsd.workflow_controller import SWSDTurnEvent, WebSocketWorkflowController, WorkflowActionPlan


class _FakeJumpModel:
    def __init__(self, plans=None, confirmation=None):
        self.plans = list(plans or [])
        self.confirmation = confirmation or JumpConfirmationResult("confirm", "ok")
        self.confirm_replies = []

    def propose_jump_plan(self, request, prior, feedback=()):
        if self.plans:
            item = self.plans.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return {
            "workflow_id": "pcb_escape_flow",
            "from_state": request.workflow_state,
            "action": "rerun_fanout",
            "target_state": "layer_assign_escape_order",
            "confidence": 0.9,
            "entities": {},
            "reason": "default",
            "requires_clarification": False,
            "clarification": "",
        }

    def build_confirmation_reply(self, plan, request):
        self.confirm_replies.append(plan)
        return "我理解你想重新生成 fanout 参数。确认后继续；如果不是，请说明。"

    def judge_confirmation(self, *, user_text, pending_plan, rule_hint=""):
        return self.confirmation


class _Bridge:
    flow_idle = "idle"
    flow_wait_selection = "wait_selection"
    flow_wait_router_type = "wait_router_type"
    flow_wait_confirm = "wait_confirm"
    flow_routing = "routing"
    flow_reroute = "reroute"

    def escape_payload(self, session_id, extra=None):
        payload = {"legacyFlowState": "idle", "fanoutParams": {}}
        if extra:
            payload.update(extra)
        return payload

    def reroute_payload(self, session_id, extra=None):
        payload = {"legacyFlowState": "idle"}
        if extra:
            payload.update(extra)
        return payload

    def legacy_flow_for_workflow_state(self, workflow_id, state):
        if workflow_id == "pcb_reroute_flow":
            return "reroute" if state == "rip_up" else "idle"
        if state == "select_bga":
            return "wait_selection"
        if state == "layer_assign_escape_order":
            return "wait_router_type"
        if state == "param_review":
            return "wait_confirm"
        if state == "routing":
            return "routing"
        return "idle"


class _Adapter:
    _swsd_enabled = True
    _swsd_intent_model = None

    def __init__(self, jump_model=None):
        self._swsd_state = WorkflowStateManager(persist=False)
        self._swsd_jump_model = jump_model
        self._swsd_fanout_param_model = None
        self._session_flow_states = {}
        self._session_bga_selection = {}
        self._session_selected_targets = {}
        self._session_requested_bga_targets = {}
        self._session_router_types = {}
        self._session_route_algorithms = {}
        self._session_fanout_modules = {}
        self._session_fanout_params = {}
        self._session_board_summaries = {}
        self._session_layout_versions = {}
        self._session_active_params_versions = {}
        self._session_fanout_contexts = {}
        self.mode = "chat"

    def _reset_flow(self, session_id):
        self._session_flow_states[session_id] = "idle"

    def _set_session_mode(self, session_id, mode, lock_seconds=None):
        self.mode = mode

    def _set_flow_state(self, session_id, state):
        self._session_flow_states[session_id] = state

    def _session_mode(self, session_id):
        return self.mode

    def _swsd_update(self, session_id, workflow_id, state, payload, **kwargs):
        self._swsd_state.record_step(
            session_id,
            workflow_id,
            state=state,
            step_id=payload.get("step_id") or kwargs.get("intent") or "jump",
            payload=payload,
            event_type=kwargs.get("event_type", "workflow_action"),
            intent=kwargs.get("intent", ""),
            action_type=kwargs.get("action_type", "workflow_action"),
            checkpoint_label=kwargs.get("checkpoint_label"),
        )


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


def test_jump_loop_ignores_invalid_votes_and_accepts_five_valid(tmp_path):
    docs = tmp_path / "priors"
    docs.mkdir()
    (docs / "rerun.md").write_text(
        "# pcb_escape_flow rerun_fanout\n重新 fanout 改线宽 review layer_assign_escape_order rerun_fanout\n",
        encoding="utf-8",
    )
    model = _FakeJumpModel(
        plans=[
            {"bad": "shape"},
            {
                "workflow_id": "pcb_escape_flow",
                "from_state": "review",
                "action": "rerun_fanout",
                "target_state": "layer_assign_escape_order",
                "confidence": 0.9,
                "entities": {"constraints": {"LineWidth": 3}},
            },
        ]
        * 5
    )
    result = run_jump_intent_loop(
        JumpIntentLoopInput("重新fanout，要改线宽为3mil", "pcb_escape_flow", "review", {"workflows": {}}),
        model=model,
        docs_root=str(docs),
    )

    assert result.accepted is True
    assert result.invalid_rounds == 5
    assert result.plan.action == "rerun_fanout"
    assert result.plan.entities["constraints"] == {"LineWidth": 3}


def test_jump_loop_no_prior_returns_fixed_clarification(tmp_path):
    result = run_jump_intent_loop(
        JumpIntentLoopInput("完全无关的话", "pcb_escape_flow", "review", {"workflows": {}}),
        model=_FakeJumpModel(),
        docs_root=str(tmp_path),
    )

    assert result.accepted is False
    assert result.reason == "no_jump_prior"
    assert result.clarification.startswith("经检查这个跳转可能是不合规的")


def test_controller_jump_pending_confirm_commits_and_enters_fanout():
    model = _FakeJumpModel(
        plans=[
            {
                "workflow_id": "pcb_escape_flow",
                "from_state": "review",
                "action": "rerun_fanout",
                "target_state": "layer_assign_escape_order",
                "confidence": 0.9,
                "entities": {"constraints": {"LineWidth": 3}, "raw_constraints": {"line_width": "3mil"}},
                "reason": "用户要重新 fanout 并改线宽",
            }
        ]
        * 5,
        confirmation=JumpConfirmationResult("confirm", "用户确认"),
    )
    adapter = _Adapter(model)
    controller = _controller(adapter)
    adapter._swsd_state.update("s", "pcb_escape_flow", current_state="review", payload={"selectedBGA": "U5"})
    event = SWSDTurnEvent(session_id="s", raw_user_text="重新fanout，要改线宽为3mil")
    plan = WorkflowActionPlan(
        workflow_id="pcb_escape_flow",
        workflow_state="review",
        allowed_actions=("rerun_fanout", "chat"),
        action="rerun_fanout",
        phase="jump",
        reason="confidence",
        accepted=True,
    )

    first = controller.dispatch_plan(event, plan)
    assert first.decision.reason == "jump_confirmation_pending"
    assert "重新生成" in first.decision.immediate_reply

    second = controller.handle_turn(SWSDTurnEvent(session_id="s", raw_user_text="确认"))
    assert second.reason == "fanout_get_project_data"
    assert second.bootstrap_get_project is True
    assert adapter._session_fanout_params["s"]["constraints"]["LineWidth"] == 3
    state = adapter._swsd_state.load("s", "pcb_escape_flow")
    assert state["current_state"] == "select_bga"
    assert state["state_payload"]["jumpHistory"][-1]["action"] == "rerun_fanout"


def test_controller_pending_reject_reruns_jump_model():
    model = _FakeJumpModel(
        plans=[
            {
                "workflow_id": "pcb_escape_flow",
                "from_state": "review",
                "action": "rerun_fanout",
                "target_state": "layer_assign_escape_order",
                "confidence": 0.9,
            }
        ]
        * 5
        + [
            {
                "workflow_id": "pcb_reroute_flow",
                "from_state": "review",
                "action": "reroute_entry",
                "target_state": "rip_up",
                "confidence": 0.95,
            }
        ]
        * 5,
        confirmation=JumpConfirmationResult("reject", "用户改成拆线重布"),
    )
    adapter = _Adapter(model)
    controller = _controller(adapter)
    adapter._swsd_state.update("s", "pcb_escape_flow", current_state="review", payload={})
    plan = WorkflowActionPlan("pcb_escape_flow", "review", ("rerun_fanout",), "rerun_fanout", "jump", "confidence", True)

    controller.dispatch_plan(SWSDTurnEvent(session_id="s", raw_user_text="重新fanout"), plan)
    decision = controller.handle_turn(SWSDTurnEvent(session_id="s", raw_user_text="不是，我想拆线重布"))

    assert decision.reason == "jump_confirmation_pending"
    pending = controller._pending_jump_record("s")
    assert pending["plan"]["workflow_id"] == "pcb_reroute_flow"
    assert pending["plan"]["target_state"] == "rip_up"



def test_jump_retriever_ignores_index_and_prefers_rerun_prior(tmp_path):
    docs = tmp_path / "priors"
    docs.mkdir()
    (docs / "index.md").write_text("# index\n重新fanout review layer_assign_escape_order " * 20, encoding="utf-8")
    (docs / "disambiguation_rules.md").write_text("# disambiguation\n重新fanout review layer_assign_escape_order " * 20, encoding="utf-8")
    (docs / "pcb_escape_flow_rerun_clean_board.md").write_text("# rerun\npcb_escape_flow review rerun_fanout 重新fanout 改线宽 layer_assign_escape_order", encoding="utf-8")

    from agent.swsd.jump_intent_loop.retriever import retrieve_jump_prior

    prior = retrieve_jump_prior(
        user_text="重新fanout，要改线宽为3mil",
        workflow_id="pcb_escape_flow",
        workflow_state="review",
        docs_root=docs,
        candidate_action="rerun_fanout",
        entities={"constraints": {"LineWidth": 3}},
    )

    assert prior is not None
    assert Path(prior.path).name == "pcb_escape_flow_rerun_clean_board.md"
    assert all(item["name"] != "index.md" for item in prior.debug_scores)


def test_jump_retriever_allows_disambiguation_for_confirm_like_input(tmp_path):
    docs = tmp_path / "priors"
    docs.mkdir()
    (docs / "disambiguation_rules.md").write_text(("# disambiguation\npcb_escape_flow review 确认 继续 拒绝 confirm_import confirm_route " * 8), encoding="utf-8")

    from agent.swsd.jump_intent_loop.retriever import retrieve_jump_prior

    prior = retrieve_jump_prior(user_text="确认", workflow_id="pcb_escape_flow", workflow_state="review", docs_root=docs)

    assert prior is not None
    assert Path(prior.path).name == "disambiguation_rules.md"


def test_pseudo_expert_g_accepts_action_aliases_and_nested_target():
    from agent.swsd.jump_intent_loop.models import RetrievedJumpPrior
    from agent.swsd.jump_intent_loop.pseudo_expert_g import clean_jump_plan

    prior = RetrievedJumpPrior("p.md", "p", 0.8, "")
    plan, error = clean_jump_plan(
        {
            "workflowId": "pcb_escape_flow",
            "fromState": "review",
            "target": {"action": "change_target", "state": "select_bga"},
            "confidence": 0.8,
        },
        workflow_id="pcb_escape_flow",
        from_state="review",
        prior=prior,
        user_text="重新选择 U7 再 fanout",
    )

    assert error == ""
    assert plan.action == "change_target"
    assert plan.target_state == "select_bga"


def test_pseudo_expert_g_repairs_missing_action_with_debug():
    from agent.swsd.jump_intent_loop.models import RetrievedJumpPrior
    from agent.swsd.jump_intent_loop.pseudo_expert_g import clean_jump_plan

    prior = RetrievedJumpPrior("p.md", "p", 0.8, "")
    plan, error = clean_jump_plan(
        {
            "workflow_id": "pcb_escape_flow",
            "from_state": "review",
            "target_state": "layer_assign_escape_order",
            "confidence": 0.8,
        },
        workflow_id="pcb_escape_flow",
        from_state="review",
        prior=prior,
        user_text="重新fanout，要改线宽为3mil",
    )

    assert error == ""
    assert plan.action == "rerun_fanout"
    assert plan.debug["model_repaired"] is True


def test_pseudo_expert_g_rejects_illegal_action_after_repair_attempt():
    from agent.swsd.jump_intent_loop.models import RetrievedJumpPrior
    from agent.swsd.jump_intent_loop.pseudo_expert_g import clean_jump_plan

    prior = RetrievedJumpPrior("p.md", "p", 0.8, "")
    plan, error = clean_jump_plan(
        {"workflow_id": "pcb_escape_flow", "action": "delete_everything", "target_state": "review"},
        workflow_id="pcb_escape_flow",
        from_state="review",
        prior=prior,
        user_text="随便删掉",
    )

    assert plan is None
    assert error.startswith("unsupported jump action")



def test_jump_retriever_tiebreak_prefers_action_specific_prior(tmp_path):
    docs = tmp_path / "priors"
    docs.mkdir()
    shared = 'pcb_escape_flow review 重新fanout 改线宽 layer_assign_escape_order '
    (docs / "pcb_escape_flow_review.md").write_text("# review\n" + shared, encoding="utf-8")
    (docs / "pcb_escape_flow_rerun_clean_board.md").write_text("# rerun\n" + shared, encoding="utf-8")

    from agent.swsd.jump_intent_loop.retriever import retrieve_jump_prior

    prior = retrieve_jump_prior(
        user_text='重新fanout，要改线宽为3mil',
        workflow_id="pcb_escape_flow",
        workflow_state="review",
        docs_root=docs,
        candidate_action="rerun_fanout",
    )

    assert prior is not None
    assert Path(prior.path).name == "pcb_escape_flow_rerun_clean_board.md"


def test_swsd_jump_prompt_files_do_not_contain_mojibake_markers():
    files = [
        Path("agent/swsd/jump_intent_loop/tool_planning_chat_jump_model.py"),
        Path("agent/swsd/jump_intent_loop/pseudo_expert_g.py"),
        Path("agent/swsd/jump_intent_loop/retriever.py"),
    ]
    for file_path in files:
        text = file_path.read_text(encoding="utf-8")
        assert "?" * 3 not in text, file_path
        assert chr(0xfffd) not in text, file_path


def test_controller_fallback_phase_reuses_jump_chain():
    model = _FakeJumpModel(
        plans=[
            {
                "workflow_id": "pcb_escape_flow",
                "from_state": "review",
                "action": "rerun_fanout",
                "target_state": "layer_assign_escape_order",
                "confidence": 0.9,
                "entities": {"constraints": {"LineWidth": 3}},
                "reason": "fallback routed into jump",
            }
        ]
        * 5
    )
    adapter = _Adapter(model)
    controller = _controller(adapter)
    adapter._swsd_state.update("s", "pcb_escape_flow", current_state="review", payload={"selectedBGA": "U5"})
    plan = WorkflowActionPlan(
        workflow_id="pcb_escape_flow",
        workflow_state="review",
        allowed_actions=("rerun_fanout", "chat"),
        action="rerun_fanout",
        phase="fallback",
        reason="temporary_fallback_to_jump",
        accepted=True,
    )

    result = controller.dispatch_plan(SWSDTurnEvent(session_id="s", raw_user_text='重新fanout，要改线宽为3mil'), plan)

    assert result.decision is not None
    assert result.decision.reason == "jump_confirmation_pending"
    pending = controller._pending_jump_record("s")
    assert pending["plan"]["action"] == "rerun_fanout"
