"""Tool-planning-chat backed intent model for PCB SWSD loops."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any, Callable

from agent.swsd.action_candidates import ActionCandidate, IntentCandidateSet
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
                max_tokens=768,
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
                max_tokens=256,
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
                max_tokens=768,
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
                max_tokens=256,
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
        system = (
            "你是PCB SWSD意图识别专家A / Expert A. 只输出一个JSON object，禁止Markdown，禁止解释，禁止调用工具。"
            "Top-level keys must be: workflow, currentState, candidateActions, modelSource. "
            "candidateActions must be an array of {action, confidence, entities, reason}. "
            "action必须来自allowed_actions；confidence必须是0到1。"
            "Example: {\"workflow\":\"pcb_escape_flow\",\"currentState\":\"review\",\"candidateActions\":[{\"action\":\"rollback_checkpoint\",\"confidence\":0.96,\"entities\":{},\"reason\":\"用户要求回到上一步\"}],\"modelSource\":\"tool_planning_chat\"}."
        )
        payload = self._base_payload(request)
        payload["validation_feedback"] = list(feedback)
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _judge_messages(self, request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, policy_feedback: str) -> list[dict[str, str]]:
        system = (
            "你是PCB SWSD仲裁专家B / Expert B. 只输出JSON，禁止Markdown。"
            "Decide whether candidateActions should be accepted in the current workflow state. "
            "Return {\"accept\":true|false,\"action\":\"...\",\"confidence\":0..1,\"reason\":\"...\"}. "
            "If action is not allowed, accept must be false."
        )
        payload = self._base_payload(request)
        payload["candidate_set"] = _candidate_set_dict(candidate_set)
        payload["policy_feedback"] = policy_feedback
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _revise_messages(self, request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, rejection_feedback: tuple[str, ...]) -> list[dict[str, str]]:
        system = (
            "你是PCB SWSD修正专家C / Expert C. 根据拒绝原因修正完整candidateActions列表。"
            "只输出IntentCandidateSet JSON: workflow/currentState/candidateActions/modelSource。禁止Markdown，禁止解释，禁止调用工具。"
        )
        payload = self._base_payload(request)
        payload["candidate_set"] = _candidate_set_dict(candidate_set)
        payload["rejection_feedback"] = list(rejection_feedback)
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _feedback_messages(self, request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, rejection_feedback: tuple[str, ...]) -> list[dict[str, str]]:
        system = "你是PCB SWSD反馈专家C. 生成一句简短中文追问或缺失信息提示。只输出JSON: {\"reply\": str}。"
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
