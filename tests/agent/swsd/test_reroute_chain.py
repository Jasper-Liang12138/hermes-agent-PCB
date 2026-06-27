import asyncio

from agent.swsd.reroute_chain.markdown_report import build_reroute_markdown_report
from agent.swsd.state_manager import WorkflowStateManager
from agent.swsd.workflow_controller import SWSDTurnEvent, WebSocketWorkflowController, WorkflowActionPlan


class _Bridge:
    flow_idle = "idle"
    flow_reroute = "reroute"

    def reroute_payload(self, session_id, extra=None):
        payload = {"legacyFlowState": "idle"}
        if extra:
            payload.update(extra)
        return payload

    def escape_payload(self, session_id, extra=None):
        payload = {"legacyFlowState": "idle"}
        if extra:
            payload.update(extra)
        return payload


class _Adapter:
    _swsd_intent_model = None
    _swsd_fallback_model = None

    def __init__(self):
        self._swsd_state = WorkflowStateManager(persist=False)
        self._session_flow_states = {}
        self._session_bga_selection = {}
        self._session_selected_targets = {}
        self._session_requested_bga_targets = {}
        self._session_router_types = {}
        self._session_route_algorithms = {}
        self._session_fanout_modules = {}
        self._session_fanout_params = {}
        self._session_board_summaries = {}
        self._session_fanout_contexts = {}
        self._session_layout_versions = {}
        self._session_active_params_versions = {}
        self.sent = []
        self.mode = "chat"
        self.flow = "idle"

    def _reset_flow(self, session_id):
        self.flow = "idle"
        self._session_flow_states[session_id] = "idle"

    def _set_session_mode(self, session_id, mode, lock_seconds=None):
        self.mode = mode

    def _set_flow_state(self, session_id, state):
        self.flow = state
        self._session_flow_states[session_id] = state

    def _session_mode(self, session_id):
        return self.mode

    async def send(self, chat_id, content, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata or {}})

    async def send_tool_call(self, session_id, call_id, tool_name, arguments, timeout=360.0):
        self.sent.append({"tool": tool_name, "arguments": arguments, "call_id": call_id})
        return {"ok": True}


class _NoopIntentModel:
    pass


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


def test_execute_reroute_entry_records_rip_up_and_requests_delete_tool():
    adapter = _Adapter()
    controller = _controller(adapter)
    event = SWSDTurnEvent(session_id="s", raw_user_text="拆线重布")
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

    assert result.decision.tool_call["name"] == "deleteTracesForRerouting"
    state = adapter._swsd_state.load("s", "pcb_reroute_flow")
    assert state["current_state"] == "rip_up"
    assert state["state_payload"]["step_id"] == "rip_up_requested"
    assert state["state_payload"]["projectData"]["status"] == "requested"


def test_delete_result_stops_at_confirm_and_sends_user_confirmation():
    adapter = _Adapter()
    controller = _controller(adapter)
    chain = __import__("agent.swsd.reroute_chain.reroute_execute_chain", fromlist=["RerouteExecuteChain"]).RerouteExecuteChain(controller)

    asyncio.get_event_loop().run_until_complete(
        chain.handle_delete_result(
            {"sessionId": "s", "projectid": "p", "body": {"sessionId": "s", "projectid": "p"}},
            {"missing_routes": [{"net_name": "N1"}], "projectDataFilePath": r"F:\\board\\after_delete.kicad_pcb"},
        )
    )

    state = adapter._swsd_state.load("s", "pcb_reroute_flow")
    assert state["current_state"] == "confirm"
    assert state["state_payload"]["step_id"] == "rip_up_complete"
    assert "确认是否继续" in adapter.sent[-1]["content"]


