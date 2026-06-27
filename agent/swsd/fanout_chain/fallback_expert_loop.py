"""Fallback/clarify expert loop for invalid SWSD fanout entries."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from tools import pcb_model_runtime


@dataclass(frozen=True)
class FallbackExpertResult:
    reply: str
    reason: str
    severity: str = "clarify"
    accepted: bool = True
    raw_model_output: str = ""

    def as_immediate_reply(self) -> dict[str, Any]:
        return {"reply": self.reply, "metadata": {"reason": self.reason, "severity": self.severity}}


def _sanitize_reply(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"(?is)<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>", "", value)
    value = re.sub(r"(?is)</?think(?:ing)?>", "", value)
    value = value.replace("\ufffd", "")
    value = "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32)
    value = re.sub(r"\n{3,}", "\n\n", value).strip()
    if not value:
        return ""
    if len(value) >= 12 and value.count("?") / max(len(value), 1) > 0.35:
        return ""
    blocked_terms = ("pcb_entry", "layer_assign", "escape_order", "WorkflowActionPlan", "SWSDTurnDecision")
    for term in blocked_terms:
        value = value.replace(term, "当前步骤")
    return value[:1200]


def _json_from_text(text: str) -> dict[str, Any]:
    source = str(text or "").strip()
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


def _prompt(user_text: str, invalid_reason: str, current_state: str, feedback: tuple[str, ...]) -> list[dict[str, str]]:
    system = """你是 PCB fanout 流程反馈专家 / Expert E。

任务：当用户输入不能合法进入 fanout execute chain 时，把原因转成用户能理解的中文提示。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 只输出 {"reply": "..."}。
- reply 必须是自然中文，最多两句话。
- 不要暴露内部 action、state、allowed_actions、异常栈、代码路径。
- 不要输出英文调试信息或乱码。
- 不要说“系统错误”，除非确实完全无法判断。
- 只告诉用户现在缺什么、下一步该怎么做。

输出格式:
{
  "reply": "当前还不能开始 fanout。请先指定要处理的 BGA，例如 U5，或在 PCB 中选择目标器件。"
}
    """
    payload = {
        "user_text": user_text,
        "invalid_reason": invalid_reason,
        "current_state": current_state,
        "validation_feedback": list(feedback),
    }
    return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def _fallback_reply(invalid_reason: str) -> FallbackExpertResult:
    return FallbackExpertResult(
        reply="当前还不能执行全局 fanout。请先进入 PCB fanout 流程，或明确要处理的 BGA 器件和布线参数。",
        reason=invalid_reason or "fanout_entry_invalid",
        severity="clarify",
        accepted=True,
    )


def _validate_result(result: FallbackExpertResult) -> tuple[bool, tuple[str, ...]]:
    feedback: list[str] = []
    if not result.reply:
        feedback.append("empty reply")
    if result.severity not in {"clarify", "blocked"}:
        feedback.append("invalid severity")
    if any(term in result.reply for term in ("WorkflowActionPlan", "SWSDTurnDecision", "pcb_entry", "layer_assign", "escape_order")):
        feedback.append("reply exposes internal names")
    return not feedback, tuple(feedback)


def run_fallback_expert_loop(
    *,
    user_text: str,
    invalid_reason: str,
    current_state: str = "",
    model: Any = None,
    max_rounds: int = 3,
) -> FallbackExpertResult:
    feedback: tuple[str, ...] = ()
    for _round in range(max(1, max_rounds)):
        raw = ""
        data: dict[str, Any] = {}
        if model is not None and hasattr(model, "complete_json"):
            try:
                data = model.complete_json(_prompt(user_text, invalid_reason, current_state, feedback))
                raw = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data or "")
            except Exception:
                data = {}
        else:
            try:
                raw, _meta = pcb_model_runtime.chat_completion_text(
                    stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                    messages=_prompt(user_text, invalid_reason, current_state, feedback),
                    max_tokens=4096,
                    temperature=0,
                    top_p=1,
                    stream_until_json=True,
                )
                data = _json_from_text(raw)
            except Exception:
                data = {}
        if not data:
            result = _fallback_reply(invalid_reason)
        else:
            result = FallbackExpertResult(
                reply=_sanitize_reply(str(data.get("reply") or "")),
                reason=str(data.get("reason") or invalid_reason or "fanout_entry_invalid"),
                severity=str(data.get("severity") or "clarify"),
                raw_model_output=raw,
            )
        ok, feedback = _validate_result(result)
        if ok:
            return result
    return _fallback_reply(invalid_reason)
