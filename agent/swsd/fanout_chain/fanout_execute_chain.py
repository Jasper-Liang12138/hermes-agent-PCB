"""SWSD-owned fanout execute chain skeleton."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .fallback_expert_loop import run_fallback_expert_loop
from .fanout_param_loop import FanoutParamPlan, run_fanout_param_loop

if TYPE_CHECKING:
    from agent.swsd.workflow_controller import SWSDTurnDecision, SWSDTurnEvent, WorkflowActionPlan


@dataclass(frozen=True)
class FanoutChainResult:
    decision: "SWSDTurnDecision"
    fanout_param_plan: FanoutParamPlan | None = None


class FanoutExecuteChain:
    def __init__(self, controller: Any) -> None:
        self.controller = controller
        self.adapter = controller.adapter
        self.bridge = controller.bridge
        self.state = getattr(self.adapter, "_swsd_state", None)
        self.workflow_id = controller.escape_flow_id

    def handle(self, event: "SWSDTurnEvent", plan: "WorkflowActionPlan") -> FanoutChainResult:
        current_state = self._canonical_state(plan.workflow_state)
        action = plan.action or "pcb_entry"
        validation = self._validate_entry(current_state, action)
        if not validation.valid:
            return self._invalid_entry(event, current_state, validation.reason)

        self.adapter._reset_flow(event.session_id)
        self.adapter._set_session_mode(event.session_id, self.controller.route_mode_pcb)
        if current_state in {"idle", "select_bga"}:
            self.adapter._set_flow_state(event.session_id, self.bridge.flow_wait_selection)

        param_plan = run_fanout_param_loop(
            event.raw_user_text or "",
            model=getattr(self.adapter, "_swsd_fanout_param_model", None),
        )
        self._remember_param_plan(event, param_plan)
        self._record_get_project_request(event, plan, param_plan)

        # 第一阶段采用方案 A：SWSD Controller 决定进入 getProjectData，WebSocket 只做协议发送。
        decision = self._turn_decision_cls()(
            mode=self.controller.route_mode_pcb,
            reason="fanout_get_project_data",
            intent="pcb_entry",
            bootstrap_get_project=True,
        )
        return FanoutChainResult(decision=decision, fanout_param_plan=param_plan)

    @staticmethod
    def _turn_decision_cls() -> Any:
        from agent.swsd.workflow_controller import SWSDTurnDecision

        return SWSDTurnDecision

    def _validate_entry(self, current_state: str, action: str) -> Any:
        if self.state is not None and hasattr(self.state, "validate_entry"):
            return self.state.validate_entry(self.workflow_id, current_state, action)
        allowed = set(self.controller._allowed_actions(self.workflow_id, current_state, None))
        valid = action in allowed or (action == "pcb_entry" and current_state in {"idle", "select_bga"})
        return type("Validation", (), {"valid": valid, "reason": "action is not allowed", "allowed_actions": tuple(allowed)})()

    def _invalid_entry(self, event: "SWSDTurnEvent", current_state: str, reason: str) -> FanoutChainResult:
        fallback = run_fallback_expert_loop(
            user_text=event.raw_user_text or "",
            invalid_reason=reason or "fanout_entry_invalid",
            current_state=current_state,
            model=getattr(self.adapter, "_swsd_fallback_model", None),
        )
        return FanoutChainResult(
            decision=self._turn_decision_cls()(
                mode=self.controller.route_mode_pcb,
                reason=fallback.reason,
                intent="unclear",
                immediate_reply=fallback.reply,
                bootstrap_get_project=False,
            )
        )

    def _remember_param_plan(self, event: "SWSDTurnEvent", param_plan: FanoutParamPlan) -> None:
        session_id = event.session_id
        payload = param_plan.as_payload()
        if not hasattr(self.adapter, "_session_fanout_param_plans"):
            self.adapter._session_fanout_param_plans = {}
        self.adapter._session_fanout_param_plans[session_id] = payload
        if param_plan.target_bgas:
            first = param_plan.target_bgas[0].normalized
            self.adapter._session_requested_bga_targets[session_id] = first
            self.adapter._session_selected_targets[session_id] = first
        if param_plan.constraints.normalized:
            draft = dict(getattr(self.adapter, "_session_fanout_params", {}).get(session_id, {}) or {})
            constraints = dict(draft.get("constraints") or {})
            constraints.update(param_plan.constraints.normalized)
            draft["constraints"] = constraints
            self.adapter._session_fanout_params[session_id] = draft

    def _record_get_project_request(self, event: "SWSDTurnEvent", plan: "WorkflowActionPlan", param_plan: FanoutParamPlan) -> None:
        if self.state is None or not hasattr(self.state, "record_step"):
            return
        payload = self.bridge.escape_payload(
            event.session_id,
            {
                "requestedAction": plan.action,
                "lastActionReason": plan.reason,
                "fanoutParamPlan": param_plan.as_payload(),
                "targetBGAs": [target.normalized for target in param_plan.target_bgas],
                "projectData": {
                    "relative_path": "",
                    "absolute_path": "",
                    "status": "requested",
                    "source": "getProjectData",
                },
                "createdAt": time.time(),
            },
        )
        self.state.record_step(
            event.session_id,
            self.workflow_id,
            state="select_bga",
            step_id="get_project_data",
            payload=payload,
            event_type="workflow_action",
            intent="getProjectData",
            action_type="tool_call_request",
            checkpoint_label="getProjectData requested",
        )

    @staticmethod
    def _canonical_state(state: str) -> str:
        if state in {"layer_assign", "escape_order"}:
            return "layer_assign_escape_order"
        return str(state or "idle")