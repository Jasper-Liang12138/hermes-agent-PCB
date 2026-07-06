from __future__ import annotations

import json
import re
from typing import Any

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
        self.graph = build_graph(self.model, self.tools, progress_sender=progress_sender, use_model_planner=use_model_planner, require_model_planner=require_model_planner)
        self._session_states: dict[str, PCBState] = {}

    # ====== 功能：异步执行当前工具或 Agent 调用。 ======
    async def ainvoke(self, session_id: str, project_id: str, user_input: str) -> PCBState:
        previous = self._session_states.get(session_id)
        history: list[ChatMessage] = list(previous.get("conversation_history", [])) if previous else []
        state = initial_state(session_id=session_id, project_id=project_id, user_input=user_input, history=history)
        if previous:
            # 多轮对话复用上轮项目状态和中间 cache，使 fanout/reroute 可以被追问后继续执行。
            state["pcb_project"] = previous.get("pcb_project", {})
            state["design_state"] = previous.get("design_state", {})
            state["intermediate_cache"] = self._cache_for_turn(previous.get("intermediate_cache", {}), user_input, previous.get("workflow_state", "idle"))
            state["workflow_id"] = previous.get("workflow_id", "idle")
            state["workflow_state"] = previous.get("workflow_state", "idle")
        result = await self.graph.ainvoke(state)
        self._session_states[session_id] = result
        return result


    @staticmethod
    # ====== 功能：执行 _cache_for_turn 的核心逻辑。 ======
    def _cache_for_turn(cache: dict, user_input: str, workflow_state: str = "idle") -> dict:
        text = user_input.lower()
        next_cache = dict(cache or {})
        entities = dict(next_cache.get("fanoutEntities") or {})
        user_entities = extract_fanout_entities(user_input)

        # BGA 选择阶段，用户/前端只要回复 U5/Uxx，就写入目标器件并保留原有约束。
        if workflow_state == "select_bga" and user_entities.get("selectedBGA"):
            selected = str(user_entities["selectedBGA"]).upper()
            entities.update({"selectedBGA": selected, "targetBGAs": [selected], "bgaSelectionConfirmed": True})
            next_cache["fanoutEntities"] = entities

        # router 选择阶段只接受 135 或 arc 两个可见选项，内部统一为 rule_135/rule_arc。
        router_type = _router_choice_from_text(user_input)
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
            for key in ("selectedBGA", "targetBGA", "targetBGAs", "routerType", "constraints"):
                value = merged_params.get(key)
                if value not in (None, "", [], {}):
                    merged_entities[key] = value
            next_cache["fanoutEntities"] = merged_entities

        # 参数确认阶段允许用户直接修改线宽、线距或 router 类型，需要回到参数生成节点。
        if workflow_state == "param_review" and confirmed_params is None and (any(token in user_input for token in ("线宽", "线距", "间距")) or any(token in text for token in ("width", "spacing", "135", "arc"))):
            for key in ("layerAssignResult", "escapeOrderResult", "fanout_routeResult", "importLinesResult", "drcResult", "explainabilityReport"):
                next_cache.pop(key, None)
            edited_entities = dict(next_cache.get("fanoutEntities") or {})
            edited_entities.update({k: v for k, v in user_entities.items() if v not in (None, "", [], {})})
            if router_type:
                edited_entities["routerType"] = router_type
            next_cache["fanoutEntities"] = edited_entities
            next_cache.pop("fanoutParamsConfirmed", None)
        # 用户要求重来时，仅清理对应流程的阶段性结果，保留项目数据和其他上下文。
        if any(token in user_input for token in ("重新", "重来", "再来", "不满意")) or any(token in text for token in ("rerun", "again")):
            if any(token in text for token in ("fanout",)) or any(token in user_input for token in ("逃逸", "扇出")):
                for key in ("layerAssignResult", "escapeOrderResult", "fanout_routeResult", "importLinesResult", "drcResult", "explainabilityReport", "fanoutParamsConfirmed"):
                    next_cache.pop(key, None)
            if any(token in text for token in ("reroute", "rip-up", "ripup")) or any(token in user_input for token in ("拆线", "重布")):
                for key in ("deleteTracesResult", "rerouteContext", "rerouteResult", "lastRerouteResult", "rerouteAttemptCount", "rerouteDrcFailureCount", "rerouteDrcFeedbackHistory", "rerouteUnavailable", "rerouteUnavailableReason", "helpPlannerResult", "importLinesResult", "drcResult", "lastDrcResult", "explainabilityReport"):
                    next_cache.pop(key, None)
        return next_cache


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
        if _looks_like_fanout_params(nested):
            return dict(nested)
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
    return isinstance(value, dict) and any(key in value for key in ("orderLines", "constraints", "routerType", "selectedBGA", "targetBGAs"))


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
        if key == "routerType":
            normalized = normalize_router_type(value) or _router_choice_from_text(value)
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





