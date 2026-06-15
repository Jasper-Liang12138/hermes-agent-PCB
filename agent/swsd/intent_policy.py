"""SWSD hierarchical intent policy and state-constrained decoding."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


ROUTE_MODE_CHAT = "chat"
ROUTE_MODE_PCB = "pcb"

INTENT_CHAT = "chat"
INTENT_PCB_ENTRY = "pcb_entry"
INTENT_PCB_FOLLOWUP = "pcb_followup"
INTENT_PCB_SELECT_TARGET = "pcb_select_target"
INTENT_PCB_CONFIRM_ROUTE = "pcb_confirm_route"
INTENT_PCB_MODIFY_PARAMS = "pcb_modify_params"
INTENT_PCB_REROUTE_SELECTED = "pcb_reroute_selected"
INTENT_CANCEL = "cancel"
INTENT_UNCLEAR = "unclear"

FLOW_IDLE = "idle"
FLOW_WAIT_SELECTION = "wait_selection"
FLOW_WAIT_ROUTER_TYPE = "wait_router_type"
FLOW_WAIT_CONFIRM = "wait_confirm"
FLOW_ROUTING = "routing"
FLOW_REROUTE = "reroute"

EXECUTION_EXECUTE = "EXECUTE"
EXECUTION_ANALYZE = "ANALYZE"
EXECUTION_CONSULT = "CONSULT"
EXECUTION_META = "META"


_NO_TOOL_RE = re.compile(r"不要调用工具|先别执行|不要执行|不用执行|别执行|仅说明|只说明|先说明|先不执行", re.IGNORECASE)
_META_BLOCK_RE = re.compile(
    r"先别执行|不要执行|不用执行|别执行|仅说明|只说明|先说明|先不执行|不要调用工具|不用调用工具|不要布线|先不布线|只分析|只解释",
    re.IGNORECASE,
)
_CANCEL_RE = re.compile(r"取消|退出|中止|停止|不做了|结束当前流程|cancel|abort|exit", re.IGNORECASE)
_REROUTE_RE = re.compile(r"拆线|重布|重新布|重新走|重走|reroute|rip-?up|删除.*(?:线|走线|trace|net|框选|选中)|未布通网络.*整理|网络.*重新整理", re.IGNORECASE)
_NEGATED_REROUTE_RE = re.compile(r"(?:不要|不是|别|无需|不用|不需要).{0,8}(?:reroute|拆线|重布|重新布|重走)", re.IGNORECASE)
_NEGATED_FANOUT_RE = re.compile(r"(?:不要|不是|别|无需|不用|不需要).{0,8}(?:fanout|扇出|逃逸|布线)", re.IGNORECASE)
_FANOUT_RE = re.compile(r"fanout|扇出|逃逸|BGA|bga|布线|route|版图|可布线", re.IGNORECASE)
_FANOUT_ACTION_RE = re.compile(r"做|执行|开始|启动|获取|找出|识别|跑|生成|只要|只做|帮我|请|对|给|能否", re.IGNORECASE)
_CONSULT_RE = re.compile(r"什么|区别|差异|影响|风险|利弊|原理|解释|介绍|讲讲|分析|会不会|怎么选|怎么做|分几步|一回事|对比|比较|应该先学", re.IGNORECASE)
_ANALYZE_RE = re.compile(r"分析|比较|对比|利弊|风险|影响|分几步|流程|步骤|方案|评估|看看|看一下|列出", re.IGNORECASE)
_CONSULT_ONLY_RE = re.compile(r"什么是|为什么|区别|差异|原理|解释|介绍|讲讲|怎么选|怎么做|应该先学|一般分几步|一回事", re.IGNORECASE)
_EXECUTE_ACTION_RE = re.compile(r"做|执行|开始|启动|生成|获取|找出|布线|逃逸|扇出|重布|重新布|导入|应用|跑|处理|修|优化|只要|只做|想做", re.IGNORECASE)
_DIRECT_EXECUTE_RE = re.compile(
    r"直接开始|不要解释.{0,8}(?:开始|执行|做|扇出|逃逸|布线|重布)|"
    r"(?:开始|启动|执行).{0,12}(?:PCB|BGA|fanout|扇出|逃逸|布线|重布|拆线|流程)|"
    r"(?:获取|找出).{0,12}可布线BGA",
    re.IGNORECASE,
)
_DISCUSSION_RE = re.compile(r"会不会|有什么影响|利弊|影响|风险|分几步|一般|什么是|为什么|是什么意思|意思|区别|差异|对比|列出|有哪些|看看|看一下|分析|只分析|不要执行|走线太密.{0,8}重布|能.{0,8}重布吗", re.IGNORECASE)
_ENGINEERING_OBJECT_RE = re.compile(r"PCB|BGA|fanout|reroute|rip-?up|U\d+|IC\d+|FPGA\d+|BGA\d+|走线|网络|板子|布线|逃逸|扇出|重布|拆线", re.IGNORECASE)
_CONFIRM_RE = re.compile(r"确认|继续|执行|开始|开始布线|go\b|yes\b|ok\b", re.IGNORECASE)
_STRONG_CONFIRM_RE = re.compile(r"确认|继续|执行|开始|开始布线|开始执行|go\b|run\b|yes\b|ok\b", re.IGNORECASE)
_WEAK_CONFIRM_RE = re.compile(r"^(?:嗯+|好|好的|行|可以|收到|随便|都行|可以吧)[，。！？?!\s（）()]*$", re.IGNORECASE)
_PARAM_RE = re.compile(r"线宽|间距|linewidth|linespacing|width|spacing|改成|修改|调整|调到|\d+\s*mil", re.IGNORECASE)
_ROUTER_RE = re.compile(r"\b(?:arc|rl|135|rl_arc|rl_135)\b|圆弧|北科大|折角|135\s*度", re.IGNORECASE)
_TARGET_RE = re.compile(r"\b(?:U|FPGA)[A-Za-z0-9_-]*\d+\b|选择|选\s*(?:U|FPGA)?\d+", re.IGNORECASE)
_TARGET_ENTITY_RE = re.compile(r"(?<![A-Za-z0-9_])(?:U|IC|BGA|FPGA)[A-Za-z0-9_-]*\d+(?![A-Za-z0-9_])", re.IGNORECASE)
_FUZZY_RE = re.compile(r"^(?:现在|先|帮我|麻烦你|请问|嗯|好|好的|可以|随便|帮忙|弄一下|再想想|谢谢|吗|？|\\?|\s|，|。|！|!)*$", re.IGNORECASE)
_POLITE_CHAT_RE = re.compile(r"^(?:嗯|你好|您好|谢谢|谢谢你|谢谢谢谢|麻烦你|请问|，|。|\s)+$", re.IGNORECASE)
_VAGUE_UNCLEAR_RE = re.compile(r"帮忙|弄一下|有点问题|不太好看|整理一下|看看怎么办|怎么办", re.IGNORECASE)
_ACK_UNCLEAR_RE = re.compile(r"^(?:现在|先|帮我|麻烦你|麻烦|请问|请)*(?:嗯+|好|好的|可以|随便|行|收到)(?:可以吗)?$", re.IGNORECASE)


@dataclass(frozen=True)
class HierarchicalIntent:
    task_intent: str = ""
    control_intent: str = ""
    meta_intent: str = ""
    invalid_intent: str = ""
    confidence: float = 0.0


@dataclass(frozen=True)
class PolicyDecision:
    intent: str
    route_mode: str
    should_call_get_project_data: bool = False
    confidence: float = 0.0
    reason: str = ""
    hierarchy: HierarchicalIntent = HierarchicalIntent()
    execution_intent: str = ""
    guard_reason: str = ""
    allow_workflow_entry: bool = True


@dataclass(frozen=True)
class ExecutionGuardDecision:
    execution_intent: str
    guard_reason: str
    allow_workflow_entry: bool


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "off", "none", "null"}
    return bool(value)


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _candidate_from_mapping(candidate: Any) -> dict[str, Any]:
    if candidate is None:
        return {}
    if isinstance(candidate, dict):
        return candidate
    return {
        "intent": getattr(candidate, "intent", ""),
        "route_mode": getattr(candidate, "route_mode", ""),
        "confidence": getattr(candidate, "confidence", 0.0),
        "should_call_get_project_data": getattr(candidate, "should_call_get_project_data", False),
        "reason_code": getattr(candidate, "reason_code", ""),
    }


def _hierarchy_from_text(text: str, candidate: dict[str, Any]) -> HierarchicalIntent:
    raw = candidate.get("raw") if isinstance(candidate.get("raw"), dict) else candidate
    task = str(raw.get("task_intent") or raw.get("taskIntent") or "").strip()
    control = str(raw.get("control_intent") or raw.get("controlIntent") or "").strip()
    meta = str(raw.get("meta_intent") or raw.get("metaIntent") or "").strip()
    invalid = str(raw.get("invalid_intent") or raw.get("invalidIntent") or "").strip()
    text = str(text or "")
    if not control and _NO_TOOL_RE.search(text):
        control = "defer_execution"
    if not meta and _NO_TOOL_RE.search(text):
        meta = "no_tool_call"
    if not task:
        task = str(candidate.get("intent") or "").strip()
    return HierarchicalIntent(
        task_intent=task,
        control_intent=control,
        meta_intent=meta,
        invalid_intent=invalid,
        confidence=_confidence(candidate.get("confidence")),
    )


def _is_fuzzy(text: str) -> bool:
    clean = _NO_TOOL_RE.sub("", str(text or "")).strip(" ，。！？?!")
    if len(clean) <= 3:
        return True
    return bool(_FUZZY_RE.match(clean))


def _state_mode(flow_state: str, intent: str) -> str:
    if intent == INTENT_CANCEL:
        return ROUTE_MODE_CHAT
    if flow_state and flow_state != FLOW_IDLE:
        return ROUTE_MODE_PCB
    if intent in {
        INTENT_PCB_ENTRY,
        INTENT_PCB_FOLLOWUP,
        INTENT_PCB_SELECT_TARGET,
        INTENT_PCB_CONFIRM_ROUTE,
        INTENT_PCB_MODIFY_PARAMS,
        INTENT_PCB_REROUTE_SELECTED,
    }:
        return ROUTE_MODE_PCB
    return ROUTE_MODE_CHAT


def classify_execution_intent(
    *,
    text: str,
    flow_state: str = FLOW_IDLE,
    candidate: Any = None,
) -> ExecutionGuardDecision:
    """Classify whether an idle request is asking to execute or discuss PCB work."""
    text = str(text or "").strip()
    flow_state = str(flow_state or FLOW_IDLE)
    data = _candidate_from_mapping(candidate)
    raw_intent = str(data.get("intent") or "").strip()

    if _CANCEL_RE.search(text):
        return ExecutionGuardDecision(EXECUTION_META, "swsd3_guard_cancel_meta", True)
    if flow_state != FLOW_IDLE:
        return ExecutionGuardDecision(EXECUTION_EXECUTE, "swsd3_guard_active_workflow", True)
    has_engineering_object = bool(_ENGINEERING_OBJECT_RE.search(text))
    has_execute_action = bool(_EXECUTE_ACTION_RE.search(text))
    clear_reroute_execute = bool(_REROUTE_RE.search(text) and not _NEGATED_REROUTE_RE.search(text))
    clear_fanout_execute = bool(_FANOUT_RE.search(text) and has_execute_action)
    explicit_target_fanout = bool(_FANOUT_RE.search(text) and _TARGET_ENTITY_RE.search(text))
    raw_entry = raw_intent in {INTENT_PCB_ENTRY, INTENT_PCB_REROUTE_SELECTED}

    if re.search(r"只分析", text, re.IGNORECASE):
        return ExecutionGuardDecision(EXECUTION_ANALYZE, "swsd3_guard_analysis_chat", False)
    if _DIRECT_EXECUTE_RE.search(text) or explicit_target_fanout:
        return ExecutionGuardDecision(EXECUTION_EXECUTE, "swsd3_guard_execute_entry", True)
    if _NEGATED_FANOUT_RE.search(text) and not re.search(r"只要|只做", text, re.IGNORECASE):
        return ExecutionGuardDecision(EXECUTION_ANALYZE, "swsd3_guard_analysis_chat", False)
    if _CONSULT_ONLY_RE.search(text) or _DISCUSSION_RE.search(text):
        return ExecutionGuardDecision(EXECUTION_CONSULT, "swsd3_guard_consult_chat", False)
    if clear_reroute_execute or clear_fanout_execute or (raw_entry and has_execute_action and has_engineering_object):
        return ExecutionGuardDecision(EXECUTION_EXECUTE, "swsd3_guard_execute_entry", True)
    if _META_BLOCK_RE.search(text):
        return ExecutionGuardDecision(EXECUTION_META, "swsd3_guard_meta_no_execute", False)
    if has_engineering_object and (_CONSULT_RE.search(text) or "?" in text or "？" in text):
        return ExecutionGuardDecision(EXECUTION_CONSULT, "swsd3_guard_consult_chat", False)
    if raw_entry and not has_execute_action:
        return ExecutionGuardDecision(EXECUTION_ANALYZE, "swsd3_guard_analysis_chat", False)
    return ExecutionGuardDecision(EXECUTION_CONSULT, "swsd3_guard_consult_chat", False)


def _confirm_text_core(text: str) -> str:
    clean = _NO_TOOL_RE.sub("", str(text or ""))
    clean = re.sub(r"[\s，。！？?!（）()]+", "", clean)
    clean = re.sub(r"^(?:麻烦你|麻烦|请问|请|现在|先|帮我)+", "", clean)
    return clean


def _with_guard(decision: PolicyDecision, guard: ExecutionGuardDecision) -> PolicyDecision:
    return PolicyDecision(
        intent=decision.intent,
        route_mode=decision.route_mode,
        should_call_get_project_data=decision.should_call_get_project_data,
        confidence=decision.confidence,
        reason=decision.reason,
        hierarchy=decision.hierarchy,
        execution_intent=guard.execution_intent,
        guard_reason=guard.guard_reason,
        allow_workflow_entry=guard.allow_workflow_entry,
    )


def apply_swsd2_policy(
    *,
    text: str,
    flow_state: str = FLOW_IDLE,
    session_mode: str = ROUTE_MODE_CHAT,
    candidate: Any = None,
) -> PolicyDecision:
    """Calibrate a raw LLM/rule intent with SWSD 2.0 workflow constraints."""
    text = str(text or "").strip()
    flow_state = str(flow_state or FLOW_IDLE)
    session_mode = str(session_mode or ROUTE_MODE_CHAT)
    data = _candidate_from_mapping(candidate)
    raw_intent = str(data.get("intent") or "").strip() or INTENT_CHAT
    raw_route = str(data.get("route_mode") or data.get("routeMode") or "").strip() or ROUTE_MODE_CHAT
    conf = _confidence(data.get("confidence"))
    hierarchy = _hierarchy_from_text(text, data)
    in_workflow = flow_state != FLOW_IDLE or session_mode == ROUTE_MODE_PCB
    has_defer = bool(hierarchy.control_intent or hierarchy.meta_intent or _NO_TOOL_RE.search(text))

    if _CANCEL_RE.search(text):
        return PolicyDecision(INTENT_CANCEL, ROUTE_MODE_CHAT, False, conf, "swsd2_cancel_normalized", hierarchy)

    if flow_state == FLOW_WAIT_SELECTION:
        if _TARGET_RE.search(text) or raw_intent == INTENT_PCB_SELECT_TARGET:
            return PolicyDecision(INTENT_PCB_SELECT_TARGET, ROUTE_MODE_PCB, False, conf, "swsd2_state_select_target", hierarchy)
        return PolicyDecision(INTENT_UNCLEAR, ROUTE_MODE_PCB, False, conf, "swsd2_invalid_selection_turn", hierarchy)

    if flow_state == FLOW_WAIT_ROUTER_TYPE:
        if _ROUTER_RE.search(text) or raw_intent == INTENT_PCB_FOLLOWUP:
            return PolicyDecision(INTENT_PCB_FOLLOWUP, ROUTE_MODE_PCB, False, conf, "swsd2_state_router_choice", hierarchy)
        if _CONFIRM_RE.search(text) or raw_intent == INTENT_PCB_CONFIRM_ROUTE:
            return PolicyDecision(INTENT_PCB_CONFIRM_ROUTE, ROUTE_MODE_PCB, False, conf, "swsd2_confirm_before_router", hierarchy)
        return PolicyDecision(INTENT_UNCLEAR, ROUTE_MODE_PCB, False, conf, "swsd2_invalid_router_turn", hierarchy)

    if flow_state == FLOW_WAIT_CONFIRM:
        if _PARAM_RE.search(text) or raw_intent == INTENT_PCB_MODIFY_PARAMS:
            return PolicyDecision(INTENT_PCB_MODIFY_PARAMS, ROUTE_MODE_PCB, False, conf, "swsd2_modify_params", hierarchy)
        if _CONFIRM_RE.search(text) or raw_intent == INTENT_PCB_CONFIRM_ROUTE:
            return PolicyDecision(INTENT_PCB_CONFIRM_ROUTE, ROUTE_MODE_PCB, False, conf, "swsd2_confirm_route", hierarchy)
        return PolicyDecision(INTENT_UNCLEAR, ROUTE_MODE_PCB, False, conf, "swsd2_invalid_confirm_turn", hierarchy)

    if flow_state in {FLOW_ROUTING, FLOW_REROUTE}:
        if _REROUTE_RE.search(text) or raw_intent == INTENT_PCB_REROUTE_SELECTED:
            return PolicyDecision(INTENT_PCB_REROUTE_SELECTED, ROUTE_MODE_PCB, False, conf, "swsd2_reroute_context", hierarchy)
        return PolicyDecision(raw_intent if raw_intent != INTENT_CHAT else INTENT_UNCLEAR, ROUTE_MODE_PCB, False, conf, "swsd2_active_workflow", hierarchy)

    if _is_fuzzy(text):
        return PolicyDecision(INTENT_UNCLEAR, ROUTE_MODE_CHAT, False, conf, "swsd2_fuzzy_idle", hierarchy)

    if raw_intent == INTENT_CHAT and _CONSULT_RE.search(text) and not _NEGATED_REROUTE_RE.search(text):
        return PolicyDecision(INTENT_CHAT, ROUTE_MODE_CHAT, False, conf, "swsd2_preserve_consultation_chat", hierarchy)

    if raw_intent == INTENT_PCB_REROUTE_SELECTED or (
        _REROUTE_RE.search(text)
        and not _NEGATED_REROUTE_RE.search(text)
        and not (_CONSULT_RE.search(text) and not _FANOUT_ACTION_RE.search(text))
    ):
        return PolicyDecision(INTENT_PCB_REROUTE_SELECTED, ROUTE_MODE_PCB, False, conf, "swsd2_reroute_entry", hierarchy)

    fanout_requested = bool(_FANOUT_RE.search(text) and (_FANOUT_ACTION_RE.search(text) or raw_intent == INTENT_PCB_ENTRY))
    if fanout_requested and not (_CONSULT_RE.search(text) and not _FANOUT_ACTION_RE.search(text)):
        return PolicyDecision(INTENT_PCB_ENTRY, ROUTE_MODE_PCB, True, conf, "swsd2_fanout_entry", hierarchy)

    if raw_intent == INTENT_UNCLEAR:
        return PolicyDecision(INTENT_UNCLEAR, _state_mode(flow_state, INTENT_UNCLEAR), False, conf, "swsd2_raw_unclear", hierarchy)

    if raw_intent == INTENT_CANCEL:
        return PolicyDecision(INTENT_CANCEL, ROUTE_MODE_CHAT, False, conf, "swsd2_cancel_normalized", hierarchy)

    if raw_intent in {
        INTENT_PCB_ENTRY,
        INTENT_PCB_REROUTE_SELECTED,
        INTENT_PCB_FOLLOWUP,
        INTENT_PCB_SELECT_TARGET,
        INTENT_PCB_CONFIRM_ROUTE,
        INTENT_PCB_MODIFY_PARAMS,
    }:
        should_call = _as_bool(data.get("should_call_get_project_data"))
        if raw_intent == INTENT_PCB_ENTRY and not has_defer:
            should_call = True
        return PolicyDecision(raw_intent, _state_mode(flow_state, raw_intent), should_call, conf, "swsd2_raw_task_intent", hierarchy)

    if raw_route == ROUTE_MODE_PCB and in_workflow:
        return PolicyDecision(INTENT_UNCLEAR, ROUTE_MODE_PCB, False, conf, "swsd2_pcb_context_unclear", hierarchy)

    return PolicyDecision(INTENT_CHAT, ROUTE_MODE_CHAT, False, conf, "swsd2_chat", hierarchy)


def apply_swsd3_policy(
    *,
    text: str,
    flow_state: str = FLOW_IDLE,
    session_mode: str = ROUTE_MODE_CHAT,
    candidate: Any = None,
) -> PolicyDecision:
    """Apply SWSD3 execution gating before SWSD2 workflow calibration."""
    text = str(text or "").strip()
    flow_state = str(flow_state or FLOW_IDLE)
    session_mode = str(session_mode or ROUTE_MODE_CHAT)
    data = _candidate_from_mapping(candidate)
    conf = _confidence(data.get("confidence"))
    hierarchy = _hierarchy_from_text(text, data)
    guard = classify_execution_intent(text=text, flow_state=flow_state, candidate=data)

    if _CANCEL_RE.search(text):
        return PolicyDecision(
            INTENT_CANCEL,
            ROUTE_MODE_CHAT,
            False,
            conf,
            "swsd2_cancel_normalized",
            hierarchy,
            guard.execution_intent,
            guard.guard_reason,
            guard.allow_workflow_entry,
        )

    if flow_state == FLOW_WAIT_SELECTION:
        if _STRONG_CONFIRM_RE.search(_confirm_text_core(text)):
            return PolicyDecision(
                INTENT_PCB_CONFIRM_ROUTE,
                ROUTE_MODE_PCB,
                False,
                conf,
                "swsd3_strong_confirm_route",
                hierarchy,
                guard.execution_intent,
                guard.guard_reason,
                guard.allow_workflow_entry,
            )
        if _TARGET_ENTITY_RE.search(text) or _TARGET_RE.search(text):
            return PolicyDecision(
                INTENT_PCB_SELECT_TARGET,
                ROUTE_MODE_PCB,
                False,
                conf,
                "swsd3_state_select_target_entity",
                hierarchy,
                guard.execution_intent,
                guard.guard_reason,
                guard.allow_workflow_entry,
            )

    if flow_state == FLOW_WAIT_CONFIRM:
        if _PARAM_RE.search(text):
            return _with_guard(
                apply_swsd2_policy(text=text, flow_state=flow_state, session_mode=session_mode, candidate=data),
                guard,
            )
        confirm_core = _confirm_text_core(text)
        if _WEAK_CONFIRM_RE.search(confirm_core):
            return PolicyDecision(
                INTENT_UNCLEAR,
                ROUTE_MODE_PCB,
                False,
                conf,
                "swsd3_weak_confirm_unclear",
                hierarchy,
                guard.execution_intent,
                guard.guard_reason,
                guard.allow_workflow_entry,
            )
        if _STRONG_CONFIRM_RE.search(confirm_core) or "ok" in confirm_core.lower():
            return PolicyDecision(
                INTENT_PCB_CONFIRM_ROUTE,
                ROUTE_MODE_PCB,
                False,
                conf,
                "swsd3_strong_confirm_route",
                hierarchy,
                guard.execution_intent,
                guard.guard_reason,
                guard.allow_workflow_entry,
            )

    if flow_state == FLOW_IDLE and not guard.allow_workflow_entry:
        reason = guard.guard_reason
        if _POLITE_CHAT_RE.match(text):
            intent = INTENT_CHAT
        elif _ACK_UNCLEAR_RE.match(_confirm_text_core(text)) or _VAGUE_UNCLEAR_RE.search(text):
            intent = INTENT_UNCLEAR
        elif guard.execution_intent in {EXECUTION_ANALYZE, EXECUTION_CONSULT}:
            intent = INTENT_CHAT
        else:
            intent = INTENT_UNCLEAR if _is_fuzzy(text) or str(data.get("intent") or "") == INTENT_UNCLEAR else INTENT_CHAT
        return PolicyDecision(
            intent,
            ROUTE_MODE_CHAT,
            False,
            conf,
            reason,
            hierarchy,
            guard.execution_intent,
            guard.guard_reason,
            guard.allow_workflow_entry,
        )

    if flow_state == FLOW_IDLE and guard.execution_intent == EXECUTION_EXECUTE:
        if _REROUTE_RE.search(text) and not _NEGATED_REROUTE_RE.search(text):
            return PolicyDecision(
                INTENT_PCB_REROUTE_SELECTED,
                ROUTE_MODE_PCB,
                False,
                conf,
                "swsd3_guard_execute_entry",
                hierarchy,
                guard.execution_intent,
                guard.guard_reason,
                guard.allow_workflow_entry,
            )
        if _FANOUT_RE.search(text):
            return PolicyDecision(
                INTENT_PCB_ENTRY,
                ROUTE_MODE_PCB,
                True,
                conf,
                "swsd3_guard_execute_entry",
                hierarchy,
                guard.execution_intent,
                guard.guard_reason,
                guard.allow_workflow_entry,
            )

    return _with_guard(
        apply_swsd2_policy(text=text, flow_state=flow_state, session_mode=session_mode, candidate=data),
        guard,
    )


def apply_swsd4_policy(
    *,
    text: str,
    flow_state: str = FLOW_IDLE,
    session_mode: str = ROUTE_MODE_CHAT,
    candidate: Any = None,
    intent_field: Any = None,
    skill_grounding: list[Any] | None = None,
) -> PolicyDecision:
    """Apply SWSD4 probabilistic intent-field decision policy."""
    from agent.swsd.decision_policy import WorkflowContext, decide_with_intent_field
    from agent.swsd.intent_field import IntentFieldOutput
    from agent.swsd.skill_grounding import retrieve_skill_memory

    text = str(text or "").strip()
    flow_state = str(flow_state or FLOW_IDLE)
    session_mode = str(session_mode or ROUTE_MODE_CHAT)
    data = _candidate_from_mapping(candidate)

    if intent_field is None:
        raw_intent = str(data.get("intent") or "").strip()
        raw_route = str(data.get("route_mode") or "").strip()
        conf = _confidence(data.get("confidence")) or 0.8
        if raw_intent == INTENT_CANCEL:
            base = IntentFieldOutput(chat=0.05, analyze=0.05, execute=0.05, meta=conf, uncertainty=0.1, source="candidate_prior")
        elif raw_intent == INTENT_UNCLEAR:
            base = IntentFieldOutput(chat=0.25, analyze=0.2, execute=0.2, meta=0.1, uncertainty=0.45, source="candidate_prior")
        elif raw_route == ROUTE_MODE_PCB or raw_intent in {
            INTENT_PCB_ENTRY,
            INTENT_PCB_REROUTE_SELECTED,
            INTENT_PCB_FOLLOWUP,
            INTENT_PCB_SELECT_TARGET,
            INTENT_PCB_CONFIRM_ROUTE,
            INTENT_PCB_MODIFY_PARAMS,
        }:
            base = IntentFieldOutput(chat=0.05, analyze=0.05, execute=conf, meta=0.05, uncertainty=0.15, source="candidate_prior")
        else:
            base = IntentFieldOutput(chat=conf, analyze=0.1, execute=0.05, meta=0.05, uncertainty=0.15, source="candidate_prior")
        intent_field = base.normalized()
    elif isinstance(intent_field, dict):
        intent_field = IntentFieldOutput(
            chat=float(intent_field.get("chat", 0.25)),
            analyze=float(intent_field.get("analyze", 0.25)),
            execute=float(intent_field.get("execute", 0.25)),
            meta=float(intent_field.get("meta", 0.25)),
            uncertainty=float(intent_field.get("uncertainty", 0.5)),
            rationale=str(intent_field.get("rationale") or ""),
            source=str(intent_field.get("source") or "provided"),
        ).normalized()

    grounding = skill_grounding if skill_grounding is not None else retrieve_skill_memory(text, flow_state)
    decision = decide_with_intent_field(
        text=text,
        session_mode=session_mode,
        candidate=data,
        intent_field=intent_field,
        workflow_context=WorkflowContext(
            workflow_state=flow_state,
            current_node=flow_state,
            allowed_transitions=(),
            tool_context={},
        ),
        skill_grounding=grounding,
    )
    return decision.as_policy_decision()
