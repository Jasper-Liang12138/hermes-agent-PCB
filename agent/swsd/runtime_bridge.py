"""Runtime bridge between SWSD state and WebSocket adapter session caches."""

from __future__ import annotations

import asyncio
import json
import logging

from typing import Any, Dict

from agent.swsd.response_builder import SWSDResponseBuilder

logger = logging.getLogger(__name__)


class WebSocketSWSDRuntimeBridge:
    def __init__(
        self,
        adapter,
        *,
        escape_flow_id: str,
        reroute_flow_id: str,
        flow_idle: str,
        flow_wait_selection: str,
        flow_wait_router_type: str,
        flow_wait_confirm: str,
        flow_routing: str,
        flow_reroute: str,
    ) -> None:
        self.adapter = adapter
        self.escape_flow_id = escape_flow_id
        self.reroute_flow_id = reroute_flow_id
        self.flow_idle = flow_idle
        self.flow_wait_selection = flow_wait_selection
        self.flow_wait_router_type = flow_wait_router_type
        self.flow_wait_confirm = flow_wait_confirm
        self.flow_routing = flow_routing
        self.flow_reroute = flow_reroute

    def escape_payload(self, session_id: str, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "legacyFlowState": self.adapter._session_flow_states.get(session_id, self.flow_idle),
            "selection": list(self.adapter._session_bga_selection.get(session_id) or ()),
            "selectedBGA": self.adapter._session_selected_targets.get(session_id, ""),
            "requestedBGA": self.adapter._session_requested_bga_targets.get(session_id, ""),
            "routerType": self.adapter._session_router_types.get(session_id, ""),
            "routeAlgorithm": self.adapter._session_route_algorithms.get(session_id, ""),
            "fanoutModule": self.adapter._session_fanout_modules.get(session_id, ""),
            "fanoutParams": self.adapter._session_fanout_params.get(session_id, {}),
            "boardSummary": self.adapter._session_board_summaries.get(session_id, {}),
            "currentLayoutVersion": self.adapter._session_layout_versions.get(session_id),
            "activeParamsVersion": self.adapter._session_active_params_versions.get(session_id),
            "fanoutContext": self.adapter._session_fanout_contexts.get(session_id, {}),
        }
        if extra:
            payload.update(extra)
        return payload

    def reroute_payload(self, session_id: str, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "legacyFlowState": self.adapter._session_flow_states.get(session_id, self.flow_idle),
            "selection": list(self.adapter._session_bga_selection.get(session_id) or ()),
            "selectedBGA": self.adapter._session_selected_targets.get(session_id, ""),
            "requestedBGA": self.adapter._session_requested_bga_targets.get(session_id, ""),
            "routerType": self.adapter._session_router_types.get(session_id, ""),
            "routeAlgorithm": self.adapter._session_route_algorithms.get(session_id, ""),
            "fanoutModule": self.adapter._session_fanout_modules.get(session_id, ""),
            "fanoutParams": self.adapter._session_fanout_params.get(session_id, {}),
            "boardSummary": self.adapter._session_board_summaries.get(session_id, {}),
            "fanoutContext": self.adapter._session_fanout_contexts.get(session_id, {}),
            "currentLayoutVersion": self.adapter._session_layout_versions.get(session_id),
            "activeParamsVersion": self.adapter._session_active_params_versions.get(session_id),
        }
        if extra:
            payload.update(extra)
        return payload

    def _remember_reroute_fields(self, session_id: str, fields: Dict[str, Any]) -> None:
        cache = getattr(self.adapter, "_last_direct_reroute_fields", None)
        if cache is None:
            cache = {}
            setattr(self.adapter, "_last_direct_reroute_fields", cache)
        cache[session_id] = fields
    async def handle_reroute_delete_result(self, data: Dict[str, Any], result: Any) -> None:
        """Advance reroute after deleteTracesForRerouting returns.

        SWSD owns this continuation edge:
        rip_up + deleteTracesForRerouting.result -> complete_reroute -> report.
        The WebSocket adapter only delivers the event.
        """
        body = data.get("body", {}) if isinstance(data, dict) else {}
        session_id = str(body.get("sessionId") or data.get("sessionId") or "").strip()
        project_id = str(
            body.get("projectid")
            or body.get("projectID")
            or data.get("projectid")
            or data.get("projectID")
            or ""
        ).strip()
        if not session_id:
            return

        adapter = self.adapter
        adapter._set_session_mode(session_id, "pcb")
        adapter._set_flow_state(session_id, self.flow_reroute)
        adapter._swsd_update(
            session_id,
            self.reroute_flow_id,
            "reroute_llm",
            self.reroute_payload(session_id, {"deleteTracesResult": result}),
            event_type="tool_result",
            intent="complete_reroute",
            action_type="tool_result",
            checkpoint_label="reroute delete result",
        )

        try:
            from tools import pcb_tools

            transport = pcb_tools.WebSocketTransportSingleton.get_instance()
            transport.current_session_id = session_id
            transport.set_session_mode(session_id, "pcb")
            if project_id:
                transport.bind_project(session_id, project_id)
            if isinstance(result, dict):
                transport.cache_reroute_context(result, session_id=session_id)

            reroute_request = {"localRerouteCompletionPolicy": {"mode": "selected_net_local_first"}}
            reroute_json = await asyncio.to_thread(
                pcb_tools.reroute,
                json.dumps(reroute_request, ensure_ascii=False),
                session_id,
            )
            try:
                reroute_payload = json.loads(reroute_json) if isinstance(reroute_json, str) else {}
            except json.JSONDecodeError:
                reroute_payload = {"explanation": str(reroute_json or "").strip()}
            if not isinstance(reroute_payload, dict):
                reroute_payload = {"explanation": str(reroute_payload or "").strip()}

            visible = str(
                reroute_payload.get("content")
                or reroute_payload.get("report")
                or reroute_payload.get("explanation")
                or ""
            )
            reroute_payload = SWSDResponseBuilder.reroute_final(reroute_payload, visible_text=visible)
            self._remember_reroute_fields(session_id, reroute_payload)
            adapter._swsd_update(
                session_id,
                self.reroute_flow_id,
                "report",
                self.reroute_payload(session_id, {"rerouteResultPayload": reroute_payload}),
                event_type="workflow_action",
                intent="complete_reroute",
                action_type="tool_result_continuation",
                checkpoint_label="reroute complete",
            )
            content = adapter._fallback_visible_content_for_fields("", reroute_payload)
            await adapter.send(
                chat_id=session_id,
                content=content,
                metadata={"is_final": True, "pcb_fields": reroute_payload},
            )
        except Exception as exc:
            logger.exception("Failed completing SWSD reroute after deleteTracesForRerouting result: %s", exc)
            fields = SWSDResponseBuilder.recoverable_reroute_error(str(exc), status="reroute_finalize_failed")
            self._remember_reroute_fields(session_id, fields)
            adapter._swsd_update(
                session_id,
                self.reroute_flow_id,
                "report",
                self.reroute_payload(session_id, {"rerouteResultPayload": fields, "completionError": str(exc)}),
                event_type="workflow_action",
                intent="complete_reroute_failed",
                action_type="tool_result_continuation",
                checkpoint_label="reroute complete failed",
            )
            await adapter.send(
                chat_id=session_id,
                content=adapter._fallback_visible_content_for_fields("拆线重布未能完成。", fields),
                metadata={"is_final": True, "pcb_fields": fields},
            )
    def swsd_state_from_legacy_flow(self, flow_state: str) -> tuple[str, str]:
        if flow_state == self.flow_reroute:
            return self.reroute_flow_id, "rip_up"
        if flow_state == self.flow_wait_selection:
            return self.escape_flow_id, "select_bga"
        if flow_state == self.flow_wait_router_type:
            return self.escape_flow_id, "layer_assign_escape_order"
        if flow_state == self.flow_wait_confirm:
            return self.escape_flow_id, "review"
        if flow_state == self.flow_routing:
            return self.escape_flow_id, "routing"
        return self.escape_flow_id, "idle"

    def legacy_flow_for_workflow_state(self, workflow_id: str, state: str) -> str:
        if workflow_id == self.reroute_flow_id:
            if state == "rip_up":
                return self.flow_reroute
            return self.flow_idle
        if state == "select_bga":
            return self.flow_wait_selection
        if state == "layer_assign":
            return self.flow_wait_router_type
        if state == "escape_order":
            return self.flow_wait_confirm
        if state == "review":
            return self.flow_wait_confirm
        if state == "import":
            return self.flow_wait_confirm
        if state == "routing":
            return self.flow_routing
        return self.flow_idle

    def restore_workflow_context_from_state(self, session_id: str, workflow_id: str) -> None:
        if not self.adapter._swsd_enabled:
            return
        state = self.adapter._swsd_state.load(session_id, workflow_id)
        if not state:
            return
        payload = state.get("state_payload") if isinstance(state.get("state_payload"), dict) else {}
        selection = payload.get("selection")
        if isinstance(selection, list):
            self.adapter._session_bga_selection[session_id] = tuple(
                item for item in selection if isinstance(item, dict)
            )
            labels = self.adapter._selection_labels_from_items(selection)
            if labels:
                self.adapter._session_selection_labels[session_id] = tuple(labels)
        selected = payload.get("selectedBGA")
        if isinstance(selected, str) and selected.strip():
            self.adapter._session_selected_targets[session_id] = selected.strip()
        requested = str(payload.get("requestedBGA") or "").strip()
        if requested:
            self.adapter._session_requested_bga_targets[session_id] = requested
        router_type = str(payload.get("routerType") or "").strip()
        if router_type:
            self.adapter._session_router_types[session_id] = router_type
        route_algorithm = str(payload.get("routeAlgorithm") or "").strip()
        if route_algorithm:
            self.adapter._session_route_algorithms[session_id] = route_algorithm
        fanout_module = str(payload.get("fanoutModule") or "").strip()
        if fanout_module:
            self.adapter._session_fanout_modules[session_id] = fanout_module
        fanout_params = payload.get("fanoutParams")
        if isinstance(fanout_params, dict):
            self.adapter._session_fanout_params[session_id] = dict(fanout_params)
        active_params_version = payload.get("activeParamsVersion")
        if active_params_version not in (None, ""):
            self.adapter._session_active_params_versions[session_id] = active_params_version
        board_summary = payload.get("boardSummary")
        if isinstance(board_summary, dict):
            self.adapter._session_board_summaries[session_id] = dict(board_summary)
        fanout_context = payload.get("fanoutContext")
        if isinstance(fanout_context, dict):
            self.adapter._session_fanout_contexts[session_id] = dict(fanout_context)
        current_layout_version = payload.get("currentLayoutVersion")
        if current_layout_version not in (None, ""):
            self.adapter._session_layout_versions[session_id] = current_layout_version



