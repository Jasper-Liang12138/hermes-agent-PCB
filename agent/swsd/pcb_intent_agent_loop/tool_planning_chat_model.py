"""Tool-planning-chat backed intent model for PCB SWSD loops."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from agent.swsd.action_candidates import ActionCandidate, IntentCandidateSet
from agent.swsd.decision_policy import ActionEvidence, PolicyEvidenceSet
from agent.swsd.pcb_intent_agent_loop.loops import IntentAgentLoopInput, LocalRuleIntentModel
from tools import pcb_model_runtime

TraceSink = Callable[[str, dict[str, Any]], None]


def _json_candidates_from_text(raw: str) -> list[Any]:
    text = str(raw or "").strip()
    if not text:
        return []
    candidates: list[str] = []
    fenced = re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
    candidates.extend(fenced)
    candidates.append(text)
    for open_char, close_char in (("{", "}"), ("[", "]")):
        start = text.find(open_char)
        end = text.rfind(close_char)
        if start >= 0 and end > start:
            candidates.insert(0, text[start : end + 1])
    parsed_values: list[Any] = []
    seen: set[str] = set()
    for candidate in candidates:
        source = str(candidate or "").strip()
        if not source or source in seen:
            continue
        seen.add(source)
        try:
            parsed_values.append(json.loads(source))
        except json.JSONDecodeError:
            continue
    return parsed_values


def _json_object_from_text(raw: str) -> dict[str, Any]:
    for parsed in _json_candidates_from_text(raw):
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"candidateActions": parsed}
    return {}


def _candidate_items_from_any(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("candidateActions", "candidate_actions", "actionCandidates", "actions", "candidates"):
            raw = value.get(key)
            if isinstance(raw, list):
                return raw
        if value.get("action") or value.get("intent"):
            return [value]
    return []


def _normalize_entities(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return dict(parsed)
        except json.JSONDecodeError:
            return {"raw": value.strip()}
    return {}


def _normalize_candidate_set(data: Any, request: IntentAgentLoopInput, *, model_source: str) -> IntentCandidateSet:
    obj = data if isinstance(data, dict) else {"candidateActions": data if isinstance(data, list) else []}
    raw_actions = _candidate_items_from_any(obj)
    candidates: list[ActionCandidate] = []
    for item in raw_actions:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or item.get("intent") or item.get("name") or "").strip()
        if not action:
            continue
        try:
            confidence = float(item.get("confidence", item.get("score", 0.0)))
        except (TypeError, ValueError):
            confidence = 0.0
        candidates.append(
            ActionCandidate(
                action=action,
                confidence=max(0.0, min(1.0, confidence)),
                entities=_normalize_entities(item.get("entities")),
                reason=str(item.get("reason") or item.get("why") or "").strip(),
                source=str(item.get("source") or obj.get("modelSource") or model_source or "tool_planning_chat"),
            )
        )
    if not candidates:
        candidates.extend(_entity_only_candidates(obj, request, model_source=model_source))
    return IntentCandidateSet(
        workflow=str(obj.get("workflow") or obj.get("workflowId") or request.workflow_id or "").strip(),
        current_state=str(obj.get("currentState") or obj.get("current_state") or obj.get("state") or request.workflow_state or "").strip(),
        candidate_actions=tuple(candidates),
        model_source=str(obj.get("modelSource") or obj.get("model_source") or model_source or "tool_planning_chat").strip(),
    )



def _entity_only_candidates(obj: dict[str, Any], request: IntentAgentLoopInput, *, model_source: str) -> tuple[ActionCandidate, ...]:
    allowed = set(request.allowed_actions or ())
    candidates: list[ActionCandidate] = []

    def add(action: str, confidence: float, entities: dict[str, Any], reason: str) -> None:
        if allowed and action not in allowed:
            return
        candidates.append(ActionCandidate(action, confidence, entities, reason, model_source or "tool_planning_chat"))

    router_type = str(obj.get("routerType") or obj.get("router_type") or "").strip()
    if router_type:
        entities = {"routerType": router_type}
        add("layer_assigned", 0.86, entities, "model returned routerType entity")
        add("modify_params", 0.82, entities, "model returned routerType entity")
    if obj.get("question_type") or obj.get("questionType"):
        add("chat", 0.82, {"questionType": str(obj.get("question_type") or obj.get("questionType"))}, "model returned question type")
    target = str(obj.get("target") or obj.get("targetBGA") or obj.get("selectedBGA") or "").strip()
    if target:
        add("select_target", 0.84, {"selectedBGA": target}, "model returned target BGA entity")
        add("change_target", 0.80, {"selectedBGA": target}, "model returned target BGA entity")
    return tuple(candidates)


def _action_evidence_dict(item: ActionEvidence) -> dict[str, Any]:
    return {
        "action": item.action,
        "confidence": item.confidence,
        "evidence": list(item.evidence),
        "risk": item.risk,
        "hard_reject": item.hard_reject,
        "reason": item.reason,
    }


def _policy_evidence_dict(evidence_set: PolicyEvidenceSet) -> dict[str, Any]:
    return {
        "top_candidates": [_action_evidence_dict(item) for item in evidence_set.top_candidates],
        "reason": evidence_set.reason,
    }


def _normalize_refined_evidence(parsed: dict[str, Any], evidence_set: PolicyEvidenceSet) -> PolicyEvidenceSet:
    raw_items = parsed.get("refined_candidates") or parsed.get("candidates") or parsed.get("top_candidates") or []
    if not isinstance(raw_items, list):
        return evidence_set
    by_action = {item.action: item for item in evidence_set.top_candidates}
    refined: list[ActionEvidence] = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        action = str(raw.get("action") or "").strip()
        base = by_action.get(action)
        if base is None:
            continue
        raw_evidence = raw.get("evidence")
        if isinstance(raw_evidence, list):
            evidence = tuple(str(item) for item in raw_evidence if str(item).strip())
        elif isinstance(raw_evidence, str) and raw_evidence.strip():
            evidence = (raw_evidence.strip(),)
        else:
            evidence = base.evidence
        refined.append(
            ActionEvidence(
                action=base.action,
                confidence=base.confidence,
                candidate=base.candidate,
                evidence=evidence,
                risk=str(raw.get("risk") or base.risk or ""),
                hard_reject=base.hard_reject,
                reason=str(raw.get("reason") or base.reason or "refined_evidence"),
            )
        )
    if not refined:
        return evidence_set
    ordered = tuple(refined + [item for item in evidence_set.top_candidates if item.action not in {entry.action for entry in refined}])
    return PolicyEvidenceSet(top_candidates=ordered[: len(evidence_set.top_candidates)], rejected_candidates=evidence_set.rejected_candidates, reason="expert_c_refined_evidence")

def file_trace_sink(trace_dir: str | Path) -> TraceSink:
    root = Path(trace_dir)
    root.mkdir(parents=True, exist_ok=True)

    def sink(stage: str, payload: dict[str, Any]) -> None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        name = f"{stamp}-{int(time.time() * 1000) % 100000:05d}-{stage}.json"
        (root / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    return sink


class ToolPlanningChatIntentModel(LocalRuleIntentModel):
    """Expert A/B/C adapter using config.ini ``[tool-planning-chat-model]``."""

    def __init__(self, *, timeout_s: float = 8.0, trace_sink: TraceSink | None = None) -> None:
        self.timeout_s = timeout_s
        self.trace_sink = trace_sink

    def _trace(self, stage: str, payload: dict[str, Any]) -> None:
        if self.trace_sink is None:
            return
        try:
            self.trace_sink(stage, payload)
        except Exception:
            return

    def propose_candidates(self, request: IntentAgentLoopInput, feedback: tuple[str, ...] = ()) -> IntentCandidateSet:
        messages = self._proposal_messages(request, feedback)
        try:
            content, meta = pcb_model_runtime.chat_completion_text(
                stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                messages=messages,
                timeout_s=self.timeout_s,
                max_tokens=4096,
                temperature=0,
                top_p=1,
                stream_until_json=True,
            )
            parsed = _json_object_from_text(content)
            candidate_set = _normalize_candidate_set(parsed, request, model_source=str(meta.get("stage") or "tool_planning_chat"))
            self._trace("proposal", {"request": self._base_payload(request), "feedback": list(feedback), "messages": messages, "raw_output": content, "parsed": parsed, "candidate_set": _candidate_set_dict(candidate_set), "meta": meta})
            if candidate_set.candidate_actions:
                return candidate_set
        except Exception as exc:
            self._trace("proposal_error", {"request": self._base_payload(request), "feedback": list(feedback), "messages": messages, "error": str(exc)})
        return super().propose_candidates(request, feedback)


    def vote_action(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        evidence_set: PolicyEvidenceSet,
        *,
        model_stage: str = "tool_planning_chat",
        negative_feedback: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        messages = self._vote_messages(request, candidate_set, evidence_set, model_stage, negative_feedback)
        stage = pcb_model_runtime.STAGE_REROUTE if model_stage == "reroute" else pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT
        try:
            content, meta = pcb_model_runtime.chat_completion_text(
                stage=stage,
                messages=messages,
                timeout_s=self.timeout_s,
                max_tokens=1024 if stage == pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT else 4096,
                temperature=0,
                top_p=1,
                stream_until_json=True,
                extra_payload={"chat_template_kwargs": {"enable_thinking": False}, "enable_thinking": False}
                if stage == pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT
                else None,
            )
            parsed = _json_object_from_text(content)
            self._trace("vote", {"request": self._base_payload(request), "candidate_set": _candidate_set_dict(candidate_set), "evidence_set": _policy_evidence_dict(evidence_set), "model_stage": model_stage, "messages": messages, "raw_output": content, "parsed": parsed, "meta": meta})
            if parsed:
                return parsed
        except Exception as exc:
            self._trace("vote_error", {"request": self._base_payload(request), "candidate_set": _candidate_set_dict(candidate_set), "evidence_set": _policy_evidence_dict(evidence_set), "model_stage": model_stage, "messages": messages, "error": str(exc)})
        return super().vote_action(request, candidate_set, evidence_set, model_stage=model_stage, negative_feedback=negative_feedback)

    def refine_evidence(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        evidence_set: PolicyEvidenceSet,
        rejection_feedback: tuple[str, ...],
    ) -> PolicyEvidenceSet:
        messages = self._refine_evidence_messages(request, candidate_set, evidence_set, rejection_feedback)
        try:
            content, meta = pcb_model_runtime.chat_completion_text(
                stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                messages=messages,
                timeout_s=self.timeout_s,
                max_tokens=4096,
                temperature=0,
                top_p=1,
                stream_until_json=True,
            )
            parsed = _json_object_from_text(content)
            refined = _normalize_refined_evidence(parsed, evidence_set)
            self._trace("refine_evidence", {"request": self._base_payload(request), "candidate_set": _candidate_set_dict(candidate_set), "evidence_set": _policy_evidence_dict(evidence_set), "rejection_feedback": list(rejection_feedback), "messages": messages, "raw_output": content, "parsed": parsed, "refined": _policy_evidence_dict(refined), "meta": meta})
            return refined
        except Exception as exc:
            self._trace("refine_evidence_error", {"request": self._base_payload(request), "candidate_set": _candidate_set_dict(candidate_set), "evidence_set": _policy_evidence_dict(evidence_set), "rejection_feedback": list(rejection_feedback), "messages": messages, "error": str(exc)})
        return super().refine_evidence(request, candidate_set, evidence_set, rejection_feedback)

    def judge_candidates(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        policy_feedback: str = "",
    ) -> bool:
        messages = self._judge_messages(request, candidate_set, policy_feedback)
        try:
            content, meta = pcb_model_runtime.chat_completion_text(
                stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                messages=messages,
                timeout_s=self.timeout_s,
                max_tokens=4096,
                temperature=0,
                top_p=1,
                stream_until_json=True,
            )
            parsed = _json_object_from_text(content)
            accept = _parse_accept(parsed)
            self._trace("judge", {"request": self._base_payload(request), "candidate_set": _candidate_set_dict(candidate_set), "policy_feedback": policy_feedback, "messages": messages, "raw_output": content, "parsed": parsed, "accepted": accept, "meta": meta})
            if accept is not None:
                return accept
        except Exception as exc:
            self._trace("judge_error", {"request": self._base_payload(request), "candidate_set": _candidate_set_dict(candidate_set), "policy_feedback": policy_feedback, "messages": messages, "error": str(exc)})
        return super().judge_candidates(request, candidate_set, policy_feedback)

    def revise_candidates(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        rejection_feedback: tuple[str, ...],
    ) -> IntentCandidateSet:
        messages = self._revise_messages(request, candidate_set, rejection_feedback)
        try:
            content, meta = pcb_model_runtime.chat_completion_text(
                stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                messages=messages,
                timeout_s=self.timeout_s,
                max_tokens=4096,
                temperature=0,
                top_p=1,
                stream_until_json=True,
            )
            parsed = _json_object_from_text(content)
            revised = _normalize_candidate_set(parsed, request, model_source=str(meta.get("stage") or "tool_planning_chat"))
            self._trace("revise", {"request": self._base_payload(request), "candidate_set": _candidate_set_dict(candidate_set), "rejection_feedback": list(rejection_feedback), "messages": messages, "raw_output": content, "parsed": parsed, "revised": _candidate_set_dict(revised), "meta": meta})
            if revised.candidate_actions:
                return revised
        except Exception as exc:
            self._trace("revise_error", {"request": self._base_payload(request), "candidate_set": _candidate_set_dict(candidate_set), "rejection_feedback": list(rejection_feedback), "messages": messages, "error": str(exc)})
        return super().revise_candidates(request, candidate_set, rejection_feedback)

    def build_feedback_reply(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        rejection_feedback: tuple[str, ...],
    ) -> str:
        messages = self._feedback_messages(request, candidate_set, rejection_feedback)
        try:
            content, meta = pcb_model_runtime.chat_completion_text(
                stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                messages=messages,
                timeout_s=self.timeout_s,
                max_tokens=4096,
                temperature=0,
                top_p=1,
                stream_until_json=True,
            )
            parsed = _json_object_from_text(content)
            reply = str(parsed.get("reply") or parsed.get("question") or "").strip()
            self._trace("feedback", {"request": self._base_payload(request), "candidate_set": _candidate_set_dict(candidate_set), "rejection_feedback": list(rejection_feedback), "messages": messages, "raw_output": content, "parsed": parsed, "reply": reply, "meta": meta})
            if reply:
                return reply
        except Exception as exc:
            self._trace("feedback_error", {"request": self._base_payload(request), "candidate_set": _candidate_set_dict(candidate_set), "rejection_feedback": list(rejection_feedback), "messages": messages, "error": str(exc)})
        return super().build_feedback_reply(request, candidate_set, rejection_feedback)

    def _base_payload(self, request: IntentAgentLoopInput) -> dict[str, Any]:
        return {
            "user_text": request.user_text,
            "workflow": request.workflow_id,
            "currentState": request.workflow_state,
            "allowed_actions": list(request.allowed_actions),
            "explicit_fields": request.explicit_fields,
            "hints": request.hints,
            "fallback_candidates": [
                {
                    "action": item.action,
                    "confidence": item.confidence,
                    "entities": item.entities,
                    "reason": item.reason,
                    "source": item.source,
                }
                for item in request.fallback_candidates
            ],
        }

    def _proposal_messages(self, request: IntentAgentLoopInput, feedback: tuple[str, ...]) -> list[dict[str, str]]:
        system = """你是 PCB SWSD 意图识别专家 / Expert A。