def test_confirm_reroute_returns_backend_reroute_tool_call():
    adapter = _Adapter()
    controller = _controller(adapter)
    adapter._swsd_state.update("s", "pcb_reroute_flow", current_state="confirm", payload={})
    event = SWSDTurnEvent(session_id="s", raw_user_text="确认")
    plan = WorkflowActionPlan(
        workflow_id="pcb_reroute_flow",
        workflow_state="confirm",
        allowed_actions=("confirm_reroute", "reroute_again", "chat"),
        action="confirm_reroute",
        phase="execute",
        reason="confidence",
        accepted=True,
    )

    result = controller.dispatch_plan(event, plan)

    assert result.decision.tool_call["name"] == "reroute"
    state = adapter._swsd_state.load("s", "pcb_reroute_flow")
    assert state["current_state"] == "reroute_llm"


def test_reroute_result_sends_full_markdown_in_content_without_pcb_fields():
    adapter = _Adapter()
    controller = _controller(adapter)
    chain = __import__("agent.swsd.reroute_chain.reroute_execute_chain", fromlist=["RerouteExecuteChain"]).RerouteExecuteChain(controller)
    payload = {
        "rerouteResult": {"status": "local_completion_passed", "drcPassed": True, "selectedNets": ["N1"], "importLinesFilePath": r"F:\\out\\line.out"},
        "checkReport": {"passed": True, "checks": [{"name": "clearance", "passed": True, "message": "ok"}]},
        "report": "原始 txt 报告全文\n第二行",
    }

    asyncio.get_event_loop().run_until_complete(
        chain.handle_reroute_result({"sessionId": "s", "body": {"sessionId": "s"}}, payload)
    )

    msg = adapter.sent[-1]
    assert msg["metadata"] == {"is_final": True}
    assert msg["content"].startswith("# 拆线重布报告")
    assert "原始 txt 报告全文" in msg["content"]
    assert "| clearance | 通过 | ok |" in msg["content"]
    state = adapter._swsd_state.load("s", "pcb_reroute_flow")
    assert state["current_state"] == "report"
    assert state["state_payload"]["rerouteFiles"]["importLinesFilePath"] == r"F:\\out\\line.out"


def test_reject_import_does_not_resend_report():
    adapter = _Adapter()
    controller = _controller(adapter)
    event = SWSDTurnEvent(session_id="s", raw_user_text="不导入")
    plan = WorkflowActionPlan(
        workflow_id="pcb_reroute_flow",
        workflow_state="report",
        allowed_actions=("reject_import", "confirm_import"),
        action="reject_import",
        phase="execute",
        reason="confidence",
        accepted=True,
    )

    result = controller.dispatch_plan(event, plan)

    assert result.decision.immediate_reply == "已取消导入。你想重新拆线重布，还是切换到其他 PCB 流程？"
    assert "# 拆线重布报告" not in result.decision.immediate_reply


def test_markdown_report_keeps_full_text():
    markdown = build_reroute_markdown_report({"report": "完整内容 A\n完整内容 B", "checkReport": {"passed": False}})

    assert markdown.startswith("# 拆线重布报告")
    assert "完整内容 A\n完整内容 B" in markdown

def test_confirm_import_returns_import_lines_tool_call():
    adapter = _Adapter()
    controller = _controller(adapter)
    adapter._swsd_state.update(
        "s",
        "pcb_reroute_flow",
        current_state="report",
        payload={"rerouteFiles": {"importLinesFilePath": r"F:\\out\\line.out"}},
    )
    event = SWSDTurnEvent(session_id="s", raw_user_text="确认导入")
    plan = WorkflowActionPlan(
        workflow_id="pcb_reroute_flow",
        workflow_state="report",
        allowed_actions=("confirm_import", "reject_import"),
        action="confirm_import",
        phase="jump",
        reason="confidence",
        accepted=True,
    )

    result = controller.dispatch_plan(event, plan)

    assert result.decision.tool_call["name"] == "importLines"
    assert result.decision.tool_call["arguments"]["filePath"] == r"F:\\out\\line.out"
    state = adapter._swsd_state.load("s", "pcb_reroute_flow")
    assert state["current_state"] == "import"
