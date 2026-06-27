"""Live smoke for SWSD entry -> real intent arbitration -> phase dispatch.

This script calls the real [tool-planning-chat-model]. It does not execute PCB
side effects; it stops at SWSDTurnDecision/tool-call request boundaries.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.swsd.pcb_intent_agent_loop import ToolPlanningChatIntentModel, file_trace_sink
from agent.swsd.state_manager import WorkflowStateManager
from agent.swsd.workflow_controller import SWSDTurnEvent, WebSocketWorkflowController
from tools import pcb_model_runtime


class _EntryBridge:
    flow_idle = "idle"
    flow_wait_selection = "wait_selection"
    flow_wait_router_type = "wait_router_type"
    flow_wait_confirm = "wait_confirm"
    flow_routing = "routing"
    flow_reroute = "reroute"

    def escape_payload(self, session_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"legacyFlowState": "idle", "fanoutParams": {}}
        if extra:
            payload.update(extra)
        return payload

    def reroute_payload(self, session_id: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = {"legacyFlowState": "idle"}
        if extra:
            payload.update(extra)
        return payload

    def legacy_flow_for_workflow_state(self, workflow_id: str, state: str) -> str:
        if workflow_id == "pcb_reroute_flow":
            return "reroute" if state in {"rip_up", "confirm", "reroute_llm", "report", "drc_loop", "import"} else "idle"
        if state == "select_bga":
            return "wait_selection"
        if state == "layer_assign_escape_order":
            return "wait_router_type"
        if state == "param_review":
            return "wait_confirm"
        if state == "routing":
            return "routing"
        return "idle"

    def swsd_state_from_legacy_flow(self, legacy_state: str) -> tuple[str, str]:
        if legacy_state == "reroute":
            return "pcb_reroute_flow", "rip_up"
        if legacy_state == "wait_selection":
            return "pcb_escape_flow", "select_bga"
        if legacy_state == "wait_router_type":
            return "pcb_escape_flow", "layer_assign_escape_order"
        if legacy_state == "wait_confirm":
            return "pcb_escape_flow", "param_review"
        if legacy_state == "routing":
            return "pcb_escape_flow", "routing"
        return "pcb_escape_flow", "idle"


class _EntryAdapter:
    _swsd_enabled = True
    _swsd_intent_model = None
    _swsd_jump_model = None
    _swsd_fanout_param_model = None
    _swsd_fallback_model = None

    def __init__(self) -> None:
        self._swsd_intent_model = ToolPlanningChatIntentModel(
            timeout_s=30.0,
            trace_sink=file_trace_sink(Path("tests/artifacts/swsd_entry_live_trace")),
        )
        self._swsd_state = WorkflowStateManager(persist=False)
        self._session_flow_states: dict[str, str] = {}
        self._session_bga_selection: dict[str, Any] = {}
        self._session_selected_targets: dict[str, str] = {}
        self._session_requested_bga_targets: dict[str, str] = {}
        self._session_router_types: dict[str, str] = {}
        self._session_route_algorithms: dict[str, str] = {}
        self._session_fanout_modules: dict[str, str] = {}
        self._session_fanout_params: dict[str, dict[str, Any]] = {}
        self._session_board_summaries: dict[str, Any] = {}
        self._session_layout_versions: dict[str, Any] = {}
        self._session_active_params_versions: dict[str, Any] = {}
        self._session_fanout_contexts: dict[str, Any] = {}
        self._session_fanout_param_plans: dict[str, Any] = {}
        self.mode = "chat"
        self.sent_messages: list[dict[str, Any]] = []

    def _session_mode(self, session_id: str) -> str:
        return self.mode

    def _reset_flow(self, session_id: str) -> None:
        self._session_flow_states[session_id] = "idle"

    def _set_session_mode(self, session_id: str, mode: str, lock_seconds: float | None = None) -> None:
        self.mode = mode

    def _set_flow_state(self, session_id: str, state: str) -> None:
        self._session_flow_states[session_id] = state

    def _swsd_update(self, session_id: str, workflow_id: str, state: str, payload: dict[str, Any], **kwargs: Any) -> None:
        self._swsd_state.record_step(
            session_id,
            workflow_id,
            state=state,
            step_id=payload.get("step_id") or kwargs.get("intent") or kwargs.get("action_type") or "entry_live",
            payload=payload,
            event_type=kwargs.get("event_type", "workflow_action"),
            intent=kwargs.get("intent", ""),
            action_type=kwargs.get("action_type", "workflow_action"),
            checkpoint_label=kwargs.get("checkpoint_label"),
        )

    def _resolve_pcb_experience(self, *args: Any, **kwargs: Any) -> None:
        return None

    def _extract_targeted_global_fanout_refdes(self, text: str) -> str:
        import re

        match = re.search(r"\b([A-Za-z]+\d+)\b", text or "")
        return match.group(1).upper() if match else ""

    def _is_pcb_concept_question_without_execution(self, text: str) -> bool:
        lowered = (text or "").lower()
        question_terms = ("why", "what", "how", "???", "???", "??", "??")
        execution_terms = ("??", "??", "??", "fanout", "????", "reroute")
        return any(term in lowered for term in question_terms) and not any(term in lowered for term in execution_terms)

    def _is_forced_global_fanout_command(self, text: str) -> bool:
        lowered = (text or "").lower()
        return "fanout" in lowered or "??" in lowered or "??" in lowered

    def _extract_complete_router_choice(self, session_id: str, text: str) -> str:
        import re

        if re.search(r"135\s*\+\s*RL", text or "", flags=re.IGNORECASE):
            return "135+RL"
        return ""

    def _extract_route_algorithm(self, text: str) -> str:
        return ""

    def _extract_fanout_module(self, text: str) -> str:
        return ""

    def _resolve_selected_label(self, session_id: str, text: str) -> str:
        return self._extract_targeted_global_fanout_refdes(text)

    def _selection_example(self, session_id: str) -> str:
        return "U5"

    def _router_choice_followup_prompt(self, session_id: str) -> str:
        return '请补充 routerType，例如 135 + RL。'

    def _router_type_prompt(self, session_id: str) -> str:
        return '请补充 routerType，例如 135 + RL。'

    def _refresh_fanout_params_draft(self, session_id: str, user_text: str = "") -> dict[str, Any]:
        draft = dict(self._session_fanout_params.get(session_id) or {})
        self._session_fanout_params[session_id] = draft
        return draft

    async def send(self, *args: Any, **kwargs: Any) -> None:
        self.sent_messages.append({"args": args, "kwargs": kwargs})


class _SmokeController(WebSocketWorkflowController):
    def _run_chat_agent(self, event: SWSDTurnEvent, plan) -> str:  # type: ignore[override]
        return "[entry-live-smoke] chat branch reached; PCB tools were not called."


def _controller(adapter: _EntryAdapter) -> _SmokeController:
    return _SmokeController(
        adapter,
        bridge=_EntryBridge(),
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


def _preflight() -> dict[str, Any]:
    runtime = pcb_model_runtime.resolve_model_runtime(pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT)
    public_runtime = {"model": runtime.get("model"), "base_url": runtime.get("base_url"), "api_key": "***" if runtime.get("api_key") else ""}
    try:
        content, meta = pcb_model_runtime.chat_completion_text(
            stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
            messages=[
                {"role": "system", "content": "Return only JSON. No Markdown. No explanation."},
                {"role": "user", "content": '请只返回 {"ok": true}'},
            ],
            timeout_s=30,
            max_tokens=64,
            temperature=0,
            top_p=1,
            stream_until_json=True,
        )
        return {"ok": True, "runtime": public_runtime, "content": content, "meta": {k: meta.get(k) for k in ("model", "base_url", "stream_finish_reason", "response_id")}}
    except Exception as exc:
        return {"ok": False, "runtime": public_runtime, "error": str(exc)}


def _decision_dict(decision: Any) -> dict[str, Any]:
    return {
        "mode": decision.mode,
        "reason": decision.reason,
        "intent": decision.intent,
        "immediate_reply": decision.immediate_reply,
        "bootstrap_get_project": decision.bootstrap_get_project,
        "tool_call": decision.tool_call,
    }


def _plan_dict(plan: Any) -> dict[str, Any]:
    return {
        "workflow_id": plan.workflow_id,
        "workflow_state": plan.workflow_state,
        "allowed_actions": list(plan.allowed_actions),
        "action": plan.action,
        "phase": plan.phase,
        "reason": plan.reason,
        "accepted": plan.accepted,
        "entities": plan.entities,
        "immediate_reply": plan.immediate_reply,
        "stage": plan.stage,
        "rejection_feedback": list(plan.rejection_feedback),
        "votes": list(plan.votes),
        "debug": plan.debug,
    }


def _state_summary(adapter: _EntryAdapter, session_id: str) -> dict[str, Any]:
    rows = {}
    for workflow_id in ("pcb_escape_flow", "pcb_reroute_flow"):
        state = adapter._swsd_state.load(session_id, workflow_id) or {}
        payload = state.get("state_payload") if isinstance(state.get("state_payload"), dict) else {}
        rows[workflow_id] = {
            "current_state": state.get("current_state"),
            "payload_keys": sorted(payload.keys()),
            "pending_jump": bool(payload.get("pendingJump")),
            "project_data": payload.get("projectData"),
            "target_bgas": payload.get("targetBGAs"),
            "reroute_files": payload.get("rerouteFiles"),
        }
    return rows


def _seed_state(adapter: _EntryAdapter, session_id: str, workflow_id: str, state: str, payload: dict[str, Any] | None = None) -> None:
    if state and state != "idle":
        adapter._swsd_state.update(session_id, workflow_id, current_state=state, payload=payload or {}, merge=False)


def _run_case(case: dict[str, Any]) -> dict[str, Any]:
    adapter = _EntryAdapter()
    controller = _controller(adapter)
    session_id = str(case.get("session_id") or f"entry-live-{case['name']}")
    workflow_id = str(case.get("workflow_id") or "pcb_escape_flow")
    workflow_state = str(case.get("workflow_state") or "idle")
    print(f"[case:start] {case['name']} {workflow_id}/{workflow_state}", flush=True)
    _seed_state(adapter, session_id, workflow_id, workflow_state, dict(case.get("state_payload") or {}))
    event = SWSDTurnEvent(session_id=session_id, project_id="entry-live-project", raw_user_text=str(case.get("user_text") or ""))
    print(f"[case:plan] {case['name']} planning", flush=True)
    plan = controller.plan_turn(event)
    print(f"[case:plan-done] {case['name']} action={plan.action} phase={plan.phase} reason={plan.reason}", flush=True)
    dispatched = controller.dispatch_plan(event, plan)
    print(f"[case:dispatch-done] {case['name']} decision={getattr(dispatched.decision, 'intent', None)}", flush=True)
    decision = dispatched.decision or controller.handle_turn(event)
    print(f"[case:done] {case['name']} intent={decision.intent} mode={decision.mode}", flush=True)
    return {
        "name": case["name"],
        "input": case.get("user_text"),
        "initial": {"workflow_id": workflow_id, "workflow_state": workflow_state},
        "plan": _plan_dict(plan),
        "decision": _decision_dict(decision),
        "state_summary": _state_summary(adapter, session_id),
        "legacy_flow_state": adapter._session_flow_states.get(session_id),
        "session_mode": adapter.mode,
    }


def main() -> int:
    print("[live-smoke] preflight", flush=True)
    preflight = _preflight()
    print(f"[live-smoke] preflight ok={preflight.get('ok')} model={preflight.get('runtime', {}).get('model')}", flush=True)
    if not preflight["ok"]:
        print(json.dumps({"runtime_config_error": preflight}, ensure_ascii=False, indent=2, default=str))
        return 0
    cases = [
        {"name": "chat_plain", "workflow_id": "pcb_escape_flow", "workflow_state": "idle", "user_text": '这个布线为什么这样走？'},
        {"name": "fanout_entry", "workflow_id": "pcb_escape_flow", "workflow_state": "idle", "user_text": '给 U5 做 fanout，线宽3mil，线距3mil'},
        {"name": "fanout_param_modify", "workflow_id": "pcb_escape_flow", "workflow_state": "param_review", "state_payload": {"selectedBGA": "U5"}, "user_text": '线宽改成4mil，重新生成'},
        {"name": "reroute_entry", "workflow_id": "pcb_escape_flow", "workflow_state": "idle", "user_text": '拆线重布'},
        {"name": "jump_rerun_fanout", "workflow_id": "pcb_escape_flow", "workflow_state": "review", "state_payload": {"selectedBGA": "U5"}, "user_text": '重新fanout，要改线宽为3mil'},
        {"name": "fallback_to_jump_like", "workflow_id": "pcb_escape_flow", "workflow_state": "review", "state_payload": {"selectedBGA": "U5"}, "user_text": '改一下线宽然后重新来'},
    ]
    results = []
    for case in cases:
        results.append(_run_case(case))
        print(f"[live-smoke] completed {case['name']}", flush=True)
    print(json.dumps({"preflight": preflight, "cases": results}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
