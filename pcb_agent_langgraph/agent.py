from __future__ import annotations

import json
import re
import uuid
from typing import Any

from pcb_agent_langgraph.debug_logging import AgentDebugLogger, agent_debug_context
from pcb_agent_langgraph.entry import entry_task_workflow, normalize_entry_module
from pcb_agent_langgraph.graph.graph import build_graph
from pcb_agent_langgraph.graph.state import ChatMessage, PCBState, initial_state
from pcb_agent_langgraph.models.pcb_model import PCBModel
from pcb_agent_langgraph.planner.intent_entities import extract_fanout_entities, normalize_router_type
from pcb_agent_langgraph.tools.frontend import FrontendToolSender, ProgressSender
from pcb_agent_langgraph.tools.registry import build_tool_registry
from pcb_agent_langgraph.utils.config import AppConfig


# ====== 功能：封装 LangGraph PCB Agent 的会话状态和图调用入口。 ======
class PCBLangGraphAgent:
    # Agent 只负责会话状态续接和图调用；流程推进交给 LangGraph 节点与 planner。
    # ====== 功能：初始化对象并保存运行所需依赖。 ======
    def __init__(self, config: AppConfig, frontend_sender: FrontendToolSender | None = None, *, progress_sender: ProgressSender | None = None, use_model_planner: bool = True, require_model_planner: bool = False) -> None:
        self.config = config
        self.model = PCBModel(config.model)
        self.tools = build_tool_registry(config, frontend_sender)
        self.graph = build_graph(self.model, self.tools, progress_sender=progress_sender, use_model_planner=use_model_planner, require_model_planner=require_model_planner, config=config)
        self._session_states: dict[str, PCBState] = {}

    # ====== 功能：异步执行当前工具或 Agent 调用。 ======
    async def ainvoke(self, session_id: str, project_id: str, user_input: str, *, entry_module: str = "", entry_action: str = "", entry_payload: dict[str, Any] | None = None) -> PCBState:
        run_id = str(uuid.uuid4())
        logger = AgentDebugLogger(self.config.debug_log, run_id=run_id, session_id=session_id, project_id=project_id, root=self.config.root)
        with agent_debug_context(logger):
            logger.log("turn.start", {"user_input": user_input, "entry_module": entry_module, "entry_action": entry_action, "entry_payload": entry_payload or {}})
            try:
                previous = self._session_states.get(session_id)
                history: list[ChatMessage] = list(previous.get("conversation_history", [])) if previous else []
                state = initial_state(session_id=session_id, project_id=project_id, user_input=user_input, history=history)
                normalized_entry = normalize_entry_module(entry_module)
                payload = dict(entry_payload or {})
                if previous:
                    # 多轮对话复用上轮项目状态和中间 cache，使 fanout/reroute 可以被追问后继续执行。
                    state["pcb_project"] = previous.get("pcb_project", {})
                    state["design_state"] = previous.get("design_state", {})
                    state["intermediate_cache"] = self._cache_for_turn(previous.get("intermediate_cache", {}), user_input, previous.get("workflow_state", "idle"))
                    state["workflow_id"] = previous.get("workflow_id", "idle")
                    state["workflow_state"] = previous.get("workflow_state", "idle")
                if normalized_entry:
                    state["entry_module"] = normalized_entry
                    state["entry_action"] = str(entry_action or "")
                    payload["entry_module"] = normalized_entry
                    if entry_action:
                        payload["entry_action"] = str(entry_action)
                    state["entry_payload"] = payload
                    if normalized_entry == "global_fanout":
                        state["intermediate_cache"] = self._cache_for_entry_payload(state.get("intermediate_cache", {}), payload)
                    task_workflow = entry_task_workflow(normalized_entry)
                    if task_workflow:
                        task_type, workflow_id = task_workflow
                        previous_workflow = previous.get("workflow_id", "idle") if previous else "idle"
                        state["task_type"] = task_type
                        state["workflow_id"] = workflow_id
                        if previous_workflow != workflow_id:
                            state["workflow_state"] = "idle"
                result = await self.graph.ainvoke(state)
                self._session_states[session_id] = result
                logger.log("turn.end", {"state": result})
                return result
            except Exception as exc:
                logger.log("turn.error", {"error": str(exc)})
                raise


    @staticmethod
    # ====== 功能：把前端入口 payload 中的 fanout 结构化字段写入 cache。 ======
    def _cache_for_entry_payload(cache: dict, entry_payload: dict[str, Any]) -> dict:
        return _cache_for_entry_payload(cache, entry_payload)


    @staticmethod
    # ====== 功能：执行 _cache_for_turn 的核心逻辑。 ======
    def _cache_for_turn(cache: dict, user_input: str, workflow_state: str = "idle") -> dict:
        text = user_input.lower()
        next_cache = dict(cache or {})
        entities = dict(next_cache.get("fanoutEntities") or {})
        user_entities = extract_fanout_entities(user_input)
        router_type = _router_choice_from_text(user_input)

        # fanout 相关实体必须跨轮保存。比如用户第一轮说“U5”，第二轮只回复“135”，
        # 后续 layer_assign 仍然要知道目标 BGA；线宽线距同理。
        fanout_signal = _is_fanout_signal(user_input, user_entities, router_type)
        if fanout_signal:
            entities = _merge_fanout_entities(entities, user_entities)
            if router_type:
                entities["routerType"] = router_type
            if entities:
                next_cache["fanoutEntities"] = entities
            for key in ("fanout_routeResult", "importLinesResult", "importLinesRejected", "importLinesRejectedReason", "fanoutParamsConfirmed"):
                next_cache.pop(key, None)

        # BGA 选择阶段，用户/前端只要回复 U5/Uxx，就写入目标器件并保留原有约束。
        if workflow_state == "select_bga" and user_entities.get("selectedBGA"):
            selected = str(user_entities["selectedBGA"]).upper()
            entities.update({"selectedBGA": selected, "targetBGAs": [selected], "bgaSelectionConfirmed": True})
            next_cache["fanoutEntities"] = entities

        # router 选择阶段只接受 135 或 arc 两个可见选项，内部统一为 rule_135/rule_arc。
        if workflow_state == "wait_router_type" and router_type:
            entities["routerType"] = router_type
            next_cache["fanoutEntities"] = entities

        # 参数确认阶段兼容前端 parameter-config-result 的 JSON 字符串/对象形态。
        confirmed_params = _parse_fanout_param_confirmation(user_input)
        if workflow_state == "param_review" and confirmed_params is not None:
            merged_params = _merge_fanout_params(dict(next_cache.get("fanoutParams") or {}), confirmed_params)
            next_cache["fanoutParams"] = merged_params
            next_cache["fanoutParamsConfirmed"] = True
            merged_entities = dict(next_cache.get("fanoutEntities") or {})
            for key in ("selectedBGA", "targetBGA", "targetBGAs", "bgaType", "bgaLayoutType", "routerType", "constraints"):
                value = merged_params.get(key)
                if value not in (None, "", [], {}):
                    merged_entities[key] = value
            next_cache["fanoutEntities"] = merged_entities

        # 参数确认阶段允许用户直接修改线宽、线距或 router 类型，需要回到参数生成节点。
        if workflow_state == "param_review" and confirmed_params is None and (any(token in user_input for token in ("线宽", "线距", "间距")) or any(token in text for token in ("width", "spacing", "135", "arc"))):
            for key in ("layerAssignResult", "escapeOrderResult", "fanout_routeResult", "importLinesResult", "drcResult", "explainabilityReport"):
                next_cache.pop(key, None)
            edited_entities = dict(next_cache.get("fanoutEntities") or {})
            edited_entities = _merge_fanout_entities(edited_entities, user_entities)
            if router_type:
                edited_entities["routerType"] = router_type
            next_cache["fanoutEntities"] = edited_entities
            next_cache.pop("fanoutParamsConfirmed", None)
        # 用户要求重来时，仅清理对应流程的阶段性结果，保留项目数据和其他上下文。
        if any(token in user_input for token in ("重新", "重来", "再来", "不满意")) or any(token in text for token in ("rerun", "again")):
            if any(token in text for token in ("fanout",)) or any(token in user_input for token in ("逃逸", "扇出")):
                for key in ("layerAssignResult", "escapeOrderResult", "fanout_routeResult", "importLinesResult", "importLinesRejected", "importLinesRejectedReason", "drcResult", "explainabilityReport", "fanoutParamsConfirmed"):
                    next_cache.pop(key, None)
            if any(token in text for token in ("reroute", "rip-up", "ripup")) or any(token in user_input for token in ("拆线", "重布")):
                for key in ("deleteTracesResult", "rerouteInput", "localRouteCsvPath", "rerouteContext", "rerouteLoopResult", "rerouteResult", "lastRerouteResult", "rerouteStartedAt", "rerouteAttemptCount", "rerouteDrcFailureCount", "rerouteDrcFeedbackHistory", "rerouteUnavailable", "rerouteUnavailableReason", "helpPlannerResult", "importLinesResult", "helperDrcResult", "drcResult", "lastDrcResult", "explainabilityReport"):
                    next_cache.pop(key, None)
        return next_cache