任务：根据用户输入、当前 workflow/state、allowed_actions，生成状态内可执行的 ActionCandidate 列表。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 不要调用工具，不要执行 PCB 操作。
- action 必须来自 allowed_actions。
- JSON key 必须保持英文：workflow, currentState, candidateActions, action, confidence, entities, reason, modelSource。
- confidence 必须是 0 到 1 的数字。
- 用户主要使用中文，也可能夹杂 fanout、reroute、BGA、135+RL、routerType 等英文术语。
- 如果用户只是询问原因、解释、普通问题，优先输出 chat，不要触发 PCB 工具。
- “拆线重布 / reroute / rip-up”属于 reroute_entry，不属于全局 fanout。
- “fanout / 扇出 / 逃逸 / 给 Ux 布线”属于全局 fanout 入口 pcb_entry。
- 如果用户同时给出目标 BGA 或参数，把它们放入 entities，不要改写 action 名。

输出格式:
{
  "workflow": "pcb_escape_flow",
  "currentState": "review",
  "candidateActions": [
    {
      "action": "rollback_checkpoint",
      "confidence": 0.96,
      "entities": {},
      "reason": "用户要求回到上一步"
    }
  ],
  "modelSource": "tool_planning_chat"
}

例子:
- “回到上一步” => action=rollback_checkpoint
- “拆线重布” => action=reroute_entry
- “给 U5 做 fanout” => action=pcb_entry, entities={"selectedBGA":"U5"}
- “给 U5 和 U7 布线，线宽3mil，线距3mil” => action=pcb_entry, entities={"targetBGAs":["U5","U7"],"constraints":{"LineWidth":3,"LineSpacing":3}}
- “135 + RL” => action=layer_assigned, entities={"routerType":"135+RL"}
- “这个布线为什么这样走？” => action=chat
        """
        payload = self._base_payload(request)
        payload["validation_feedback"] = list(feedback)
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


    def _vote_messages(self, request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, evidence_set: PolicyEvidenceSet, model_stage: str, negative_feedback: tuple[str, ...]) -> list[dict[str, str]]:
        system = """你是 PCB SWSD 动作投票专家 / Expert B。

