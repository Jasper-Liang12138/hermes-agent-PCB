"""SWSD intent understanding with tool-planning-chat model isolation."""

from __future__ import annotations

import json
import re
from typing import Any

from agent.swsd.control_signals import matches_cancel_signal, matches_confirm_signal, matches_rollback_signal
from tools import pcb_model_runtime


_CANCEL_RE = re.compile(r"取消|退出|中止|停止|cancel|abort", re.IGNORECASE)
_ROLLBACK_RE = re.compile(r"回到上一步|上一步|rollback|回退", re.IGNORECASE)
_REROUTE_RE = re.compile(r"拆线|重布|重新布|reroute|rip-?up", re.IGNORECASE)
_CONFIRM_RE = re.compile(r"确认|继续|执行|开始|go|yes|ok", re.IGNORECASE)
_CHANGE_TARGET_RE = re.compile(r"目标.*改|换.*BGA|选择|选\s*[A-Za-z]", re.IGNORECASE)


def classify_intent_rules(text: str, current_state: str = "") -> str:
    text = str(text or "")
    if matches_cancel_signal(text):
        return "cancel"
    if matches_rollback_signal(text):
        return "rollback"
    if _REROUTE_RE.search(text):
        return "pcb_reroute_selected"
    if _CHANGE_TARGET_RE.search(text):
        return "select_target" if current_state in {"select_bga", "idle", ""} else "change_target"
    if matches_confirm_signal(text):
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
    system = """你是 PCB SWSD 轻量意图分类器。

任务：根据用户输入和当前状态，输出一个简短 JSON 分类结果。此分类器只用于兼容或辅助路径；实时主路径以 pcb_intent_agent_loop 的多阶段仲裁结果为准。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 不要调用工具。
- 不要执行 PCB 操作。
- intent/action 必须使用英文枚举值，不要翻译。
- 如果不能确定，输出 chat 或 clarify，不要编造 PCB 操作。
- “拆线重布”归为 reroute，不归为 fanout。
- “fanout/扇出/逃逸/给 Ux 布线”归为 fanout。

输出 JSON 必须包含当前代码要求的 key。
    """
    user = json.dumps(
        {"text": text, "workflow": workflow_id, "currentState": current_state},
        ensure_ascii=False,
    )
    content, meta = pcb_model_runtime.chat_completion_text(
        stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        timeout_s=timeout_s,
        max_tokens=4096,
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



