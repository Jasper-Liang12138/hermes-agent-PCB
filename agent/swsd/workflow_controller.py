"""Workflow control layer for SWSD-backed WebSocket routing."""

from __future__ import annotations

import re
from typing import Any, Dict, Optional


class WebSocketWorkflowController:
    def __init__(
        self,
        adapter,
        *,
        bridge,
        escape_flow_id: str,
        reroute_flow_id: str,
        route_mode_pcb: str,
        flow_wait_router_type: str,
        flow_routing: str,
        flow_reroute: str,
        intent_pcb_followup: str,
        intent_pcb_reroute_selected: str,
        intent_pcb_select_target: str,
        intent_pcb_confirm_route: str,
        confirm_re,
    ) -> None:
        self.adapter = adapter
        self.bridge = bridge
        self.escape_flow_id = escape_flow_id
        self.reroute_flow_id = reroute_flow_id
        self.route_mode_pcb = route_mode_pcb
        self.flow_wait_router_type = flow_wait_router_type
        self.flow_routing = flow_routing
        self.flow_reroute = flow_reroute
        self.intent_pcb_followup = intent_pcb_followup
        self.intent_pcb_reroute_selected = intent_pcb_reroute_selected
        self.intent_pcb_select_target = intent_pcb_select_target
        self.intent_pcb_confirm_route = intent_pcb_confirm_route
        self.confirm_re = confirm_re

    def active_workflow_state(self, session_id: str) -> tuple[str, str]:
        if self.adapter._swsd_enabled:
            reroute = self.adapter._swsd_state.load(session_id, self.reroute_flow_id) or {}
            reroute_state = str(reroute.get("current_state") or "")
            if reroute_state and reroute_state != "idle":
                return self.reroute_flow_id, reroute_state
            escape = self.adapter._swsd_state.load(session_id, self.escape_flow_id) or {}
            escape_state = str(escape.get("current_state") or "")
            if escape_state and escape_state != "idle":
                return self.escape_flow_id, escape_state
        return self.bridge.swsd_state_from_legacy_flow(
            self.adapter._session_flow_states.get(session_id, self.bridge.flow_idle)
        )

    def jump_to_checkpoint(
        self,
        session_id: str,
        workflow_id: str,
        checkpoint_id: str | None = None,
    ) -> bool:
        if not self.adapter._swsd_enabled:
            return False
        restored = self.adapter._swsd_state.rollback(session_id, workflow_id, checkpoint_id=checkpoint_id)
        if not restored:
            return False
        current_state = str(restored.get("current_state") or "")
        self.bridge.restore_workflow_context_from_state(session_id, workflow_id)
        self.adapter._set_session_mode(session_id, self.route_mode_pcb)
        self.adapter._set_flow_state(
            session_id,
            self.bridge.legacy_flow_for_workflow_state(workflow_id, current_state),
        )
        return True

    def handle_jump(self, session_id: str, workflow_id: str, workflow_state: str, text: str) -> Optional[Dict[str, Any]]:
        if workflow_id == self.reroute_flow_id and workflow_state in {"report", "import", "drc_loop"}:
            if re.search(r"再\s*(?:reroute|重布|拆线重布)|重新\s*(?:reroute|重布|拆线重布)", text, flags=re.IGNORECASE):
                self.adapter._set_session_mode(session_id, self.route_mode_pcb)
                self.adapter._set_flow_state(session_id, self.flow_reroute)
                self.adapter._swsd_update(
                    session_id,
                    self.reroute_flow_id,
                    "rip_up",
                    self.bridge.reroute_payload(session_id, {"reentryText": text}),
                    event_type="user_jump",
                    intent="reroute_again",
                    action_type="user_jump",
                    checkpoint_label="reroute reentry",
                )
                return {
                    "mode": self.route_mode_pcb,
                    "reason": "reroute_reentry",
                    "intent": self.intent_pcb_reroute_selected,
                    "bootstrap_get_project": False,
                }
            if re.search(r"回到上一步|上一步|rollback|回退", text, flags=re.IGNORECASE):
                if self.jump_to_checkpoint(session_id, self.reroute_flow_id):
                    return {
                        "mode": self.route_mode_pcb,
                        "immediate_reply": "已恢复到上一个拆线重布检查点，请继续。",
                        "reason": "reroute_rollback",
                        "intent": self.intent_pcb_followup,
                    }
            return None

        if workflow_id != self.escape_flow_id:
            return None

        if workflow_state in {"layer_assign", "escape_order", "routing", "review", "import"} and re.search(
            r"回到上一步|上一步|rollback|回退", text, flags=re.IGNORECASE
        ):
            if self.jump_to_checkpoint(session_id, self.escape_flow_id):
                return {
                    "mode": self.route_mode_pcb,
                    "immediate_reply": "已恢复到上一个全局 fanout 检查点，请继续。",
                    "reason": "escape_rollback",
                    "intent": self.intent_pcb_followup,
                }
        if workflow_state not in {"routing", "review", "import"}:
            return None

        selected_label = self.adapter._resolve_selected_label(session_id, text)
        if selected_label:
            self.adapter._session_selected_targets[session_id] = selected_label
            self.adapter._session_requested_bga_targets[session_id] = selected_label
            self.adapter._session_fanout_params.pop(session_id, None)
            self.adapter._session_router_types.pop(session_id, None)
            self.adapter._session_route_algorithms.pop(session_id, None)
            self.adapter._session_fanout_modules.pop(session_id, None)
            self.adapter._set_session_mode(session_id, self.route_mode_pcb)
            self.adapter._set_flow_state(session_id, self.flow_wait_router_type)
            self.adapter._swsd_update(
                session_id,
                self.escape_flow_id,
                "layer_assign",
                self.bridge.escape_payload(session_id, {"jumpText": text}),
                event_type="user_jump",
                intent="change_target",
                action_type="user_jump",
                checkpoint_label="fanout change target",
            )
            return {
                "mode": self.route_mode_pcb,
                "immediate_reply": self.adapter._router_type_prompt(session_id),
                "reason": "escape_change_target",
                "intent": self.intent_pcb_select_target,
            }

        router_choice = self.adapter._extract_complete_router_choice(session_id, text)
        has_param_adjustment = bool(
            router_choice
            or self.adapter._extract_route_algorithm(text)
            or self.adapter._extract_fanout_module(text)
        )
        if has_param_adjustment:
            self.adapter._session_fanout_params.pop(session_id, None)
            self.adapter._set_session_mode(session_id, self.route_mode_pcb)
            self.adapter._set_flow_state(session_id, self.flow_wait_router_type)
            self.adapter._swsd_update(
                session_id,
                self.escape_flow_id,
                "layer_assign",
                self.bridge.escape_payload(session_id, {"jumpText": text}),
                event_type="user_jump",
                intent="modify_params",
                action_type="user_jump",
                checkpoint_label="fanout modify params",
            )
            if router_choice:
                return {
                    "mode": self.route_mode_pcb,
                    "reason": "router_type_step",
                    "intent": self.intent_pcb_followup,
                }
            return {
                "mode": self.route_mode_pcb,
                "immediate_reply": self.adapter._router_choice_followup_prompt(session_id),
                "reason": "escape_modify_params",
                "intent": self.intent_pcb_followup,
            }

        if workflow_state in {"review", "import"} and self.confirm_re.search(text):
            if not self.adapter._session_fanout_params.get(session_id):
                return {
                    "mode": self.route_mode_pcb,
                    "immediate_reply": "缺少已确认的逃逸参数配置，请先重新生成逃逸参数。",
                    "reason": "confirm_without_fanout_params",
                    "intent": self.intent_pcb_confirm_route,
                }
            self.adapter._set_session_mode(session_id, self.route_mode_pcb)
            self.adapter._set_flow_state(session_id, self.flow_routing)
            self.adapter._swsd_update(
                session_id,
                self.escape_flow_id,
                "routing",
                self.bridge.escape_payload(session_id, {"reentryText": text}),
                event_type="user_jump",
                intent="route_again",
                action_type="user_jump",
                checkpoint_label="fanout rerun",
            )
            return {
                "mode": self.route_mode_pcb,
                "reason": "confirm_route",
                "intent": self.intent_pcb_confirm_route,
            }
        return None