任务：基于原始输入、当前 workflow/state、memory/rules，以及 top-3 action evidence，独立选择唯一一个 selected_action。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 必须从 top_candidates 里选择一个 action。
- 只输出 selected_action，不要输出 workflow_id、state、phase。
- 不要调用工具，不要执行 PCB 操作。
- 如果当前 state 是 fanout review/param_review，用户说重新 fanout、重新来、改线宽/线距/参数，优先考虑 rerun_fanout 或 modify_params，不要泛化成 pcb_entry。
- 拆线重布 / reroute / rip-up 优先选择 reroute_entry 或 reroute_again。
- 普通解释性问题才选择 chat。

输出格式:
{
  "selected_action": "rerun_fanout",
  "confidence": 0.91,
  "reason": "当前处于 review，用户要求重新 fanout 并修改线宽，应优先重跑 fanout 参数"
}
"""
        payload = self._base_payload(request)
        payload["candidate_set"] = _candidate_set_dict(candidate_set)
        payload["candidate_evidence"] = _policy_evidence_dict(evidence_set)
        payload["expert_model_role"] = model_stage
        payload["negative_feedback"] = list(negative_feedback)
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _refine_evidence_messages(self, request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, evidence_set: PolicyEvidenceSet, rejection_feedback: tuple[str, ...]) -> list[dict[str, str]]:
        system = """你是 PCB SWSD 证据细化专家 / Expert C。

