"""SWSD-owned reroute execute chain."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict

from agent.swsd.fanout_chain.fallback_expert_loop import run_fallback_expert_loop
from agent.swsd.response_builder import SWSDResponseBuilder

from .markdown_report import build_reroute_markdown_report

if TYPE_CHECKING:
    from agent.swsd.workflow_controller import SWSDTurnDecision, SWSDTurnEvent, WorkflowActionPlan


@dataclass(frozen=True)
class RerouteChainResult:
    decision: "SWSDTurnDecision"


class RerouteExecuteChain:
    def __init__(self, controller: Any) -> None:
        self.controller = controller
        self.adapter = controller.adapter
        self.bridge = controller.bridge
        self.state = getattr(self.adapter, "_swsd_state", None)
        self.workflow_id = controller.reroute_flow_id

    def handle(self, event: "SWSDTurnEvent", plan: "WorkflowActionPlan") -> RerouteChainResult:
        current_state = str(plan.workflow_state or "idle")
        action = plan.action or "reroute_entry"
        if action in {"reroute_entry", "reroute_again"}:
            validation = self._validate_entry(current_state, action)
            if not validation.valid:
                return self._invalid_entry(event, current_state, validation.reason)
            return self._request_rip_up(event, plan, action)
        if action == "confirm_reroute":
            return self._confirm_reroute(event, plan)
        if action == "confirm_import":
            return self._confirm_import(event, plan)
        if action == "reject_import":
            return self._reject_import(event, plan)
        return self._invalid_entry(event, current_state, f"reroute action {action!r} is not supported by reroute execute chain")

    @staticmethod
    def _turn_decision_cls() -> Any:
        from agent.swsd.workflow_controller import SWSDTurnDecision

        return SWSDTurnDecision

    def _validate_entry(self, current_state: str, action: str) -> Any:
        if self.state is not None and hasattr(self.state, "validate_entry"):
            return self.state.validate_entry(self.workflow_id, current_state, action)
        allowed = set(self.controller._allowed_actions(self.workflow_id, current_state, None))
        valid = action in allowed or (action == "reroute_entry" and current_state == "idle")
        return type("Validation", (), {"valid": valid, "reason": "action is not allowed", "allowed_actions": tuple(allowed)})()

    def _invalid_entry(self, event: "SWSDTurnEvent", current_state: str, reason: str) -> RerouteChainResult:
        fallback = run_fallback_expert_loop(
            user_text=event.raw_user_text or "",
            invalid_reason=reason or "reroute_entry_invalid",
            current_state=current_state,
            model=getattr(self.adapter, "_swsd_fallback_model", None),
        )
        return RerouteChainResult(
            decision=self._turn_decision_cls()(
                mode=self.controller.route_mode_pcb,
                reason=fallback.reason,
                intent="unclear",
                immediate_reply=fallback.reply,
                bootstrap_get_project=False,
            )
        )

    def _request_rip_up(self, event: "SWSDTurnEvent", plan: "WorkflowActionPlan", action: str) -> RerouteChainResult:
        session_id = event.session_id
        self.adapter._reset_flow(session_id)
        self.adapter._set_session_mode(session_id, self.controller.route_mode_pcb)
        self.adapter._set_flow_state(session_id, self.controller.flow_reroute)
        self._record_step(
            session_id,
            "rip_up",
            "rip_up_requested",
            {
                "requestedAction": action,
                "lastActionReason": plan.reason,
                "userText": event.raw_user_text or "",
                "projectData": self._project_data_payload(status="requested"),
                "rerouteFiles": {},
                "createdAt": time.time(),
            },
            event_type="workflow_action",
            intent=action,
            action_type="tool_call_request",
            checkpoint_label="reroute rip-up requested",
        )
        return RerouteChainResult(
            decision=self._turn_decision_cls()(
                mode=self.controller.route_mode_pcb,
                reason="reroute_rip_up_request",
                intent=self.controller.intent_pcb_reroute_selected,
                bootstrap_get_project=False,
                tool_call={"name": "deleteTracesForRerouting", "arguments": {}, "timeout": 360.0},
            )
        )

    def _confirm_reroute(self, event: "SWSDTurnEvent", plan: "WorkflowActionPlan") -> RerouteChainResult:
        session_id = event.session_id
        self.adapter._set_session_mode(session_id, self.controller.route_mode_pcb)
        self.adapter._set_flow_state(session_id, self.controller.flow_reroute)
        self._record_step(
            session_id,
            "reroute_llm",
            "reroute_confirmed",
            {"confirmText": event.raw_user_text or "", "lastActionReason": plan.reason},
            event_type="user_jump",
            intent="confirm_reroute",
            action_type="user_confirm",
            checkpoint_label="reroute confirmed",
        )
        return RerouteChainResult(
            decision=self._turn_decision_cls()(
                mode=self.controller.route_mode_pcb,
                reason="reroute_generate_request",
                intent="confirm_reroute",
                tool_call={"name": "reroute", "arguments": {"localRerouteCompletionPolicy": {"mode": "selected_net_local_first"}}, "timeout": 720.0},
            )
        )

    def _confirm_import(self, event: "SWSDTurnEvent", plan: "WorkflowActionPlan") -> RerouteChainResult:
        session_id = event.session_id
        payload = self._state_payload(session_id)
        import_file = self._import_file_from_payload(payload)
        if not import_file:
            return RerouteChainResult(
                decision=self._turn_decision_cls()(
                    mode=self.controller.route_mode_pcb,
                    reason="reroute_import_missing_file",
                    intent="unclear",
                    immediate_reply="还没有可导入的 reroute 结果。请先完成拆线重布并确认报告。",
                )
            )
        self.adapter._set_session_mode(session_id, self.controller.route_mode_pcb)
        self._record_step(
            session_id,
            "import",
            "import_requested",
            {"confirmText": event.raw_user_text or "", "rerouteFiles": {"importLinesFilePath": import_file}},
            event_type="user_jump",
            intent="confirm_import",
            action_type="tool_call_request",
            checkpoint_label="reroute import requested",
        )
        return RerouteChainResult(
            decision=self._turn_decision_cls()(
                mode=self.controller.route_mode_pcb,
                reason="reroute_import_request",
                intent="confirm_import",
                tool_call={"name": "importLines", "arguments": {"filePath": import_file}, "timeout": 360.0},
            )
        )

    def _reject_import(self, event: "SWSDTurnEvent", plan: "WorkflowActionPlan") -> RerouteChainResult:
        session_id = event.session_id
        self.adapter._set_session_mode(session_id, self.controller.route_mode_pcb)
        self._record_step(
            session_id,
            plan.workflow_state or "report",
            "reject_import",
            {"rejectText": event.raw_user_text or ""},
            event_type="user_jump",
            intent="reject_import",
            action_type="user_jump",
            checkpoint_label="reroute reject import",
        )
        return RerouteChainResult(
            decision=self._turn_decision_cls()(
                mode=self.controller.route_mode_pcb,
                reason="reject_import",
                intent=self.controller.intent_pcb_followup,
                immediate_reply="已取消导入。你想重新拆线重布，还是切换到其他 PCB 流程？",
            )
        )

    async def handle_delete_result(self, data: Dict[str, Any], result: Any) -> None:
        session_id, project_id = self._session_project_from_tool_result(data)
        if not session_id:
            return
        self.adapter._set_session_mode(session_id, self.controller.route_mode_pcb)
        self.adapter._set_flow_state(session_id, self.controller.flow_reroute)
        if isinstance(result, dict):
            self._cache_project_data_from_delete_result(session_id, result)
            self._cache_reroute_context_for_tools(session_id, project_id, result)
        self._record_step(
            session_id,
            "confirm",
            "rip_up_complete",
            {
                "deleteTracesResult": result,
                "projectData": self._project_data_from_delete_result(result),
                "rerouteFiles": self._reroute_files_from_payload(result),
            },
            event_type="tool_result",
            intent="ripup_complete",
            action_type="tool_result",
            checkpoint_label="reroute rip-up complete",
        )
        await self.adapter.send(
            chat_id=session_id,
            content="已完成拆线准备。请确认是否继续生成局部 reroute，或回复取消/重新拆线。",
            metadata={"is_final": True},
        )

    async def handle_reroute_result(self, data: Dict[str, Any], result: Any) -> None:
        session_id, _project_id = self._session_project_from_tool_result(data)
        if not session_id:
            return
        self.adapter._set_session_mode(session_id, self.controller.route_mode_pcb)
        payload = self._coerce_tool_payload(result)
        visible = str(payload.get("content") or payload.get("report") or payload.get("explanation") or "")
        fields = SWSDResponseBuilder.reroute_final(payload, visible_text=visible)
        report_markdown = build_reroute_markdown_report(fields, visible_text=visible)
        fields["report"] = report_markdown
        self._remember_reroute_fields(session_id, fields)
        drc_passed = self._drc_passed(fields)
        next_state = "report" if drc_passed else "drc_loop"
        intent = "drc_passed" if drc_passed else "drc_failed"
        self._record_step(
            session_id,
            next_state,
            "reroute_report" if drc_passed else "reroute_drc_failed",
            {
                "rerouteResultPayload": fields,
                "rerouteFiles": self._reroute_files_from_payload(fields),
                "projectData": self._project_data_from_reroute_payload(fields),
            },
            event_type="workflow_action",
            intent=intent,
            action_type="tool_result_continuation",
            checkpoint_label="reroute report" if drc_passed else "reroute drc failed",
        )
        if drc_passed:
            await self.adapter.send(chat_id=session_id, content=report_markdown, metadata={"is_final": True})
            return
        await self.adapter.send(
            chat_id=session_id,
            content=report_markdown + "\n\nDRC 未通过。你可以重新拆线重布，或切换到其他流程。",
            metadata={"is_final": True},
        )

    async def handle_import_result(self, data: Dict[str, Any], result: Any) -> None:
        session_id, _project_id = self._session_project_from_tool_result(data)
        if not session_id:
            return
        status = self._format_import_status(result)
        self._record_step(
            session_id,
            "import",
            "import_complete",
            {"importResult": result, "importStatus": status},
            event_type="tool_result",
            intent="complete",
            action_type="tool_result",
            checkpoint_label="reroute import complete",
        )
        await self.adapter.send(chat_id=session_id, content=status, metadata={"is_final": True})
        await self._request_project_data_refresh(session_id)

    async def handle_project_data_refresh_result(self, data: Dict[str, Any], result: Any) -> None:
        session_id, _project_id = self._session_project_from_tool_result(data)
        if not session_id:
            return
        self._record_step(
            session_id,
            "idle",
            "project_data_refreshed_after_import",
            {"projectData": self._project_data_payload(status="imported", result=result)},
            event_type="tool_result",
            intent="complete",
            action_type="tool_result",
            checkpoint_label="reroute project data refreshed",
        )

    def _record_step(self, session_id: str, state: str, step_id: str, payload: Dict[str, Any], **kwargs: Any) -> None:
        if hasattr(self.bridge, "reroute_payload"):
            full_payload = self.bridge.reroute_payload(session_id, payload)
        else:
            full_payload = dict(payload or {})
        if self.state is not None and hasattr(self.state, "record_step"):
            self.state.record_step(session_id, self.workflow_id, state=state, step_id=step_id, payload=full_payload, **kwargs)
            return
        update = getattr(self.adapter, "_swsd_update", None)
        if callable(update):
            update(session_id, self.workflow_id, state, full_payload, **kwargs)
        return

    def _state_payload(self, session_id: str) -> Dict[str, Any]:
        state = self.state.load(session_id, self.workflow_id) if self.state is not None and hasattr(self.state, "load") else None
        payload = state.get("state_payload") if isinstance(state, dict) and isinstance(state.get("state_payload"), dict) else {}
        return dict(payload)

    def _remember_reroute_fields(self, session_id: str, fields: Dict[str, Any]) -> None:
        remember = getattr(self.bridge, "_remember_reroute_fields", None)
        if callable(remember):
            remember(session_id, fields)
            return
        cache = getattr(self.adapter, "_last_direct_reroute_fields", None) or {}
        cache[session_id] = fields
        setattr(self.adapter, "_last_direct_reroute_fields", cache)

    @staticmethod
    def _session_project_from_tool_result(data: Dict[str, Any]) -> tuple[str, str]:
        body = data.get("body", {}) if isinstance(data, dict) else {}
        session_id = str(body.get("sessionId") or data.get("sessionId") or "").strip()
        project_id = str(body.get("projectid") or body.get("projectID") or data.get("projectid") or data.get("projectID") or "").strip()
        return session_id, project_id

    @staticmethod
    def _cache_reroute_context_for_tools(session_id: str, project_id: str, result: Dict[str, Any]) -> None:
        try:
            from tools import pcb_tools

            transport = pcb_tools.WebSocketTransportSingleton.get_instance()
            transport.current_session_id = session_id
            transport.set_session_mode(session_id, "pcb")
            if project_id:
                transport.bind_project(session_id, project_id)
            transport.cache_reroute_context(result, session_id=session_id)
        except Exception:
            return

    @staticmethod
    def _coerce_tool_payload(result: Any) -> Dict[str, Any]:
        if isinstance(result, dict):
            return dict(result)
        if isinstance(result, str):
            try:
                parsed = json.loads(result)
            except json.JSONDecodeError:
                return {"explanation": result.strip()}
            return parsed if isinstance(parsed, dict) else {"explanation": str(parsed)}
        return {"explanation": str(result or "")}

    @staticmethod
    def _drc_passed(fields: Dict[str, Any]) -> bool:
        check_report = fields.get("checkReport") if isinstance(fields.get("checkReport"), dict) else {}
        reroute_result = fields.get("rerouteResult") if isinstance(fields.get("rerouteResult"), dict) else {}
        return check_report.get("passed") is True or reroute_result.get("drcPassed") is True

    def _import_file_from_payload(self, payload: Dict[str, Any]) -> str:
        files = payload.get("rerouteFiles") if isinstance(payload.get("rerouteFiles"), dict) else {}
        if files.get("importLinesFilePath"):
            return str(files.get("importLinesFilePath") or "").strip()
        final = payload.get("rerouteResultPayload") if isinstance(payload.get("rerouteResultPayload"), dict) else {}
        return str(final.get("importLinesFilePath") or (final.get("rerouteResult") or {}).get("importLinesFilePath") or "").strip()

    @staticmethod
    def _reroute_files_from_payload(payload: Any) -> Dict[str, Any]:
        data = payload if isinstance(payload, dict) else {}
        reroute_result = data.get("rerouteResult") if isinstance(data.get("rerouteResult"), dict) else {}
        return {
            "originalBoardDataFilePath": data.get("originalBoardDataFilePath") or reroute_result.get("originalBoardDataFilePath") or "",
            "droppedBoardDataFilePath": data.get("droppedBoardDataFilePath") or reroute_result.get("droppedBoardDataFilePath") or "",
            "routedLayoutTxtFilePath": data.get("routedLayoutTxtFilePath") or reroute_result.get("routedLayoutTxtFilePath") or "",
            "importLinesFilePath": data.get("importLinesFilePath") or reroute_result.get("importLinesFilePath") or "",
            "markdownReportPath": data.get("markdownReportPath") or "",
        }

    @staticmethod
    def _project_data_payload(*, status: str, result: Any = None) -> Dict[str, Any]:
        text = str(result or "").strip()
        absolute = text if text and Path(text).is_absolute() else ""
        relative = "" if absolute else text
        return {"relative_path": relative, "absolute_path": absolute, "status": status, "source": "getProjectData"}

    def _project_data_from_delete_result(self, result: Any) -> Dict[str, Any]:
        if isinstance(result, dict):
            for key in ("projectDataFilePath", "projectDataPath", "filePath", "boardDataFilePath"):
                if result.get(key):
                    return self._project_data_payload(status="loaded", result=result.get(key))
            if result.get("projectData"):
                return self._project_data_payload(status="loaded", result=result.get("projectData"))
        return self._project_data_payload(status="loaded")

    def _project_data_from_reroute_payload(self, fields: Dict[str, Any]) -> Dict[str, Any]:
        reroute_result = fields.get("rerouteResult") if isinstance(fields.get("rerouteResult"), dict) else {}
        return self._project_data_payload(status="loaded", result=reroute_result.get("routedBoardDataFilePath") or fields.get("routedBoardDataFilePath") or "")

    def _cache_project_data_from_delete_result(self, session_id: str, result: Dict[str, Any]) -> None:
        project_data = result.get("projectData")
        cache = getattr(self.adapter, "_session_project_data", None)
        if isinstance(cache, dict) and project_data:
            cache[session_id] = project_data

    async def _request_project_data_refresh(self, session_id: str) -> None:
        call_id = f"swsd_reroute_get_project_{uuid.uuid4().hex[:8]}"
        self._record_step(
            session_id,
            "import",
            "get_project_data_after_import_requested",
            {"projectData": self._project_data_payload(status="requested")},
            event_type="workflow_action",
            intent="getProjectData",
            action_type="tool_call_request",
        )
        try:
            await self.adapter.send_tool_call(session_id=session_id, call_id=call_id, tool_name="getProjectData", arguments={}, timeout=360.0)
        except Exception:
            return

    @staticmethod
    def _format_import_status(result: Any) -> str:
        if isinstance(result, dict):
            if result.get("success") is False or result.get("ok") is False:
                return f"已调用导入工具，但前端返回失败：{result.get('message') or result.get('error') or result}"
            return f"导入完成。{result.get('message') or result.get('status') or ''}".strip()
        text = str(result or "").strip()
        return f"导入完成。{text}".strip() if text else "导入完成。"
