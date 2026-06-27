"""Controlled fanout parameter intent loop for SWSD fanout execution."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from tools import pcb_model_runtime
from tools.pcb_nl_fanout import parse_fanout_constraints_from_text, parse_fanout_target_from_text


@dataclass(frozen=True)
class FanoutTarget:
    raw: str
    normalized: str


@dataclass(frozen=True)
class FanoutConstraintSet:
    raw: str
    normalized: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FanoutParamPlan:
    intent_kind: str
    target_bgas: tuple[FanoutTarget, ...] = ()
    constraints: FanoutConstraintSet = field(default_factory=lambda: FanoutConstraintSet(raw="", normalized={}))
    jump_to: str = "select_bga"
    skip_select_bga: bool = False
    reason: str = ""
    accepted: bool = True
    raw_model_output: str = ""
    validation_feedback: tuple[str, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "intent_kind": self.intent_kind,
            "target_bgas": [target.__dict__ for target in self.target_bgas],
            "constraints": {
                "raw": self.constraints.raw,
                "normalized": dict(self.constraints.normalized),
            },
            "jump_to": self.jump_to,
            "skip_select_bga": self.skip_select_bga,
            "reason": self.reason,
            "accepted": self.accepted,
            "validation_feedback": list(self.validation_feedback),
        }


_BGA_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z]{1,6}\d{1,5})(?![A-Za-z0-9_])")
_ALLOWED_KINDS = {
    "global_fanout",
    "target_fanout",
    "global_fanout_with_constraints",
    "target_fanout_with_constraints",
}


def _target_bgas_from_text(text: str) -> tuple[FanoutTarget, ...]:
    seen: set[str] = set()
    targets: list[FanoutTarget] = []
    parsed = parse_fanout_target_from_text(text)
    candidates = [parsed] if parsed else []
    candidates.extend(match.group(1) for match in _BGA_RE.finditer(text or ""))
    for raw in candidates:
        value = str(raw or "").strip()
        normalized = value.upper()
        if not value or normalized in seen:
            continue
        seen.add(normalized)
        targets.append(FanoutTarget(raw=value, normalized=normalized))
    return tuple(targets)


def _constraints_from_text(text: str) -> FanoutConstraintSet:
    normalized = parse_fanout_constraints_from_text(text)
    raw = ""
    if normalized:
        raw = str(text or "")
    return FanoutConstraintSet(raw=raw, normalized=normalized)


def _rule_based_plan(user_text: str, *, reason: str = "rule_based") -> FanoutParamPlan:
    targets = _target_bgas_from_text(user_text)
    constraints = _constraints_from_text(user_text)
    has_target = bool(targets)
    has_constraints = bool(constraints.normalized)
    if has_target and has_constraints:
        kind = "target_fanout_with_constraints"
    elif has_target:
        kind = "target_fanout"
    elif has_constraints:
        kind = "global_fanout_with_constraints"
    else:
        kind = "global_fanout"
    return FanoutParamPlan(
        intent_kind=kind,
        target_bgas=targets,
        constraints=constraints,
        jump_to="layer_assign_escape_order" if has_target else "select_bga",
        skip_select_bga=has_target,
        reason=reason,
    )


def _json_from_text(text: str) -> dict[str, Any]:
    source = str(text or "").strip()
    if not source:
        return {}
    if source.startswith("```"):
        source = re.sub(r"^```(?:json)?\s*", "", source, flags=re.IGNORECASE)
        source = re.sub(r"\s*```$", "", source)
    try:
        value = json.loads(source)
        return value if isinstance(value, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", source)
        if not match:
            return {}
        try:
            value = json.loads(match.group(0))
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}


def _normalize_model_plan(data: dict[str, Any], user_text: str, raw_output: str) -> FanoutParamPlan:
    fallback = _rule_based_plan(user_text, reason="model_output_normalized")
    kind = str(data.get("intent_kind") or data.get("intentKind") or fallback.intent_kind).strip()
    if kind not in _ALLOWED_KINDS:
        kind = fallback.intent_kind

    raw_targets = data.get("target_bgas") or data.get("targetBGAs") or data.get("targets") or data.get("target_bga") or data.get("targetBGA")
    targets: list[FanoutTarget] = []
    if isinstance(raw_targets, dict):
        raw_targets = [raw_targets]
    if isinstance(raw_targets, str):
        raw_targets = [raw_targets]
    if isinstance(raw_targets, list):
        for item in raw_targets:
            if isinstance(item, dict):
                raw = str(item.get("raw") or item.get("value") or item.get("normalized") or "").strip()
                normalized = str(item.get("normalized") or raw).strip().upper()
            else:
                raw = str(item or "").strip()
                normalized = raw.upper()
            if raw and normalized:
                targets.append(FanoutTarget(raw=raw, normalized=normalized))
    if not targets:
        targets = list(fallback.target_bgas)

    constraints_obj = data.get("constraints") if isinstance(data.get("constraints"), dict) else {}
    normalized_constraints = constraints_obj.get("normalized") if isinstance(constraints_obj.get("normalized"), dict) else {}
    if not normalized_constraints:
        normalized_constraints = fallback.constraints.normalized
    raw_constraints = str(constraints_obj.get("raw") or fallback.constraints.raw or "")

    jump_to = str(data.get("jump_to") or data.get("jumpTo") or fallback.jump_to).strip()
    if jump_to not in {"select_bga", "layer_assign_escape_order"}:
        jump_to = fallback.jump_to
    skip_select = bool(data.get("skip_select_bga") if "skip_select_bga" in data else data.get("skipSelectBga", fallback.skip_select_bga))
    if targets:
        skip_select = True
        jump_to = "layer_assign_escape_order"
    return FanoutParamPlan(
        intent_kind=kind,
        target_bgas=tuple(targets),
        constraints=FanoutConstraintSet(raw=raw_constraints, normalized=dict(normalized_constraints or {})),
        jump_to=jump_to,
        skip_select_bga=skip_select,
        reason=str(data.get("reason") or fallback.reason),
        raw_model_output=raw_output,
    )


def _validate_plan(plan: FanoutParamPlan) -> tuple[bool, tuple[str, ...]]:
    feedback: list[str] = []
    if plan.intent_kind not in _ALLOWED_KINDS:
        feedback.append("intent_kind is not allowed")
    if plan.jump_to not in {"select_bga", "layer_assign_escape_order"}:
        feedback.append("jump_to is not allowed")
    for target in plan.target_bgas:
        if not re.fullmatch(r"[A-Z]{1,6}\d{1,5}", target.normalized or ""):
            feedback.append(f"invalid target BGA: {target.normalized}")
    for key, value in plan.constraints.normalized.items():
        if key not in {"LineWidth", "LineSpacing"}:
            feedback.append(f"unsupported constraint: {key}")
        if not isinstance(value, (int, float)) or value <= 0:
            feedback.append(f"invalid constraint value: {key}")
    return not feedback, tuple(feedback)


def _model_prompt(user_text: str, feedback: tuple[str, ...]) -> list[dict[str, str]]:
    system = """你是 PCB fanout 参数识别专家 / Expert F。