# ====== 功能：判断用户本轮是否在继续或重新发起 fanout。 ======
def _is_fanout_signal(user_input: str, entities: dict[str, Any], router_type: str) -> bool:
    text = str(user_input or "").lower()
    if entities or router_type:
        return True
    return any(token in user_input for token in ("逃逸", "扇出", "线宽", "线距", "间距")) or any(
        token in text for token in ("fanout", "escape", "width", "spacing")
    )


# ====== 功能：把前端结构化 fanout 入口数据合并到中间缓存。 ======
def _cache_for_entry_payload(cache: dict, entry_payload: dict[str, Any]) -> dict:
    payload = entry_payload if isinstance(entry_payload, dict) else {}
    next_cache = dict(cache or {})
    entities = dict(next_cache.get("fanoutEntities") or {})
    action = str(payload.get("entry_action") or payload.get("action") or "").strip()

    candidates = _payload_bga_candidates(payload)
    if candidates:
        next_cache["bgaCandidates"] = candidates

    selected = _payload_text(payload, ("selectedBGA", "targetBGA", "componentId", "refdes"))
    if selected:
        selected = selected.upper()
        entities["selectedBGA"] = selected
        entities["targetBGAs"] = [selected]
        entities["bgaSelectionConfirmed"] = True

    bga_type = _normalize_bga_type_text(_payload_text(payload, ("bgaType", "bga_type", "bgaLayoutType", "layoutType")))
    if bga_type:
        entities["bgaType"] = bga_type
        entities["bgaLayoutType"] = bga_type

    router_type = normalize_router_type(_payload_text(payload, ("routerType", "algorithm", "router", "routeType")))
    if router_type:
        entities["routerType"] = router_type

    constraints = payload.get("constraints") if isinstance(payload.get("constraints"), dict) else {}
    if constraints:
        entities["constraints"] = dict(constraints)

    if entities:
        next_cache["fanoutEntities"] = entities

    fanout_params = _fanout_params_from_payload(payload)
    if fanout_params:
        merged_params = _merge_fanout_params(dict(next_cache.get("fanoutParams") or {}), fanout_params)
        next_cache["fanoutParams"] = merged_params
        merged_entities = dict(next_cache.get("fanoutEntities") or {})
        for key in ("selectedBGA", "targetBGA", "targetBGAs", "bgaType", "bgaLayoutType", "routerType", "constraints"):
            value = merged_params.get(key)
            if value not in (None, "", [], {}):
                merged_entities[key] = value
        if merged_entities:
            next_cache["fanoutEntities"] = merged_entities

    if _is_fanout_param_confirm_action(action) or payload.get("fanoutParamsConfirmed") is True:
        next_cache["fanoutParamsConfirmed"] = True
    return next_cache