任务：在不改变 top-3 action 的前提下，细化每个 action 的 evidence，帮助 Expert B 下一轮投票。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 不允许新增 action，不允许删除 action，不允许重排 action。
- 不允许输出 workflow_id、state、phase。
- 只能补充或改写 evidence/risk/reason。
- 证据必须结合用户原始输入、current workflow/state、allowed_actions、memory/hints/rules。
- 如果当前 state 是 review/param_review，用户表达重新 fanout 或改参数，要明确指出 pcb_entry 的风险和 rerun_fanout/modify_params 的状态语义优势。

输出格式:
{
  "refined_candidates": [
    {
      "action": "rerun_fanout",
      "evidence": ["当前处于 review", "用户要求重新 fanout", "用户提出修改线宽"],
      "risk": "",
      "reason": "状态内重跑 fanout 更符合用户意图"
    }
  ]
}
"""
        payload = self._base_payload(request)
        payload["candidate_set"] = _candidate_set_dict(candidate_set)
        payload["candidate_evidence"] = _policy_evidence_dict(evidence_set)
        payload["rejection_feedback"] = list(rejection_feedback)
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _judge_messages(self, request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, policy_feedback: str) -> list[dict[str, str]]:
        system = """你是 PCB SWSD 仲裁专家 / Expert B。

