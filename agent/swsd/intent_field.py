"""SWSD4 semantic intent field estimation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from tools import pcb_model_runtime


@dataclass(frozen=True)
class IntentFieldOutput:
    chat: float
    analyze: float
    execute: float
    meta: float
    uncertainty: float
    rationale: str = ""
    source: str = "llm"

    def normalized(self) -> "IntentFieldOutput":
        values = [max(0.0, self.chat), max(0.0, self.analyze), max(0.0, self.execute), max(0.0, self.meta)]
        total = sum(values)
        if total <= 0:
            values = [0.25, 0.25, 0.25, 0.25]
            total = 1.0
        return IntentFieldOutput(
            chat=round(values[0] / total, 6),
            analyze=round(values[1] / total, 6),
            execute=round(values[2] / total, 6),
            meta=round(values[3] / total, 6),
            uncertainty=max(0.0, min(1.0, self.uncertainty)),
            rationale=self.rationale,
            source=self.source,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "chat": self.chat,
            "analyze": self.analyze,
            "execute": self.execute,
            "meta": self.meta,
            "uncertainty": self.uncertainty,
            "rationale": self.rationale,
            "source": self.source,
        }


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_intent_field_output(raw_output: str, *, source: str = "llm") -> IntentFieldOutput:
    raw = str(raw_output or "").strip()
    data: dict[str, Any] = {}
    if raw:
        candidates = [raw]
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            candidates.insert(0, raw[start : end + 1])
        for candidate in candidates:
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                data = parsed
                break
    output = IntentFieldOutput(
        chat=_coerce_float(data.get("chat"), 0.25),
        analyze=_coerce_float(data.get("analyze"), 0.25),
        execute=_coerce_float(data.get("execute"), 0.25),
        meta=_coerce_float(data.get("meta"), 0.25),
        uncertainty=_coerce_float(data.get("uncertainty"), 0.5),
        rationale=str(data.get("rationale") or data.get("reason") or "").strip(),
        source=source,
    )
    return output.normalized()


def build_intent_field_prompt(
    *,
    user_text: str,
    flow_state: str,
    session_mode: str,
    candidate: dict[str, Any] | None = None,
    allowed_transitions: list[str] | None = None,
    tool_context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    system = """你是 PCB SWSD 字段抽取器。

任务：从用户中文输入中抽取明确出现的结构化字段，用于辅助意图仲裁。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 只抽取用户明确表达或高度确定的信息。
- 不要仅靠关键词做 action 分类。
- 不要编造 selectedBGA、routerType、constraints。
- JSON key 保持英文。
- 如果字段不存在，返回空对象或空字段，不要猜测。

可抽取字段示例:
- “给 U5 和 U7 布线” => {"targetBGAs":["U5","U7"]}
- “线宽3mil，线距3mil” => {"constraints":{"LineWidth":3,"LineSpacing":3}}
- “135+RL” => {"routerType":"135+RL"}
- “拆线重布 netA” => {"rerouteHint":"拆线重布","nets":["netA"]}
    """
    payload = {
        "user_text": str(user_text or ""),
        "flow_state": str(flow_state or "idle"),
        "session_mode": str(session_mode or "chat"),
        "raw_candidate": candidate or {},
        "allowed_transitions": allowed_transitions or [],
        "tool_context": tool_context or {},
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def estimate_intent_field(
    *,
    user_text: str,
    flow_state: str,
    session_mode: str,
    candidate: dict[str, Any] | None = None,
    allowed_transitions: list[str] | None = None,
    tool_context: dict[str, Any] | None = None,
    timeout_s: float = 8.0,
    max_tokens: int = 4096,
) -> IntentFieldOutput:
    raw_output, _meta = pcb_model_runtime.chat_completion_text(
        stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
        messages=build_intent_field_prompt(
            user_text=user_text,
            flow_state=flow_state,
            session_mode=session_mode,
            candidate=candidate,
            allowed_transitions=allowed_transitions,
            tool_context=tool_context,
        ),
        temperature=0,
        top_p=1,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
    )
    return parse_intent_field_output(raw_output, source="llm")
