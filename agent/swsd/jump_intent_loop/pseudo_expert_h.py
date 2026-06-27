"""Rule pseudo-expert H for confirmation response cleanup."""

from __future__ import annotations

import re
from typing import Any

from .models import JumpConfirmationResult, WorkflowJumpPlan


FIXED_UNCLEAR_REPLY = "我还不能确认你的意思。请回复“确认”继续这个跳转，或直接说明你想改成哪一步。"


def sanitize_confirmation_reply(reply: str, plan: WorkflowJumpPlan) -> str:
    text = _clean_text(reply)
    if not text:
        text = _default_confirmation_reply(plan)
    return text[:1000]


def clean_confirmation_result(raw: Any) -> JumpConfirmationResult:
    if not isinstance(raw, dict):
        return JumpConfirmationResult("unclear", clarification=FIXED_UNCLEAR_REPLY)
    decision = str(raw.get("decision") or raw.get("intent") or "").strip().lower()
    if decision in {"confirm", "yes", "ok", "确认", "继续"}:
        decision = "confirm"
    elif decision in {"reject", "no", "cancel", "拒绝", "取消", "不是"}:
        decision = "reject"
    elif decision != "unclear":
        decision = "unclear"
    clarification = _clean_text(str(raw.get("clarification") or raw.get("reply") or ""))
    if decision == "unclear" and not clarification:
        clarification = FIXED_UNCLEAR_REPLY
    return JumpConfirmationResult(
        decision=decision,
        reason=_clean_text(str(raw.get("reason") or "")),
        clarification=clarification,
    )


def rule_hint_for_confirmation(text: str) -> str:
    source = text or ""
    if re.search(r"^\s*(确认|可以|对|是|继续|ok|yes)\s*[。.!！]?\s*$", source, flags=re.IGNORECASE):
        return "规则辅助判断：用户回复可能是明显确认。"
    if re.search(r"不是|不对|拒绝|取消|不是这个意思|别", source, flags=re.IGNORECASE):
        return "规则辅助判断：用户回复可能是拒绝或澄清。"
    return "规则辅助判断：不明显，需要根据上下文判断。"


def _default_confirmation_reply(plan: WorkflowJumpPlan) -> str:
    return (
        f"我理解你想从当前步骤跳到 {plan.target_state}，执行 {plan.action}。"
        "确认后我会继续处理；如果不是这个意思，请直接说明要改哪一步。"
    )


def _clean_text(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"(?is)<think(?:ing)?\b[^>]*>.*?</think(?:ing)?>", "", value)
    value = re.sub(r"(?is)</?think(?:ing)?>", "", value)
    value = value.replace("\ufffd", "")
    value = "".join(ch for ch in value if ch in "\n\t" or ord(ch) >= 32)
    return re.sub(r"\n{3,}", "\n\n", value).strip()