任务：判断 candidateActions 是否能在当前 workflow/state 下被采纳。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 只能接受 allowed_actions 内的 action。
- 不允许因为用户普通提问而触发 PCB 工具。
- chat action 不应迁移 workflow state，也不应执行 PCB 工具。
- fanout 与 reroute 必须区分：全局 BGA fanout 不等于拆线重布。
- 如果候选 action 和用户意图明显不一致，accept=false。
- 如果缺少当前 state 必需的实体，可以 accept=false，并在 reason 里说明缺什么。
- 如果候选合理但 confidence 过低，可以 accept=false。

输出格式:
{
  "accept": true,
  "action": "layer_assigned",
  "confidence": 0.91,
  "reason": "用户明确选择了 routerType=135+RL，且 action 在 allowed_actions 内"
}
        """
        payload = self._base_payload(request)
        payload["candidate_set"] = _candidate_set_dict(candidate_set)
        payload["policy_feedback"] = policy_feedback
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _revise_messages(self, request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, rejection_feedback: tuple[str, ...]) -> list[dict[str, str]]:
        system = """你是 PCB SWSD 候选修正专家 / Expert C。

任务：根据拒绝原因、原始用户输入、当前 workflow/state，修正完整 candidateActions 列表。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 只输出完整 IntentCandidateSet JSON。
- action 必须来自 allowed_actions。
- JSON key 必须保持英文：workflow, currentState, candidateActions, action, confidence, entities, reason, modelSource。
- 不要调用工具，不要执行 PCB 操作。
- 修正时优先保留用户显式表达的目标、routerType、constraints。
- 如果无法确定业务动作，输出 chat 或低风险 clarify 类候选，不要编造 PCB 操作。

