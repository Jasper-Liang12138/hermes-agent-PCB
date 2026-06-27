"""Tool-planning-chat backed Expert G/H for SWSD jump decisions."""

from __future__ import annotations

import json
import re
from typing import Any

from tools import pcb_model_runtime

from .models import JumpConfirmationResult, JumpIntentLoopInput, RetrievedJumpPrior, WorkflowJumpPlan
from .pseudo_expert_h import clean_confirmation_result, rule_hint_for_confirmation, sanitize_confirmation_reply


def _json_object_from_text(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    if not text:
        return {}
    candidates = [text]
    candidates.extend(re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE))
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.insert(0, text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


class ToolPlanningChatJumpModel:
    """Expert G/H adapter using config.ini [tool-planning-chat-model]."""

    def __init__(self, *, timeout_s: float = 8.0) -> None:
        self.timeout_s = timeout_s

    def propose_jump_plan(
        self,
        request: JumpIntentLoopInput,
        prior: RetrievedJumpPrior,
        feedback: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        content, meta = pcb_model_runtime.chat_completion_text(
            stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
            messages=self._jump_messages(request, prior, feedback),
            timeout_s=self.timeout_s,
            max_tokens=4096,
            temperature=0,
            top_p=1,
            stream=False,
            stream_until_json=False,
        )
        parsed = _json_object_from_text(content)
        parsed.setdefault("__raw_output", content)
        parsed.setdefault("__meta", meta)
        return parsed

    def build_confirmation_reply(self, plan: WorkflowJumpPlan, request: JumpIntentLoopInput) -> str:
        content, _meta = pcb_model_runtime.chat_completion_text(
            stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
            messages=self._confirmation_messages(plan, request),
            timeout_s=self.timeout_s,
            max_tokens=4096,
            temperature=0,
            top_p=1,
            stream_until_json=True,
        )
        parsed = _json_object_from_text(content)
        return sanitize_confirmation_reply(str(parsed.get("reply") or ""), plan)

    def judge_confirmation(
        self,
        *,
        user_text: str,
        pending_plan: WorkflowJumpPlan,
        rule_hint: str = "",
    ) -> JumpConfirmationResult:
        content, _meta = pcb_model_runtime.chat_completion_text(
            stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
            messages=self._judge_confirmation_messages(user_text, pending_plan, rule_hint or rule_hint_for_confirmation(user_text)),
            timeout_s=self.timeout_s,
            max_tokens=4096,
            temperature=0,
            top_p=1,
            stream_until_json=True,
        )
        return clean_confirmation_result(_json_object_from_text(content))

    def _jump_messages(
        self,
        request: JumpIntentLoopInput,
        prior: RetrievedJumpPrior,
        feedback: tuple[str, ...],
    ) -> list[dict[str, str]]:
        system = """输出必须是一个 JSON object，且只能包含下面这些 key：
{
  "workflow_id": "pcb_escape_flow",
  "from_state": "review",
  "action": "rerun_fanout",
  "target_state": "layer_assign_escape_order",
  "confidence": 0.92,
  "entities": {"constraints": {"LineWidth": 3}, "raw_constraints": {"line_width": "3mil"}},
  "reason": "用户要求重新 fanout 并修改线宽",
  "requires_clarification": false,
  "clarification": ""
}

你是 PCB SWSD 跳转意图专家 / Expert G。

任务：根据用户自然语言、当前 workflow/state、精简 state graph、RAG jump prior，判断是否存在合法 workflow jump，并输出唯一 WorkflowJumpPlan。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 禁止输出 "intent jump state" 这类自然语言标签。
- 不要调用工具，不要执行 PCB 操作，不要迁移状态。
- JSON key 必须保持英文：workflow_id, from_state, action, target_state, confidence, entities, reason, requires_clarification, clarification。
- workflow_id 只能是 pcb_escape_flow 或 pcb_reroute_flow。
- target_state 必须来自 full_state_graph 对应 workflow 的 states。
- action 必须来自 allowed_jump_actions。
- 如果用户要求重新 fanout、重跑 fanout 或因不满意要改线宽/线距，优先 action=rerun_fanout, target_state=layer_assign_escape_order。
- 如果用户只表达修改线宽/线距/参数/routerType，优先 action=modify_params, target_state=layer_assign_escape_order。
- 如果用户要求重新选择或更换 BGA，输出 action=change_target, target_state=select_bga。
- 如果用户说拆线重布 / reroute / rip-up，输出 action=reroute_entry, workflow_id=pcb_reroute_flow, target_state=rip_up。
- 如果当前在 reroute 流程，用户要求给某个 BGA 做 fanout，输出 action=pcb_entry, workflow_id=pcb_escape_flow, target_state=select_bga。
- 如果 RAG prior 不支持该跳转，输出 requires_clarification=true，并在 clarification 里用中文说明需要用户说清楚目标步骤。

Few-shot examples:
用户：重新fanout，要改线宽为3mil
输出：{"workflow_id":"pcb_escape_flow","from_state":"review","action":"rerun_fanout","target_state":"layer_assign_escape_order","confidence":0.94,"entities":{"constraints":{"LineWidth":3},"raw_constraints":{"line_width":"3mil"}},"reason":"用户要求重新 fanout 并修改线宽","requires_clarification":false,"clarification":""}

用户：重新选择 U7 再 fanout
输出：{"workflow_id":"pcb_escape_flow","from_state":"review","action":"change_target","target_state":"select_bga","confidence":0.93,"entities":{"selectedBGA":"U7","targetBGAs":["U7"]},"reason":"用户要求重新选择 U7 作为目标 BGA","requires_clarification":false,"clarification":""}

用户：拆线重布
输出：{"workflow_id":"pcb_reroute_flow","from_state":"review","action":"reroute_entry","target_state":"rip_up","confidence":0.95,"entities":{},"reason":"用户要求进入局部拆线重布流程","requires_clarification":false,"clarification":""}

用户：给 U5 做 fanout
输出：{"workflow_id":"pcb_escape_flow","from_state":"report","action":"pcb_entry","target_state":"select_bga","confidence":0.92,"entities":{"selectedBGA":"U5","targetBGAs":["U5"]},"reason":"用户要求从当前流程切到 fanout 入口","requires_clarification":false,"clarification":""}
"""
        payload = {
            "user_text": request.user_text,
            "current": {"workflow_id": request.workflow_id, "state": request.workflow_state},
            "full_state_graph": request.state_graph,
            "state_payload_summary": request.state_payload_summary,
            "entities": request.entities,
            "rejection_context": request.rejection_context,
            "jump_prior": {"path": prior.path, "title": prior.title, "score": prior.score, "content": prior.content},
            "allowed_jump_actions": [
                "modify_params",
                "modify_router_choice",
                "change_target",
                "rerun_fanout",
                "rollback_checkpoint",
                "confirm_route",
                "confirm_import",
                "reject_import",
                "reroute_entry",
                "pcb_entry",
                "resume_workflow",
                "clarify",
            ],
            "validation_feedback": list(feedback),
        }
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]

    def _confirmation_messages(self, plan: WorkflowJumpPlan, request: JumpIntentLoopInput) -> list[dict[str, str]]:
        system = """你是 PCB SWSD 跳转确认专家 / Expert H。

任务：把 WorkflowJumpPlan 整理成用户能理解的一句中文确认提示。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 只输出 {"reply": "..."}。
- reply 必须是自然中文，最多两句话。
- 不要暴露内部 votes、candidateActions、DecisionPolicy、stack trace。
- 要说明确认后会跳到哪个用户可理解的步骤；如果不符合用户要求，让用户拒绝并重新说明。
- 如果 entities 里有线宽、线距、BGA、routerType，要用中文简要复述。
"""
        payload = {"plan": plan.as_dict(), "user_text": request.user_text, "current": {"workflow_id": request.workflow_id, "state": request.workflow_state}}
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]

    def _judge_confirmation_messages(self, user_text: str, pending_plan: WorkflowJumpPlan, rule_hint: str) -> list[dict[str, str]]:
        system = """你是 PCB SWSD 跳转确认分类专家 / Expert H。

任务：判断用户对 pending WorkflowJumpPlan 的回复是确认、拒绝/澄清，还是表达不清。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- decision 只能是 confirm、reject、unclear。
- confirm 表示用户同意执行 pending jump。
- reject 表示用户否定这个 jump，或提出了新的澄清/新目标。
- unclear 表示无法判断。
- 不要执行工具，不要迁移 workflow state。

输出格式:
{"decision": "confirm", "reason": "用户明确确认", "clarification": ""}
"""
        payload = {"user_text": user_text, "pending_plan": pending_plan.as_dict(), "rule_hint": rule_hint}
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False, default=str)}]
