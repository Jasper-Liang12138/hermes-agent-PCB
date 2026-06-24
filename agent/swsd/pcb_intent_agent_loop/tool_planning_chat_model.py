"""Tool-planning-chat backed intent model for PCB SWSD loops."""

from __future__ import annotations

import json
from typing import Any

from agent.swsd.action_candidates import ActionCandidate, IntentCandidateSet
from agent.swsd.pcb_intent_agent_loop.loops import IntentAgentLoopInput, LocalRuleIntentModel
from tools import pcb_model_runtime


def _json_object_from_text(raw: str) -> dict[str, Any]:
    text = str(raw or "").strip()
    candidates = [text]
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


class ToolPlanningChatIntentModel(LocalRuleIntentModel):
    """Expert A/B/C adapter using config.ini ``[tool-planning-chat-model]``.

    All calls go through ``pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT`` so the
    runtime resolves the existing tool-planning-chat model section and applies
    its no-thinking/json defaults.
    """

    def __init__(self, *, timeout_s: float = 8.0) -> None:
        self.timeout_s = timeout_s

    def propose_candidates(self, request: IntentAgentLoopInput, feedback: tuple[str, ...] = ()) -> IntentCandidateSet:
        try:
            content, meta = pcb_model_runtime.chat_completion_text(
                stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                messages=self._proposal_messages(request, feedback),
                timeout_s=self.timeout_s,
                max_tokens=768,
                temperature=0,
                top_p=1,
                stream_until_json=True,
            )
            parsed = _json_object_from_text(content)
            candidate_set = IntentCandidateSet.from_mapping(parsed)
            if candidate_set.candidate_actions:
                return IntentCandidateSet(
                    workflow=candidate_set.workflow or request.workflow_id,
                    current_state=candidate_set.current_state or request.workflow_state,
                    candidate_actions=tuple(
                        ActionCandidate(
                            candidate.action,
                            candidate.confidence,
                            candidate.entities,
                            candidate.reason,
                            candidate.source or "tool_planning_chat",
                        )
                        for candidate in candidate_set.candidate_actions
                    ),
                    model_source=str(parsed.get("modelSource") or meta.get("stage") or "tool_planning_chat"),
                )
        except Exception:
            pass
        return super().propose_candidates(request, feedback)

    def judge_candidates(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        policy_feedback: str = "",
    ) -> bool:
        try:
            content, _meta = pcb_model_runtime.chat_completion_text(
                stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                messages=self._judge_messages(request, candidate_set, policy_feedback),
                timeout_s=self.timeout_s,
                max_tokens=256,
                temperature=0,
                top_p=1,
                stream_until_json=True,
            )
            parsed = _json_object_from_text(content)
            if "accept" in parsed:
                return bool(parsed.get("accept"))
        except Exception:
            pass
        return super().judge_candidates(request, candidate_set, policy_feedback)

    def revise_candidates(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        rejection_feedback: tuple[str, ...],
    ) -> IntentCandidateSet:
        try:
            content, _meta = pcb_model_runtime.chat_completion_text(
                stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                messages=self._revise_messages(request, candidate_set, rejection_feedback),
                timeout_s=self.timeout_s,
                max_tokens=768,
                temperature=0,
                top_p=1,
                stream_until_json=True,
            )
            parsed = _json_object_from_text(content)
            revised = IntentCandidateSet.from_mapping(parsed)
            if revised.candidate_actions:
                return IntentCandidateSet(
                    workflow=revised.workflow or request.workflow_id,
                    current_state=revised.current_state or request.workflow_state,
                    candidate_actions=revised.candidate_actions,
                    model_source=revised.model_source or "tool_planning_chat",
                )
        except Exception:
            pass
        return super().revise_candidates(request, candidate_set, rejection_feedback)

    def build_feedback_reply(
        self,
        request: IntentAgentLoopInput,
        candidate_set: IntentCandidateSet,
        rejection_feedback: tuple[str, ...],
    ) -> str:
        try:
            content, _meta = pcb_model_runtime.chat_completion_text(
                stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
                messages=self._feedback_messages(request, candidate_set, rejection_feedback),
                timeout_s=self.timeout_s,
                max_tokens=256,
                temperature=0,
                top_p=1,
                stream_until_json=True,
            )
            parsed = _json_object_from_text(content)
            reply = str(parsed.get("reply") or parsed.get("question") or "").strip()
            if reply:
                return reply
        except Exception:
            pass
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
            "You are Expert A for PCB SWSD intent recognition. Return only JSON matching "
            "{\"workflow\": str, \"currentState\": str, \"candidateActions\": ["
            "{\"action\": str, \"confidence\": 0..1, \"entities\": object, \"reason\": str}], "
            "\"modelSource\": \"tool_planning_chat\"}. Use only allowed_actions. Do not call tools."
        )
        payload = self._base_payload(request)
        payload["validation_feedback"] = list(feedback)
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _judge_messages(self, request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, policy_feedback: str) -> list[dict[str, str]]:
        system = "You are Expert B. Return only JSON: {\"accept\": true|false, \"reason\": str}. Do not call tools."
        payload = self._base_payload(request)
        payload["candidate_set"] = _candidate_set_dict(candidate_set)
        payload["policy_feedback"] = policy_feedback
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _revise_messages(self, request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, rejection_feedback: tuple[str, ...]) -> list[dict[str, str]]:
        system = "You are Expert C. Repair the whole candidateActions list. Return only the IntentCandidateSet JSON. Do not call tools."
        payload = self._base_payload(request)
        payload["candidate_set"] = _candidate_set_dict(candidate_set)
        payload["rejection_feedback"] = list(rejection_feedback)
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]

    def _feedback_messages(self, request: IntentAgentLoopInput, candidate_set: IntentCandidateSet, rejection_feedback: tuple[str, ...]) -> list[dict[str, str]]:
        system = "You are Expert C. Generate a concise Chinese clarification question. Return only JSON: {\"reply\": str}."
        payload = self._base_payload(request)
        payload["candidate_set"] = _candidate_set_dict(candidate_set)
        payload["rejection_feedback"] = list(rejection_feedback)
        return [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}]


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
