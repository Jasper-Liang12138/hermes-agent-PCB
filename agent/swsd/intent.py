"""SWSD intent understanding with tool-planning-chat model isolation."""

from __future__ import annotations

import json
import re
from typing import Any

from tools import pcb_model_runtime


_CANCEL_RE = re.compile(r"取消|退出|中止|停止|cancel|abort", re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"回到上一步|上一步|rollback|回退", re.IGNORECASE)
_REROUTE_RE = re.compile(r"拆线|重布|重新布|reroute|rip-?up", re.IGNORECASE)
_CONFIRM_RE = re.compile(r"确认|继续|执行|开始|go|yes|ok", re.IGNORECASE)
_CHANGE_TARGET_RE = re.compile(r"目标.*改|换.*BGA|选择|选\s*[A-Za-z]", re.IGNORECASE)


def classify_intent_rules(text: str, current_state: str = "") -> str:
    text = str(text or "")
    if _CANCEL_RE.search(text):
        return "cancel"
    if _ROLLBACK_RE.search(text):
        return "rollback"
    if _REROUTE_RE.search(text):
        return "pcb_reroute_selected"
    if _CHANGE_TARGET_RE.search(text):
        return "select_target" if current_state in {"select_bga", "idle", ""} else "change_target"
    if _CONFIRM_RE.search(text):
        return "confirm_route"
    return "chat"


def classify_intent_with_planning_model(
    text: str,
    *,
    current_state: str = "",
    workflow_id: str = "",
    timeout_s: float = 8.0,
) -> dict[str, Any]:
    """Classify SWSD intent using the tool-planning-chat stage only."""
    system = (
        "You classify PCB workflow intent for SWSD. Return only JSON with keys "
        "intent and confidence. Never generate reroute geometry."
    )
    user = json.dumps(
        {"text": text, "workflow": workflow_id, "currentState": current_state},
        ensure_ascii=False,
    )
    content, meta = pcb_model_runtime.chat_completion_text(
        stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        timeout_s=timeout_s,
        max_tokens=256,
        temperature=0,
        top_p=1,
    )
    try:
        parsed = json.loads(content)
        if isinstance(parsed, dict):
            parsed.setdefault("modelStage", meta.get("stage") or pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT)
            return parsed
    except Exception:
        pass
    return {
        "intent": classify_intent_rules(text, current_state=current_state),
        "confidence": 0.0,
        "modelStage": meta.get("stage") or pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
        "raw": content,
    }