任务：从用户中文输入中识别 fanout 参数计划，用于 SWSD fanout execute chain。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 不要调用工具，不要执行 route。
- JSON key 保持英文。
- 只识别用户明确表达或高度确定的信息。
- 支持多个 BGA，例如 “U5 和 U7” => targetBGAs=["U5","U7"]。
- 支持线宽/线距，例如 “线宽3mil，线距3mil” => constraints={"LineWidth":3,"LineSpacing":3}。
- 支持 routerType，例如 “135 + RL” => routerType="135+RL"。
- 如果用户只说 fanout/扇出/逃逸/布线，但没有目标，仍可输出 intent="fanout_entry"，不要编造 selectedBGA。
- 如果用户指定目标 BGA，可以建议 jump_to="layer_assign_escape_order"，因为可跳过 select_bga。
- 保留 raw_constraints 与 normalized 两份约束：raw_constraints 保存用户原始表达，constraints 保存规范字段。

输出格式:
{
  "intent": "fanout_entry",
  "selectedBGA": "U5",
  "targetBGAs": ["U5", "U7"],
  "routerType": "135+RL",
  "constraints": {"LineWidth": 3, "LineSpacing": 3},
  "raw_constraints": {"line_width": "3mil", "line_spacing": "3mil"},
  "jump_to": "layer_assign_escape_order",
  "reason": "用户指定了 U5/U7 和线宽线距参数"
}

例子:
- “fanout” => {"intent":"fanout_entry","reason":"用户请求进入 fanout"}
- “给 U5 布线” => {"intent":"fanout_entry","selectedBGA":"U5","targetBGAs":["U5"],"jump_to":"layer_assign_escape_order"}
- “fanout，线宽/线距3mil” => {"intent":"fanout_entry","constraints":{"LineWidth":3,"LineSpacing":3},"raw_constraints":{"line_width":"3mil","line_spacing":"3mil"}}
- “给 U5 布线，线宽3mil，线距3mil” => {"intent":"fanout_entry","selectedBGA":"U5","targetBGAs":["U5"],"constraints":{"LineWidth":3,"LineSpacing":3},"jump_to":"layer_assign_escape_order"}
    """
    payload = {"user_text": user_text, "validation_feedback": list(feedback)}
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def run_fanout_param_loop(user_text: str, *, model: Any = None, max_rounds: int = 3) -> FanoutParamPlan:
    feedback: tuple[str, ...] = ()
    raw_output = ""
    for _round in range(max(1, max_rounds)):
        if model is not None and hasattr(model, "complete_json"):
            try:
                data = model.complete_json(_model_prompt(user_text, feedback))
                raw_output = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data or "")
            except Exception:
                data = {}
        else:
            try:
                raw_output, _meta = pcb_model_runtime.chat_completion_text(
                    stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                    messages=_model_prompt(user_text, feedback),
                    max_tokens=4096,
                    temperature=0,
                    top_p=1,
                    stream_until_json=True,
                )
                data = _json_from_text(raw_output)
            except Exception:
                data = {}
        plan = _normalize_model_plan(data, user_text, raw_output) if data else _rule_based_plan(user_text)
        ok, feedback = _validate_plan(plan)
        if ok:
            return plan
    fallback = _rule_based_plan(user_text, reason="fanout_param_loop_fallback")
    ok, feedback = _validate_plan(fallback)
    if ok:
        return fallback
    return FanoutParamPlan(
        intent_kind="global_fanout",
        jump_to="select_bga",
        skip_select_bga=False,
        reason="fanout_param_loop_default_global",
        accepted=False,
        validation_feedback=feedback,
    )