输出格式:
{
  "workflow": "pcb_escape_flow",
  "currentState": "review",
  "candidateActions": [
    {
      "action": "chat",
      "confidence": 0.8,
      "entities": {},
      "reason": "用户问题更像解释性聊天，不应执行 PCB 工具"
    }
  ],
  "modelSource": "tool_planning_chat"
}
        """
        payload = self._base_payload(request)
        payload["candidate_set"] = _candidate_set_dict(candidate_set)
        payload["rejection_feedback"] = list(rejection_feedback)
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _feedback_messages(self, request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, rejection_feedback: tuple[str, ...]) -> list[dict[str, str]]:
        system = """你是 PCB SWSD 反馈专家 / Expert C。

任务：当候选动作无法被采纳时，生成一句用户能理解的中文追问或缺失信息提示。

硬性规则:
- Return only JSON. No Markdown. No explanation.
- 只输出 {"reply": "..."}。
- reply 必须是自然中文。
- 不要暴露内部 action、state、confidence、votes、DecisionPolicy、stack trace。
- 不要输出乱码、JSON 片段、英文调试信息。
- 如果缺目标对象，提示用户选择或说明 BGA/走线。
- 如果缺参数，提示用户补充 routerType、线宽、线距等必要信息。

输出格式:
{
  "reply": "我还需要知道要处理哪个 BGA，例如 U5 或 U7。"
}
        """
        payload = self._base_payload(request)
        payload["candidate_set"] = _candidate_set_dict(candidate_set)
        payload["rejection_feedback"] = list(rejection_feedback)
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


def _parse_accept(parsed: dict[str, Any]) -> bool | None:
    if not isinstance(parsed, dict):
        return None
    for key in ("accept", "accepted", "shouldAccept"):
        if key in parsed:
            value = parsed.get(key)
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                lowered = value.strip().lower()
                if lowered in {"true", "yes", "accept", "accepted", "通过", "接受"}:
                    return True
                if lowered in {"false", "no", "reject", "rejected", "拒绝", "不通过"}:
                    return False
            return bool(value)
    return None


def _candidate_set_dict(candidate_set: IntentCandidateSet) -> dict[str, Any]:
    return {
        "workflow": candidate_set.workflow,
        "currentState": candidate_set.current_state,
        "candidateActions": [
            {
                "action": item.action,
                "confidence": item.confidence,
                "entities": item.entities,
                "reason": item.reason,
                "source": item.source,
            }
            for item in candidate_set.candidate_actions
        ],
        "modelSource": candidate_set.model_source,
    }
