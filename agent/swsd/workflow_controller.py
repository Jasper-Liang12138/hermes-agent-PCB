"""Workflow control layer for SWSD-backed WebSocket routing."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from agent.swsd.action_candidates import ActionCandidate, IntentCandidateSet
from agent.swsd.control_signals import matches_cancel_signal, matches_confirm_signal, matches_reject_signal, matches_rollback_signal
from agent.swsd.pcb_intent_agent_loop import IntentAgentLoopInput, IntentAgentLoopResult, run_pcb_intent_agent_loops
from agent.swsd.registry import get_workflow
from agent.swsd.restore_renderer import render_restore_summary
from tools import pcb_model_runtime


@dataclass(frozen=True)
class SWSDTurnEvent:
    """Protocol-normalized user turn handed from WebSocket to SWSD."""

    session_id: str
    project_id: str = ""
    raw_user_text: str = ""
    body: Dict[str, Any] = field(default_factory=dict)
    turn_options: Dict[str, Any] = field(default_factory=dict)
    inbound_fields: Dict[str, Any] = field(default_factory=dict)
    body_fanout_params: Dict[str, Any] = field(default_factory=dict)
    content_fanout_params: Dict[str, Any] = field(default_factory=dict)
    is_slash_command: bool = False


@dataclass(frozen=True)
class SWSDTurnDecision:
    """Controller-owned decision consumed by the WebSocket transport layer."""

    mode: str
    reason: str
    intent: str
    immediate_reply: str | None = None
    bootstrap_get_project: bool = False


@dataclass(frozen=True)
class WorkflowActionPlan:
    """Controller-owned normalized result of the intent arbitration chain."""

    workflow_id: str
    workflow_state: str
    allowed_actions: tuple[str, ...]
    action: str
    phase: str
    reason: str
    accepted: bool
    entities: Dict[str, Any] = field(default_factory=dict)
    immediate_reply: str | None = None
    candidate_set: IntentCandidateSet | None = None
    stage: str = ""
    rejection_feedback: tuple[str, ...] = ()
    votes: tuple[bool, ...] = ()
    debug: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowPlanResult:
    """Result of dispatching a WorkflowActionPlan inside the controller."""

    plan: WorkflowActionPlan
    decision: SWSDTurnDecision | None = None


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

    def handle_turn(self, event: SWSDTurnEvent) -> SWSDTurnDecision:
        """Run SWSD-owned candidate generation, hint resolution, and arbitration."""
        plan = self.plan_turn(event)
        dispatched = self.dispatch_plan(event, plan)
        if dispatched.decision is not None:
            return dispatched.decision
        if plan.reason == "slash_command":
            return SWSDTurnDecision(mode="chat", reason="slash_command", intent="chat")
        if plan.reason == "empty":
            return SWSDTurnDecision(mode="chat", reason="empty", intent="chat")
        if not plan.accepted and plan.immediate_reply:
            mode = self.route_mode_pcb if plan.workflow_state != "idle" or self.adapter._session_mode(event.session_id) == self.route_mode_pcb else "chat"
            return SWSDTurnDecision(
                mode=mode,
                immediate_reply=plan.immediate_reply,
                reason=plan.stage or plan.reason or "intent_loop_feedback",
                intent="unclear",
            )
        if plan.reason == "decision_requires_confirmation":
            mode = self.route_mode_pcb if plan.workflow_state != "idle" or self.adapter._session_mode(event.session_id) == self.route_mode_pcb else "chat"
            return SWSDTurnDecision(
                mode=mode,
                immediate_reply="我还不能确定要执行哪一步，请补充目标器件、router 类型，或回复“确认/取消”。",
                reason=plan.reason,
                intent="unclear",
            )
        return self._decision_from_action(event, plan.workflow_id, plan.workflow_state, plan.action, plan.reason)

    def dispatch_plan(self, event: SWSDTurnEvent, plan: WorkflowActionPlan) -> WorkflowPlanResult:
        """Dispatch the normalized plan into a phase handler.

        Only chat is executed here for now. The other phases intentionally keep
        the existing SWSDTurnDecision mapping until their runtime chains are
        designed.
        """
        if plan.phase == "chat" and plan.reason not in {"slash_command", "empty"}:
            return self._handle_chat_plan(event, plan)
        if plan.phase == "execute":
            return self._handle_execute_plan(event, plan)
        return WorkflowPlanResult(plan=plan)

    def _handle_execute_plan(self, event: SWSDTurnEvent, plan: WorkflowActionPlan) -> WorkflowPlanResult:
        chain = self._execute_chain_for_plan(plan)
        if chain == "fanout":
            from agent.swsd.fanout_chain.fanout_execute_chain import FanoutExecuteChain

            result = FanoutExecuteChain(self).handle(event, plan)
            return WorkflowPlanResult(plan=plan, decision=result.decision)
        if chain == "reroute":
            return WorkflowPlanResult(
                plan=plan,
                decision=self._decision_from_action(event, plan.workflow_id, plan.workflow_state, plan.action, plan.reason),
            )
        return WorkflowPlanResult(plan=plan)

    def _execute_chain_for_plan(self, plan: WorkflowActionPlan) -> str:
        if plan.action == "pcb_entry" or plan.workflow_id == self.escape_flow_id:
            return "fanout"
        if plan.action in {"reroute_entry", "reroute_again"} or plan.workflow_id == self.reroute_flow_id:
            return "reroute"
        return ""
    def _handle_chat_plan(self, event: SWSDTurnEvent, plan: WorkflowActionPlan) -> WorkflowPlanResult:
        reply = self._sanitize_chat_reply(self._run_chat_agent(event, plan))
        if not reply:
            reply = "\u6211\u6682\u65f6\u6ca1\u6709\u751f\u6210\u53ef\u9760\u56de\u590d\uff0c\u8bf7\u7a0d\u540e\u518d\u8bd5\u3002"
        mode = self.route_mode_pcb if self._is_workflow_inner_chat(plan, event.session_id) else "chat"
        return WorkflowPlanResult(
            plan=plan,
            decision=SWSDTurnDecision(
                mode=mode,
                reason=plan.reason or "chat",
                intent="chat",
                immediate_reply=reply,
                bootstrap_get_project=False,
            ),
        )

    def _run_chat_agent(self, event: SWSDTurnEvent, plan: WorkflowActionPlan) -> str:
        runtime = pcb_model_runtime.resolve_model_runtime(pcb_model_runtime.STAGE_REROUTE)
        prompt = self._build_chat_agent_prompt(event, plan)
        try:
            from run_agent import AIAgent

            agent = AIAgent(
                model=runtime.get("model", ""),
                base_url=runtime.get("base_url", ""),
                api_key=runtime.get("api_key", ""),
                api_mode="chat_completions",
                max_iterations=2,
                enabled_toolsets=None,
                disabled_toolsets=["pcb"],
                quiet_mode=True,
                platform="websocket",
                session_id=event.session_id,
                skip_context_files=True,
                skip_memory=True,
                persist_session=False,
            )
            result = agent.run_conversation(
                event.raw_user_text,
                system_message=prompt,
                task_id=f"swsd-chat-{event.session_id}",
            )
            if isinstance(result, dict):
                return str(result.get("final_response") or "")
            return str(result or "")
        except Exception:
            return ""

    def _build_chat_agent_prompt(self, event: SWSDTurnEvent, plan: WorkflowActionPlan) -> str:
        chat_scope = "workflow_inner_chat" if self._is_workflow_inner_chat(plan, event.session_id) else "plain_chat"
        context = {
            "chat_scope": chat_scope,
            "workflow_id": plan.workflow_id,
            "workflow_state": plan.workflow_state,
            "project_id": event.project_id,
            "allowed_actions": list(plan.allowed_actions),
            "plan_reason": plan.reason,
            "entities": plan.entities,
            "explicit_fields": {
                "inbound_fields": event.inbound_fields,
                "body_fanout_params": event.body_fanout_params,
                "content_fanout_params": event.content_fanout_params,
            },
        }
        return (
            "You are handling a SWSD chat branch. Answer the user in Chinese unless the user asks otherwise. "
            "Do not call PCB or EDA tools, do not change workflow state, and do not claim that an action was executed. "
            "If chat_scope is workflow_inner_chat, keep the current workflow context in mind and explain without exiting it.\n\n"
            "SWSD_CONTEXT:\n"
            + json.dumps(context, ensure_ascii=False, default=str)
        )

    def _is_workflow_inner_chat(self, plan: WorkflowActionPlan, session_id: str) -> bool:
        if plan.workflow_state and plan.workflow_state != "idle":
            return True
        session_mode = getattr(self.adapter, "_session_mode", lambda _session_id: "chat")(session_id)
        return session_mode == self.route_mode_pcb

    @staticmethod
    def _sanitize_chat_reply(reply: str) -> str:
        text = str(reply or "")
        text = re.sub(r"(?is)<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>", "", text)
        text = re.sub(r"(?is)</?think(?:ing)?>", "", text)
        text = text.replace("\ufffd", "")
        text = "".join(ch for ch in text if ch in "\n\t" or ord(ch) >= 32)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if not text:
            return ""
        question_marks = text.count("?") + text.count("\uff1f")
        if len(text) >= 12 and question_marks / max(len(text), 1) > 0.35:
            return ""
        return text[:2000]

    def plan_turn(self, event: SWSDTurnEvent) -> WorkflowActionPlan:
        """Normalize the intent chain into a controller-owned action plan."""
        if event.is_slash_command:
            return WorkflowActionPlan(
                workflow_id="",
                workflow_state="idle",
                allowed_actions=("chat",),
                action="chat",
                phase="chat",
                reason="slash_command",
                accepted=True,
            )
        text = (event.raw_user_text or "").strip()
        if not text:
            return WorkflowActionPlan(
                workflow_id="",
                workflow_state="idle",
                allowed_actions=("chat",),
                action="chat",
                phase="chat",
                reason="empty",
                accepted=True,
            )
        loop_input = self.build_intent_loop_input(event)
        loop_result = self.run_intent_loop(loop_input)
        return self._workflow_action_plan_from_loop_result(loop_input, loop_result)

    def build_intent_loop_input(self, event: SWSDTurnEvent) -> IntentAgentLoopInput:
        """Assemble the loop input from the current workflow context and event."""
        session_id = event.session_id
        workflow_id, workflow_state = self.active_workflow_state(session_id)
        if not workflow_id:
            workflow_id = self.escape_flow_id
        if not workflow_state:
            workflow_state = "idle"
        candidates = self._generate_state_action_candidates(event, workflow_id, workflow_state)
        hints = self._resolve_experience_hints(event, workflow_id, workflow_state)
        experience_actions = self._experience_action_candidates(hints, event, workflow_id, workflow_state)
        explicit_action = self._explicit_protocol_action(event, workflow_id, workflow_state)
        allowed = self._allowed_actions(workflow_id, workflow_state, event)
        hint_payload = hints.as_dict() if hasattr(hints, "as_dict") else {}
        return IntentAgentLoopInput(
            user_text=(event.raw_user_text or "").strip(),
            workflow_id=workflow_id,
            workflow_state=workflow_state,
            allowed_actions=tuple(allowed),
            explicit_fields={
                "body": event.body,
                "turn_options": event.turn_options,
                "inbound_fields": event.inbound_fields,
                "body_fanout_params": event.body_fanout_params,
                "content_fanout_params": event.content_fanout_params,
            },
            hints=hint_payload,
            fallback_candidates=candidates,
            explicit_action=explicit_action,
            experience_actions=experience_actions,
            session_id=session_id,
            project_id=event.project_id,
        )

    def run_intent_loop(self, loop_input: IntentAgentLoopInput) -> IntentAgentLoopResult:
        """Run the multi-stage intent arbitration chain."""
        return run_pcb_intent_agent_loops(
            loop_input,
            getattr(self.adapter, "_swsd_intent_model", None),
        )

    def _workflow_action_plan_from_loop_result(
        self,
        loop_input: IntentAgentLoopInput,
        loop_result: IntentAgentLoopResult,
    ) -> WorkflowActionPlan:
        """Normalize the loop result into a future runtime-ready action plan."""
        policy = loop_result.policy
        action = loop_result.final_action or policy.action or "chat"
        if not loop_result.accepted and loop_result.feedback_reply:
            action = "clarify"
        entities = self._entities_from_loop_result(loop_result)
        phase = self._phase_for_action(action, accepted=loop_result.accepted)
        reason = loop_result.stage or policy.reason or ("candidate_accepted" if loop_result.accepted else "intent_loop_feedback")
        if policy.requires_confirmation and not policy.action and not loop_result.feedback_reply:
            phase = "fallback"
            reason = policy.reason or "decision_requires_confirmation"
        return WorkflowActionPlan(
            workflow_id=loop_input.workflow_id,
            workflow_state=loop_input.workflow_state,
            allowed_actions=loop_input.allowed_actions,
            action=action,
            phase=phase,
            reason=reason,
            accepted=loop_result.accepted,
            entities=entities,
            immediate_reply=loop_result.feedback_reply,
            candidate_set=loop_result.candidate_set,
            stage=loop_result.stage,
            rejection_feedback=loop_result.rejection_feedback,
            votes=loop_result.votes,
            debug={
                "policy_action": policy.action,
                "policy_reason": policy.reason,
                "policy_requires_confirmation": policy.requires_confirmation,
            },
        )

    @staticmethod
    def _phase_for_action(action: str, *, accepted: bool) -> str:
        if not accepted or action == "clarify":
            return "fallback"
        if not action or action == "chat":
            return "chat"
        if action in {
            "rollback_checkpoint",
            "restore_params_version",
            "restore_layout_checkpoint",
            "change_target",
            "modify_params",
            "modify_router_choice",
            "modify_order_lines",
            "modify_constraints",
            "rerun_fanout",
            "confirm_import",
            "reject_import",
            "reject_route",
        }:
            return "jump"
        return "execute"

    @staticmethod
    def _entities_from_loop_result(loop_result: IntentAgentLoopResult) -> Dict[str, Any]:
        if loop_result.policy.accepted_candidates:
            return dict(loop_result.policy.accepted_candidates[0].entities)
        if loop_result.final_action:
            for candidate in loop_result.candidate_set.candidate_actions:
                if candidate.action == loop_result.final_action:
                    return dict(candidate.entities)
        if loop_result.candidate_set.candidate_actions:
            return dict(loop_result.candidate_set.candidate_actions[0].entities)
        return {}

    def _resolve_experience_hints(self, event: SWSDTurnEvent, workflow_id: str, workflow_state: str) -> Any:
        resolver = getattr(self.adapter, "_resolve_pcb_experience", None)
        if resolver is None:
            return None
        return resolver(
            event.session_id,
            project_id=event.project_id,
            query=event.raw_user_text,
            workflow_id=workflow_id,
            workflow_state=workflow_state,
        )

    def _allowed_actions(self, workflow_id: str, workflow_state: str, event: SWSDTurnEvent) -> list[str]:
        actions: list[str] = ["chat"]
        workflow = get_workflow(workflow_id)
        if workflow is not None:
            actions.extend(transition.intent for transition in workflow.transitions if transition.from_state == workflow_state)
        if workflow_id == self.escape_flow_id:
            if workflow_state == "idle":
                actions.extend(["pcb_entry", "reroute_entry"])
            actions.extend(["pcb_entry", "select_target", "change_target", "modify_params", "rerun_fanout", "rollback_checkpoint", "restore_params_version", "restore_layout_checkpoint", "confirm_route", "cancel_flow"])
        elif workflow_id == self.reroute_flow_id:
            actions.extend(["reroute_entry", "reroute_again", "rollback_checkpoint", "confirm_import", "reject_import", "cancel_flow"])
        else:
            actions.extend(["pcb_entry", "reroute_entry", "cancel_flow"])
        return list(dict.fromkeys(action for action in actions if action))

    def _explicit_protocol_action(self, event: SWSDTurnEvent, workflow_id: str, workflow_state: str) -> str:
        text = event.raw_user_text or ""
        if event.body_fanout_params and self._is_confirm_text(text):
            return "confirm_route"
        if event.inbound_fields.get("routingResult") and self._is_confirm_text(text):
            return "confirm_import"
        return ""

    def _generate_state_action_candidates(self, event: SWSDTurnEvent, workflow_id: str, workflow_state: str) -> tuple[ActionCandidate, ...]:
        adapter = self.adapter
        text = event.raw_user_text or ""
        flow_state = adapter._session_flow_states.get(event.session_id, self.bridge.flow_idle)
        candidates: list[ActionCandidate] = []

        def add(action: str, confidence: float, *, entities: dict[str, Any] | None = None, reason: str = "", source: str = "intent_model") -> None:
            candidates.append(ActionCandidate(action, confidence, entities or {}, reason, source))

        direct_execute = bool(re.search(r"direct\s+(?:start|execute)|(?:start|run|execute).{0,12}(?:PCB|BGA|fanout)", text, flags=re.IGNORECASE))
        if not direct_execute:
            direct_phrases = (
                "\u76f4\u63a5\u5f00\u59cb",
                "\u4e0d\u8981\u89e3\u91ca",
                "\u5f00\u59cb\u9003\u9038",
                "\u6267\u884c\u9003\u9038",
                "\u5f00\u59cb\u5e03\u7ebf",
                "\u6267\u884c\u5e03\u7ebf",
            )
            pcb_terms = ("PCB", "BGA", "fanout", "\u6247\u51fa", "\u9003\u9038", "\u5e03\u7ebf")
            direct_execute = any(phrase in text for phrase in direct_phrases) and any(term.lower() in text.lower() for term in pcb_terms)
        if adapter._is_pcb_concept_question_without_execution(text) and not direct_execute:
            add("chat", 0.99, reason="concept or explicit no-operation request", source="intent_model")
        if matches_cancel_signal(text):
            add("cancel_flow", 1.0, reason="explicit cancel signal", source="control_signal")
        if self._is_rollback_text(text):
            add("rollback_checkpoint", 0.98, reason="explicit rollback signal", source="control_signal")
        if re.search(r"(?:#|\uff03)\s*(?:reroute|\u62c6\u7ebf\s*\u91cd\u5e03)|\u62c6\u7ebf\s*\u91cd\u5e03|\u5220\u9664.*(?:\u7ebf|\u8d70\u7ebf|trace|traces|\u6846\u9009|\u9009\u4e2d)|\breroute\b|\bripup\b|\brip-up\b", text, flags=re.IGNORECASE):
            action = "reroute_again" if workflow_id == self.reroute_flow_id and workflow_state in {"report", "import", "drc_loop"} else "reroute_entry"
            add(action, 0.96, reason="explicit reroute request", source="control_signal")
        if re.search(r"\u6062\u590d.*(?:\u53c2\u6570|\u7248)", text, flags=re.IGNORECASE):
            add("restore_params_version", 0.95, reason="restore fanout params request")
        if re.search(r"\u6062\u590d.*(?:\u7248\u56fe|layout)", text, flags=re.IGNORECASE):
            add("restore_layout_checkpoint", 0.95, reason="restore layout request")
        if re.search(r"(?:\u91cd\u65b0\u751f\u6210\u53c2\u6570|\u91cd\u65b0\s*fanout|\u518d\u8dd1\u4e00\u8f6e\s*fanout|rerun\s*fanout|\u91cd\u65b0\u5e03\u7ebf)", text, flags=re.IGNORECASE):
            add("rerun_fanout", 0.9, reason="fanout rerun request")
        if not direct_execute and self._is_confirm_text(text):
            add("confirm_route" if workflow_id == self.escape_flow_id else "confirm_import", 0.82, reason="confirm signal", source="control_signal")
        if not direct_execute and self._is_reject_text(text):
            add("reject_import" if workflow_id == self.reroute_flow_id else "reject_route", 0.82, reason="reject signal", source="control_signal")

        selected = adapter._resolve_selected_label(event.session_id, text)
        if selected:
            add("change_target" if workflow_state not in {"idle", "select_bga"} else "select_target", 0.92, entities={"selectedBGA": selected}, reason="target label resolved")
        router_type = adapter._extract_complete_router_choice(event.session_id, text)
        if router_type:
            add("modify_params" if workflow_state in {"review", "import", "escape_order", "layer_assign_escape_order"} else "layer_assigned", 0.9, entities={"routerType": router_type}, reason="router choice resolved")
        elif adapter._extract_route_algorithm(text) or adapter._extract_fanout_module(text):
            add("modify_params" if workflow_state in {"review", "import", "escape_order", "layer_assign_escape_order"} else "layer_assigned", 0.7, reason="partial router choice")

        try:
            from tools.pcb_nl_fanout import parse_fanout_constraints_from_text, parse_fanout_target_from_text, parse_natural_language_order_lines
        except Exception:
            parse_fanout_constraints_from_text = None
            parse_fanout_target_from_text = None
            parse_natural_language_order_lines = None
        if parse_fanout_constraints_from_text is not None and parse_fanout_constraints_from_text(text):
            add("modify_params", 0.88, reason="fanout constraint change")
        if parse_natural_language_order_lines is not None and parse_natural_language_order_lines(text, adapter._session_fanout_params.get(event.session_id, {}).get("orderLines") or []):
            add("modify_order_lines", 0.86, reason="fanout order line change")
        target = adapter._extract_targeted_global_fanout_refdes(text)
        if not target and parse_fanout_target_from_text is not None:
            target = parse_fanout_target_from_text(text)
        if target or adapter._is_forced_global_fanout_command(text) or direct_execute:
            add("pcb_entry", 0.99 if direct_execute else 0.94, entities={"target": target} if target else {}, reason="fanout entry request")
        elif flow_state == self.bridge.flow_idle and re.search(r"BGA|fanout|\u9003\u9038|\u6247\u51fa|\u5e03\u7ebf", text, flags=re.IGNORECASE) and re.search(r"\u5e2e\u6211|\u8bf7|\u505a|\u6267\u884c|\u5f00\u59cb|\u751f\u6210|\u83b7\u53d6|\u5bf9", text, flags=re.IGNORECASE):
            add("pcb_entry", 0.82, reason="pcb fanout execution request")
        if not candidates:
            add("chat", 0.8, reason="no workflow action candidate", source="default")
        return tuple(candidates)

    def _experience_action_candidates(self, hints: Any, event: SWSDTurnEvent, workflow_id: str, workflow_state: str) -> tuple[ActionCandidate, ...]:
        if hints is None or not getattr(hints, "experience_used", False):
            return ()
        actions: list[ActionCandidate] = []
        prefs = hints.hint_value("pcbPreference", {}) if hasattr(hints, "hint_value") else {}
        if workflow_id == self.escape_flow_id and workflow_state in {"layer_assign", "escape_order", "layer_assign_escape_order"} and isinstance(prefs, dict):
            if prefs.get("defaultRouterType") or prefs.get("defaultLayerOrderModule"):
                actions.append(ActionCandidate("modify_params", 0.58, {"preference": prefs}, "project default router preference", "experience"))
        return tuple(actions)

    def _decision_from_action(self, event: SWSDTurnEvent, workflow_id: str, workflow_state: str, action: str, policy_reason: str = "") -> SWSDTurnDecision:
        adapter = self.adapter
        session_id = event.session_id
        text = event.raw_user_text or ""
        flow_state = adapter._session_flow_states.get(session_id, self.bridge.flow_idle)
        if not action or action == "chat":
            return SWSDTurnDecision(mode="chat", reason=policy_reason or "default_chat", intent="chat")
        if action == "cancel_flow":
            adapter._reset_flow(session_id)
            adapter._set_session_mode(session_id, "chat", lock_seconds=0.0)
            return SWSDTurnDecision(mode="chat", immediate_reply="\u5df2\u9000\u51fa\u5f53\u524d PCB \u6d41\u7a0b\uff0c\u4f60\u53ef\u4ee5\u7ee7\u7eed\u804a\u5929\u6216\u91cd\u65b0\u53d1\u8d77\u65b0\u7684 PCB \u64cd\u4f5c\u3002", reason="cancel_flow", intent="cancel")

        jump = self.handle_jump(session_id, workflow_id, workflow_state, text)
        if jump is not None:
            return SWSDTurnDecision(**jump)

        if action in {"reroute_entry", "reroute_again"}:
            adapter._reset_flow(session_id)
            adapter._set_flow_state(session_id, self.flow_reroute)
            adapter._set_session_mode(session_id, self.route_mode_pcb)
            return SWSDTurnDecision(mode=self.route_mode_pcb, reason="pcb_reroute_selected", intent=self.intent_pcb_reroute_selected, bootstrap_get_project=False)
        if action == "pcb_entry":
            target = adapter._extract_targeted_global_fanout_refdes(text)
            if not target:
                try:
                    from tools.pcb_nl_fanout import parse_fanout_target_from_text
                    target = parse_fanout_target_from_text(text)
                except Exception:
                    target = ""
            adapter._reset_flow(session_id)
            if target:
                adapter._session_requested_bga_targets[session_id] = target
            adapter._set_session_mode(session_id, self.route_mode_pcb)
            return SWSDTurnDecision(mode=self.route_mode_pcb, reason="forced_global_fanout", intent="pcb_entry", bootstrap_get_project=True)
        if action in {"layer_assigned", "modify_params", "modify_router_choice"}:
            router_type = adapter._extract_complete_router_choice(session_id, text)
            if router_type:
                adapter._session_router_types[session_id] = router_type
                adapter._set_session_mode(session_id, self.route_mode_pcb)
                return SWSDTurnDecision(mode=self.route_mode_pcb, reason="router_type_step", intent=self.intent_pcb_followup)
            adapter._set_session_mode(session_id, self.route_mode_pcb)
            return SWSDTurnDecision(mode=self.route_mode_pcb, immediate_reply=adapter._router_choice_followup_prompt(session_id), reason="partial_router_choice", intent=self.intent_pcb_followup)
        if action in {"select_target", "change_target"}:
            selected = adapter._resolve_selected_label(session_id, text)
            if selected:
                adapter._session_selected_targets[session_id] = selected
                adapter._set_session_mode(session_id, self.route_mode_pcb)
                adapter._set_flow_state(session_id, self.flow_wait_router_type)
                return SWSDTurnDecision(mode=self.route_mode_pcb, immediate_reply=adapter._router_type_prompt(session_id), reason="selection_step_wait_router_type", intent=self.intent_pcb_select_target)
            return SWSDTurnDecision(mode=self.route_mode_pcb, immediate_reply=f"\u8bf7\u5148\u9009\u62e9\u76ee\u6807\u5668\u4ef6\uff08\u4f8b\u5982\u201c\u9009\u62e9 {adapter._selection_example(session_id)}\u201d\uff09\uff0c\u6216\u56de\u590d\u201c\u53d6\u6d88\u201d\u9000\u51fa\u3002", reason="invalid_selection_turn", intent="unclear")
        if action == "confirm_route":
            if flow_state == self.bridge.flow_wait_selection:
                return SWSDTurnDecision(mode=self.route_mode_pcb, immediate_reply=f"当前还在选择阶段，请先回复器件，例如“选择 {adapter._selection_example(session_id)}”，或回复“取消”。", reason="confirm_before_selection", intent=self.intent_pcb_confirm_route)
            if flow_state == self.flow_wait_router_type and not adapter._session_fanout_params.get(session_id):
                return SWSDTurnDecision(mode=self.route_mode_pcb, immediate_reply="\u6267\u884c\u5e03\u7ebf\u524d\u5fc5\u987b\u5148\u9009\u62e9\u8d70\u7ebf\u7b97\u6cd5\u548c\u5c42\u5206\u914d/\u9003\u9038\u987a\u5e8f\u751f\u6210\u6a21\u5757\u3002\u8bf7\u56de\u590d\u4f8b\u5982 `135 + RL`\u3002", reason="confirm_before_router_type", intent=self.intent_pcb_confirm_route)
            if not adapter._session_fanout_params.get(session_id):
                return SWSDTurnDecision(mode=self.route_mode_pcb, immediate_reply="\u7f3a\u5c11\u5df2\u786e\u8ba4\u7684\u9003\u9038\u53c2\u6570\u914d\u7f6e\uff0c\u8bf7\u5148\u91cd\u65b0\u751f\u6210\u9003\u9038\u53c2\u6570\u3002", reason="confirm_without_fanout_params", intent=self.intent_pcb_confirm_route)
            adapter._set_flow_state(session_id, self.flow_routing)
            return SWSDTurnDecision(mode=self.route_mode_pcb, reason="confirm_route", intent=self.intent_pcb_confirm_route)
        return SWSDTurnDecision(mode="chat", reason=policy_reason or "unhandled_action", intent="chat")

    @staticmethod
    def _is_rollback_text(text: str) -> bool:
        return matches_rollback_signal(text)

    @staticmethod
    def _is_reject_text(text: str) -> bool:
        return matches_reject_signal(text)

    @staticmethod
    def _is_confirm_text(text: str) -> bool:
        return matches_confirm_signal(text)

    def _handle_escape_restore_request(self, session_id: str, text: str) -> Optional[Dict[str, Any]]:
        try:
            from agent.swsd.experience.fanout_versions import parse_fanout_version_request
            from tools import pcb_tools
        except Exception:
            return None

        request = parse_fanout_version_request(text)
        if not request.get("isVersionRequest"):
            return None
        if request.get("intent") == "list_versions":
            return None

        transport = pcb_tools._transport
        summary = transport.fanout_version_summary(session_id)
        previous_active_version = summary.get("activeParamsVersion") if isinstance(summary, dict) else None
        previous_layout_version = summary.get("currentLayoutVersion") if isinstance(summary, dict) else None

        restored_kind = ""
        restored_version = None
        changed_fields: list[str] = []

        if request.get("intent") == "iterate_from_layout":
            layout_text, restored_version, _layout_path = transport.fanout_layout_data(
                session_id,
                request.get("baseLayoutVersion"),
            )
            if not layout_text:
                return {
                    "mode": self.route_mode_pcb,
                    "immediate_reply": "没有找到可恢复的版图检查点；可以先完成一轮 fanout 或 reroute 后再恢复。",
                    "reason": "restore_layout_missing",
                    "intent": self.intent_pcb_followup,
                }
            self.adapter._session_layout_versions[session_id] = restored_version
            self.adapter._set_session_mode(session_id, self.route_mode_pcb)
            self.adapter._set_flow_state(session_id, self.bridge.flow_wait_confirm)
            self.adapter._swsd_update(
                session_id,
                self.escape_flow_id,
                "review",
                self.bridge.escape_payload(
                    session_id,
                    {
                        "restoredKind": "layout",
                        "restoredFromVersion": restored_version,
                        "versionRequest": request,
                        "restoredLayoutText": layout_text[:4000],
                    },
                ),
                event_type="user_jump",
                intent="restore_layout_checkpoint",
                action_type="user_jump",
                checkpoint_label="restore layout checkpoint",
            )
            restored_kind = "layout"
            changed_fields = ["currentLayoutVersion"]
        else:
            version = request.get("restoredFromVersion")
            if version in (None, ""):
                version = request.get("baseLayoutVersion") or "current"
            fanout_params, restored_version = transport.fanout_params_for_version(session_id, version)
            if not fanout_params and version in {"current", "latest", "last"}:
                fanout_params, restored_version = transport.latest_fanout_params(session_id)
            if not fanout_params:
                return {
                    "mode": self.route_mode_pcb,
                    "immediate_reply": "没有找到可恢复的 fanout 参数版本；可以先执行一次 fanout 生成/布线后再恢复。",
                    "reason": "restore_params_missing",
                    "intent": self.intent_pcb_followup,
                }
            fanout_params = dict(fanout_params)
            if restored_version is not None:
                fanout_params["restoredFromVersion"] = restored_version
            draft = transport.write_fanout_draft(
                session_id,
                fanout_params=fanout_params,
                user_text=text,
                base_layout_version=request.get("baseLayoutVersion"),
                restored_from_version=restored_version,
            )
            self.adapter._remember_fanout_params_from_frontend(session_id, fanout_params)
            self.adapter._session_active_params_versions[session_id] = restored_version
            if request.get("baseLayoutVersion") not in (None, ""):
                self.adapter._session_layout_versions[session_id] = request.get("baseLayoutVersion")
            self.adapter._set_session_mode(session_id, self.route_mode_pcb)
            self.adapter._set_flow_state(session_id, self.bridge.flow_wait_confirm)
            self.adapter._swsd_update(
                session_id,
                self.escape_flow_id,
                "review",
                self.bridge.escape_payload(
                    session_id,
                    {
                        "restoredKind": "params",
                        "restoredFromVersion": restored_version,
                        "versionRequest": request,
                        "versionDraft": draft,
                    },
                ),
                event_type="user_jump",
                intent="restore_params_version",
                action_type="user_jump",
                checkpoint_label="restore params version",
            )
            restored_kind = "params"
            changed_fields = ["fanoutParams", "activeParamsVersion"]

        fanout_history = transport.fanout_version_summary(session_id)
        fields = {
            "fanoutHistory": fanout_history,
            "restoredKind": restored_kind,
            "restoredFromVersion": restored_version,
            "currentLayoutVersion": self.adapter._session_layout_versions.get(session_id),
            "activeParamsVersion": self.adapter._session_active_params_versions.get(session_id),
            "changedFields": changed_fields,
            "previousValues": {
                "currentLayoutVersion": previous_layout_version,
                "activeParamsVersion": previous_active_version,
            },
            "currentValues": {
                "selectedBGA": self.adapter._session_selected_targets.get(session_id),
                "routerType": self.adapter._session_router_types.get(session_id),
                "currentLayoutVersion": self.adapter._session_layout_versions.get(session_id),
                "activeParamsVersion": self.adapter._session_active_params_versions.get(session_id),
            },
            "requiresReroute": restored_kind == "params",
            "requiresReimport": restored_kind == "layout",
        }
        if restored_kind == "params":
            fields["fanoutParams"] = self.adapter._session_fanout_params.get(session_id, {})
            reason = "restore_params_version"
        else:
            reason = "restore_layout_checkpoint"
        message = render_restore_summary(fields)
        return {
            "mode": self.route_mode_pcb,
            "immediate_reply": (
                message
                + "\n\n##PCB_FIELDS##\n"
                + json.dumps(fields, ensure_ascii=False)
                + "\n##PCB_FIELDS_END##"
            ),
            "reason": reason,
            "intent": self.intent_pcb_followup,
        }

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
            if self._is_rollback_text(text) and self.jump_to_checkpoint(session_id, self.reroute_flow_id):
                return {
                    "mode": self.route_mode_pcb,
                    "immediate_reply": "已恢复到上一个拆线重布检查点，请继续。",
                    "reason": "reroute_rollback",
                    "intent": self.intent_pcb_followup,
                }
            if workflow_state in {"report", "import"} and self._is_confirm_text(text):
                self.adapter._set_session_mode(session_id, self.route_mode_pcb)
                self.adapter._swsd_update(
                    session_id,
                    self.reroute_flow_id,
                    "import",
                    self.bridge.reroute_payload(session_id, {"confirmText": text}),
                    event_type="user_jump",
                    intent="confirm_import",
                    action_type="user_jump",
                    checkpoint_label="reroute confirm import",
                )
                return {
                    "mode": self.route_mode_pcb,
                    "reason": "confirm_import",
                    "intent": self.intent_pcb_followup,
                }
            if self._is_reject_text(text):
                self.adapter._set_session_mode(session_id, self.route_mode_pcb)
                self.adapter._swsd_update(
                    session_id,
                    self.reroute_flow_id,
                    workflow_state,
                    self.bridge.reroute_payload(session_id, {"rejectText": text}),
                    event_type="user_jump",
                    intent="reject_import",
                    action_type="user_jump",
                    checkpoint_label="reroute reject import",
                )
                return {
                    "mode": self.route_mode_pcb,
                    "immediate_reply": "已取消当前导入，你可以继续调整、恢复检查点或再来一轮 reroute。",
                    "reason": "reject_import",
                    "intent": self.intent_pcb_followup,
                }
            return None

        if workflow_id != self.escape_flow_id:
            return None

        restore_reply = self._handle_escape_restore_request(session_id, text)
        if restore_reply is not None:
            return restore_reply

        fanout_rerun = re.search(r"(?:\u91cd\u65b0\u751f\u6210\u53c2\u6570|\u91cd\u65b0\s*fanout|\u518d\u8dd1\u4e00\u8f6e\s*fanout|rerun\s*fanout|\u91cd\u65b0\u5e03\u7ebf)", text, flags=re.IGNORECASE)
        explicit_reroute = re.search(r"(?:\u62c6\u7ebf\u91cd\u5e03|\u5220\u9664.*(?:\u8d70\u7ebf|\u7ebf|trace|traces|\u6846\u9009)|\breroute\b|\bripup\b|\brip-up\b)", text, flags=re.IGNORECASE)
        if workflow_state in {"layer_assign", "escape_order", "layer_assign_escape_order", "routing", "review", "import"} and fanout_rerun and not explicit_reroute:
            draft = self.adapter._refresh_fanout_params_draft(session_id, user_text=text)
            for key in ("orderLines", "naturalLanguageOrderLines", "routingResult", "report"):
                draft.pop(key, None)
            self.adapter._session_fanout_params[session_id] = draft
            self.adapter._set_session_mode(session_id, self.route_mode_pcb)
            self.adapter._set_flow_state(session_id, self.flow_wait_router_type)
            self.adapter._swsd_update(
                session_id,
                self.escape_flow_id,
                "layer_assign",
                self.bridge.escape_payload(session_id, {"rerunText": text, "fanoutParams": draft}),
                event_type="user_jump",
                intent="rerun_fanout",
                action_type="user_jump",
                checkpoint_label="fanout rerun",
            )
            return {
                "mode": self.route_mode_pcb,
                "immediate_reply": self.adapter._with_pcb_fields_message(
                    self.adapter._router_choice_followup_prompt(session_id),
                    self.adapter._current_fanout_reply_fields(session_id),
                ),
                "reason": "rerun_fanout",
                "intent": self.intent_pcb_followup,
            }

        if workflow_state in {"layer_assign", "escape_order", "layer_assign_escape_order", "routing", "review", "import"} and self._is_rollback_text(text):
            if self.jump_to_checkpoint(session_id, self.escape_flow_id):
                return {
                    "mode": self.route_mode_pcb,
                    "immediate_reply": self.adapter._with_pcb_fields_message(
                        "已恢复到上一个全局 fanout 检查点，请继续。",
                        self.adapter._current_fanout_reply_fields(session_id),
                    ),
                    "reason": "escape_rollback",
                    "intent": self.intent_pcb_followup,
                }
        if workflow_state not in {"layer_assign", "escape_order", "layer_assign_escape_order", "routing", "review", "import"}:
            return None

        selected_label = self.adapter._resolve_selected_label(session_id, text)
        if selected_label:
            self.adapter._session_selected_targets[session_id] = selected_label
            self.adapter._session_requested_bga_targets[session_id] = selected_label
            self.adapter._refresh_fanout_params_draft(session_id, selected_bga=selected_label, user_text=text)
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
                "immediate_reply": self.adapter._with_pcb_fields_message(
                    self.adapter._router_type_prompt(session_id),
                    self.adapter._current_fanout_reply_fields(session_id),
                ),
                "reason": "escape_change_target",
                "intent": self.intent_pcb_select_target,
            }

        menu_choice = self.adapter._extract_router_menu_choice(session_id, text)
        explicit_route_algorithm = menu_choice.get("routeAlgorithm") or self.adapter._extract_route_algorithm(text)
        explicit_fanout_module = menu_choice.get("fanoutModule") or self.adapter._extract_fanout_module(text)
        constraint_patch: Dict[str, Any] = {}
        explicit_order = []
        try:
            from tools.pcb_nl_fanout import parse_fanout_constraints_from_text, parse_natural_language_order_lines
        except Exception:
            parse_fanout_constraints_from_text = None
            parse_natural_language_order_lines = None
        if parse_fanout_constraints_from_text is not None:
            constraint_patch = parse_fanout_constraints_from_text(text)
        if parse_natural_language_order_lines is not None:
            current_order = (self.adapter._session_fanout_params.get(session_id) or {}).get("orderLines") or []
            explicit_order = parse_natural_language_order_lines(text, current_order)

        has_param_adjustment = bool(
            menu_choice
            or explicit_route_algorithm
            or explicit_fanout_module
            or constraint_patch
            or explicit_order
        )
        if has_param_adjustment:
            draft = self.adapter._refresh_fanout_params_draft(
                session_id,
                user_text=text,
                route_algorithm=explicit_route_algorithm,
                fanout_module=explicit_fanout_module,
            )
            has_complete_router = bool(draft.get("routerType"))
            prefer_direct_router_step = workflow_state in {"layer_assign", "escape_order", "layer_assign_escape_order"} and has_complete_router
            next_flow_state = (
                self.flow_wait_router_type
                if not has_complete_router
                else self.bridge.flow_wait_confirm
            )
            next_workflow_state = "review" if has_complete_router else "layer_assign_escape_order"
            self.adapter._set_session_mode(session_id, self.route_mode_pcb)
            self.adapter._set_flow_state(session_id, next_flow_state)
            self.adapter._swsd_update(
                session_id,
                self.escape_flow_id,
                next_workflow_state,
                self.bridge.escape_payload(
                    session_id,
                    {
                        "jumpText": text,
                        "menuChoice": menu_choice,
                        "constraintPatch": constraint_patch,
                        "explicitOrderLines": explicit_order,
                    },
                ),
                event_type="user_jump",
                intent="modify_params",
                action_type="user_jump",
                checkpoint_label="fanout modify params",
            )
            if prefer_direct_router_step:
                return {
                    "mode": self.route_mode_pcb,
                    "reason": "router_type_step",
                    "intent": self.intent_pcb_followup,
                }
            message = (
                "已更新当前 fanout 配置，请确认是否按新参数继续。"
                if has_complete_router
                else self.adapter._router_choice_followup_prompt(session_id)
            )
            return {
                "mode": self.route_mode_pcb,
                "immediate_reply": self.adapter._with_pcb_fields_message(
                    message,
                    self.adapter._current_fanout_reply_fields(session_id),
                ),
                "reason": "escape_modify_params",
                "intent": self.intent_pcb_followup,
            }

        if workflow_state == "review" and self._is_reject_text(text):
            self.adapter._set_session_mode(session_id, self.route_mode_pcb)
            self.adapter._swsd_update(
                session_id,
                self.escape_flow_id,
                "review",
                self.bridge.escape_payload(session_id, {"rejectText": text}),
                event_type="user_jump",
                intent="reject_route",
                action_type="user_jump",
                checkpoint_label="fanout reject route",
            )
            return {
                "mode": self.route_mode_pcb,
                "immediate_reply": "已取消本次布线执行，你可以修改参数、恢复历史版本，或重新确认执行。",
                "reason": "reject_route",
                "intent": self.intent_pcb_followup,
            }

        if workflow_state == "review" and re.search(r"导入|import", text, flags=re.IGNORECASE) and self._is_confirm_text(text):
            self.adapter._set_session_mode(session_id, self.route_mode_pcb)
            self.adapter._swsd_update(
                session_id,
                self.escape_flow_id,
                "import",
                self.bridge.escape_payload(session_id, {"confirmText": text}),
                event_type="user_jump",
                intent="confirm_import",
                action_type="user_jump",
                checkpoint_label="fanout confirm import",
            )
            return {
                "mode": self.route_mode_pcb,
                "reason": "confirm_import",
                "intent": self.intent_pcb_followup,
            }

        if workflow_state == "import" and self._is_reject_text(text):
            self.adapter._set_session_mode(session_id, self.route_mode_pcb)
            self.adapter._swsd_update(
                session_id,
                self.escape_flow_id,
                "import",
                self.bridge.escape_payload(session_id, {"rejectText": text}),
                event_type="user_jump",
                intent="reject_import",
                action_type="user_jump",
                checkpoint_label="fanout reject import",
            )
            return {
                "mode": self.route_mode_pcb,
                "immediate_reply": "已取消当前导入，你可以继续调整参数、恢复版本，或再次确认导入。",
                "reason": "reject_import",
                "intent": self.intent_pcb_followup,
            }

        if workflow_state in {"review", "import"} and self._is_confirm_text(text):
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