# ====== 功能：从入口 payload 中兼容读取 BGA 候选列表。 ======
def _payload_bga_candidates(payload: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("bgaCandidates", "bgaList", "bgaComponents", "components"):
        value = payload.get(key)
        if isinstance(value, list):
            return [dict(item) for item in value if isinstance(item, dict)]
    return []


# ====== 功能：把开发文档中的 bga/layout/rules/routingPlan 映射到 fanoutParams。 ======
def _fanout_params_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    explicit = payload.get("fanoutParams") if isinstance(payload.get("fanoutParams"), dict) else {}
    if explicit:
        params = _merge_fanout_params(params, explicit)

    bga = payload.get("bga") if isinstance(payload.get("bga"), dict) else {}
    selected = _payload_text(payload, ("selectedBGA", "targetBGA", "componentId", "refdes"))
    if selected:
        params["selectedBGA"] = selected.upper()
        params["targetBGAs"] = [selected.upper()]
    if bga:
        params["bga"] = dict(bga)
        if bga.get("pinCount") not in (None, "", [], {}):
            params["pinCount"] = bga.get("pinCount")
        if bga.get("name") not in (None, "", [], {}):
            params["bgaName"] = bga.get("name")

    bga_type = _normalize_bga_type_text(_payload_text(payload, ("bgaType", "bga_type", "bgaLayoutType", "layoutType")))
    if bga_type:
        params["bgaType"] = bga_type
        params["bgaLayoutType"] = bga_type

    routing_plan = payload.get("routingPlan") if isinstance(payload.get("routingPlan"), dict) else {}
    router_type = normalize_router_type(_payload_text(payload, ("routerType", "algorithm", "router", "routeType"))) or normalize_router_type(_first_text_from_dict(routing_plan, ("routerType", "algorithm", "router", "routeType")))
    if router_type:
        params["routerType"] = router_type

    constraints = _merge_constraints_from_payload(payload, routing_plan)
    if constraints:
        params["constraints"] = constraints

    layout = payload.get("layout") if isinstance(payload.get("layout"), dict) else {}
    if layout:
        params["layout"] = dict(layout)
        for source_key, target_key in (("boardId", "boardId"), ("snapshotId", "snapshotId")):
            if layout.get(source_key) not in (None, "", [], {}):
                params[target_key] = layout.get(source_key)

    rules = payload.get("rules") if isinstance(payload.get("rules"), dict) else {}
    if rules:
        params["rules"] = dict(rules)
        rule_id = rules.get("ruleManagerConfigId") or rules.get("ruleConfigId") or rules.get("id")
        if rule_id not in (None, "", [], {}):
            params["ruleManagerConfigId"] = rule_id

    network_info = _first_payload_value(payload, ("networkInfo", "networks", "nets"))
    if network_info not in (None, "", [], {}):
        params["networkInfo"] = network_info

    if routing_plan:
        params["routingPlan"] = dict(routing_plan)
        for source_key, target_key in (("layerAssignment", "layerAssignment"), ("escapeOrder", "escapeOrder"), ("orderLines", "orderLines")):
            value = routing_plan.get(source_key)
            if value not in (None, "", [], {}):
                params[target_key] = value
        if "orderLines" not in params and routing_plan.get("escapeOrder") not in (None, "", [], {}):
            params["orderLines"] = routing_plan.get("escapeOrder")

    task_id = payload.get("taskId")
    if task_id not in (None, "", [], {}):
        params["taskId"] = task_id
    return {key: value for key, value in params.items() if value not in (None, "", [], {})}


# ====== 功能：从 payload/routingPlan/rules 中合并前端约束。 ======
def _merge_constraints_from_payload(payload: dict[str, Any], routing_plan: dict[str, Any]) -> dict[str, Any]:
    constraints: dict[str, Any] = {}
    for source in (
        payload.get("constraints") if isinstance(payload.get("constraints"), dict) else {},
        routing_plan.get("constraints") if isinstance(routing_plan.get("constraints"), dict) else {},
        (payload.get("rules") or {}).get("constraints") if isinstance(payload.get("rules"), dict) and isinstance((payload.get("rules") or {}).get("constraints"), dict) else {},
    ):
        constraints.update({key: value for key, value in source.items() if value not in (None, "", [], {})})
    return constraints


# ====== 功能：判断前端动作是否代表 fanout 参数已确认。 ======
def _is_fanout_param_confirm_action(action: str) -> bool:
    normalized = action.replace("-", "_").lower()
    return normalized in {"confirm", "confirm_params", "submit_params", "parameter_config_result", "start_routing", "start_route", "start_fanout"}


# ====== 功能：从 payload 或嵌套 bga 对象中读取第一个非空文本字段。 ======
def _payload_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str:
    sources = [payload]
    for nested_key in ("bga", "selected", "selection"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            sources.append(nested)
    for source in sources:
        for key in keys:
            value = source.get(key)
            if value not in (None, "", [], {}):
                return str(value).strip()
    return ""


# ====== 功能：从字典中读取第一个非空文本字段。 ======
def _first_text_from_dict(source: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = source.get(key)
        if value not in (None, "", [], {}):
            return str(value).strip()
    return ""


# ====== 功能：读取 payload 顶层第一个非空对象字段。 ======
def _first_payload_value(payload: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = payload.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


# ====== 功能：统一前端传入的 BGA 类型命名。 ======
def _normalize_bga_type_text(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if text in {"rect", "rectangle", "rectangular", "矩形", "正交", "regular"}:
        return "rectangular"
    if text in {"stagger", "staggered", "交错", "错列"}:
        return "staggered"
    return text


# ====== 功能：增量合并 fanout 实体，constraints 不整体覆盖。 ======
def _merge_fanout_entities(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base or {})
    for key, value in (updates or {}).items():
        if value in (None, "", [], {}):
            continue
        if key == "constraints" and isinstance(value, dict):
            constraints = dict(merged.get("constraints") or {})
            constraints.update({k: v for k, v in value.items() if v not in (None, "", [], {})})
            if constraints:
                merged["constraints"] = constraints
        else:
            merged[key] = value
    return merged

# ====== 功能：识别用户在 router 选择停点回复的 135/arc 选项。 ======
def _router_choice_from_text(text: Any) -> str:
    source = str(text or "").strip()
    if re.fullmatch(r"(?i)\s*(135|rule_?135|135\s*规则)\s*", source) or re.search(r"135|折角", source):
        return "rule_135"
    if re.fullmatch(r"(?i)\s*(arc|rule_?arc)\s*", source) or re.search(r"\barc\b|弧形|圆弧|曲线", source, re.IGNORECASE):
        return "rule_arc"
    return normalize_router_type(source)


# ====== 功能：兼容前端参数确认 JSON 的多种包裹格式。 ======
def _parse_fanout_param_confirmation(payload: Any) -> dict[str, Any] | None:
    data = _loads_json_object(payload)
    if data is None:
        return None
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    for candidate in (
        data.get("fanoutParams"),
        body.get("fanoutParams") if isinstance(body, dict) else None,
        data.get("content"),
        body.get("content") if isinstance(body, dict) else None,
        data,
    ):
        params = _loads_json_object(candidate)
        if params is None:
            continue
        nested = params.get("fanoutParams") if isinstance(params.get("fanoutParams"), dict) else params
        normalized = _fanout_params_from_payload(dict(nested))
        if _looks_like_fanout_params(nested) or normalized:
            return normalized or dict(nested)
    return None


# ====== 功能：将 JSON 字符串或字典统一解析为字典。 ======
def _loads_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text.startswith("{"):
        return None
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


# ====== 功能：判断前端返回是否像 fanout 参数确认。 ======
def _looks_like_fanout_params(value: Any) -> bool:
    return isinstance(value, dict) and any(key in value for key in ("orderLines", "constraints", "routerType", "algorithm", "selectedBGA", "targetBGAs", "bga", "bgaType", "layout", "rules", "routingPlan", "layerAssignment", "escapeOrder"))


# ====== 功能：合并前端确认参数，避免空 orderLines 覆盖真实逃逸顺序。 ======
def _merge_fanout_params(original: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(original)
    for key, value in updates.items():
        if key == "orderLines":
            if _has_effective_order_lines(value):
                merged[key] = value
            continue
        if key == "constraints" and isinstance(value, dict):
            constraints = dict(merged.get("constraints") or {})
            constraints.update({k: v for k, v in value.items() if v not in (None, "")})
            merged["constraints"] = constraints
            continue
        if key in {"routerType", "algorithm", "routeType"}:
            normalized = normalize_router_type(value) or _router_choice_from_text(value)
            if normalized:
                merged[key] = normalized
                merged["routerType"] = normalized
            continue
        if key in {"bgaType", "bgaLayoutType"}:
            normalized = _normalize_bga_type_text(value)
            if normalized:
                merged[key] = normalized
            continue
        if value not in (None, "", [], {}):
            merged[key] = value
    return merged


# ====== 功能：确认前端传回的 orderLines 至少包含有效 net/layer 或真实文本行。 ======
def _has_effective_order_lines(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for item in value:
        if isinstance(item, str) and item.strip():
            return True
        if isinstance(item, dict) and (str(item.get("net") or "").strip() or str(item.get("layer") or "").strip()):
            return True
    return False
