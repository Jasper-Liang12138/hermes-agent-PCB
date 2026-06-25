"""Run live tool-planning-chat evaluation for the SWSD intent loop chain.

This script is intentionally read-only. It exercises:

    IntentAgentLoopInput
    -> run_pcb_intent_agent_loops(...)
    -> IntentAgentLoopResult
    -> WorkflowActionPlan normalization

against the real ``[tool-planning-chat-model]`` runtime.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
from typing import Any

from agent.swsd.pcb_intent_agent_loop import (
    IntentAgentLoopInput,
    ToolPlanningChatIntentModel,
    file_trace_sink,
    run_pcb_intent_agent_loops,
)
from agent.swsd.workflow_controller import WebSocketWorkflowController


DEFAULT_CASES = (
    {
        "name": "rollback_review",
        "user_text": "回到上一步",
        "workflow_id": "pcb_escape_flow",
        "workflow_state": "review",
        "allowed_actions": ["rollback_checkpoint", "modify_params", "confirm_import", "chat"],
    },
    {
        "name": "router_choice",
        "user_text": "135 + RL",
        "workflow_id": "pcb_escape_flow",
        "workflow_state": "layer_assign",
        "allowed_actions": ["layer_assigned", "modify_params", "chat"],
        "fallback_candidates": [
            {
                "action": "layer_assigned",
                "confidence": 0.88,
                "entities": {"routerType": "135+RL"},
                "reason": "router choice resolved",
                "source": "fallback_rules",
            }
        ],
    },
    {
        "name": "reroute_entry",
        "user_text": "拆线重布",
        "workflow_id": "pcb_reroute_flow",
        "workflow_state": "idle",
        "allowed_actions": ["reroute_entry", "chat", "cancel_flow"],
    },
    {
        "name": "ambiguous_modify",
        "user_text": "改一下",
        "workflow_id": "pcb_escape_flow",
        "workflow_state": "review",
        "allowed_actions": ["modify_params", "modify_order_lines", "change_target", "chat"],
    },
    {
        "name": "workflow_chat",
        "user_text": "这个布线为什么这样走？",
        "workflow_id": "pcb_escape_flow",
        "workflow_state": "review",
        "allowed_actions": ["chat", "rollback_checkpoint", "modify_params"],
    },
)


class _EvalBridge:
    flow_idle = "idle"


class _EvalAdapter:
    _swsd_intent_model = None

    def __init__(self) -> None:
        self.mode = "chat"
        self._session_flow_states: dict[str, str] = {}

    def _session_mode(self, session_id: str) -> str:
        return self.mode

    def _reset_flow(self, session_id: str) -> None:
        return None

    def _set_flow_state(self, session_id: str, state: str) -> None:
        self._session_flow_states[session_id] = state

    def _set_session_mode(self, session_id: str, mode: str, lock_seconds: float | None = None) -> None:
        self.mode = mode


def _make_controller() -> WebSocketWorkflowController:
    return WebSocketWorkflowController(
        _EvalAdapter(),
        bridge=_EvalBridge(),
        escape_flow_id="pcb_escape_flow",
        reroute_flow_id="pcb_reroute_flow",
        route_mode_pcb="pcb",
        flow_wait_router_type="wait_router_type",
        flow_routing="routing",
        flow_reroute="reroute",
        intent_pcb_followup="pcb_followup",
        intent_pcb_reroute_selected="pcb_reroute_selected",
        intent_pcb_select_target="pcb_select_target",
        intent_pcb_confirm_route="pcb_confirm_route",
        confirm_re=None,
    )


def _action_candidate_dicts(items: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None) -> tuple[Any, ...]:
    from agent.swsd.action_candidates import ActionCandidate

    result = []
    for item in items or ():
        if not isinstance(item, dict):
            continue
        result.append(ActionCandidate.from_mapping(item, source=str(item.get("source") or "case")))
    return tuple(result)


def load_cases(path: Path | None) -> list[dict[str, Any]]:
    if path is None:
        return list(DEFAULT_CASES)
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, dict):
        data = data.get("cases")
    if not isinstance(data, list):
        raise ValueError("case file must contain a JSON array or {\"cases\": [...]} object")
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(data, 1):
        if not isinstance(item, dict):
            raise ValueError(f"case #{index} is not an object")
        rows.append(item)
    return rows


def build_loop_input(case: dict[str, Any], *, session_id: str, project_id: str) -> IntentAgentLoopInput:
    return IntentAgentLoopInput(
        user_text=str(case.get("user_text") or ""),
        workflow_id=str(case.get("workflow_id") or "pcb_escape_flow"),
        workflow_state=str(case.get("workflow_state") or "idle"),
        allowed_actions=tuple(str(item) for item in case.get("allowed_actions") or ("chat",)),
        explicit_fields=dict(case.get("explicit_fields") or {}),
        hints=dict(case.get("hints") or {}),
        fallback_candidates=_action_candidate_dicts(case.get("fallback_candidates")),
        explicit_action=str(case.get("explicit_action") or ""),
        experience_actions=_action_candidate_dicts(case.get("experience_actions")),
        session_id=session_id,
        project_id=project_id,
    )


def evaluate_case(
    case: dict[str, Any],
    *,
    controller: WebSocketWorkflowController,
    intent_model: ToolPlanningChatIntentModel,
    project_prefix: str,
) -> dict[str, Any]:
    session_id = str(case.get("session_id") or f"loop-eval-{case.get('name') or 'case'}")
    project_id = str(case.get("project_id") or f"{project_prefix}-{case.get('name') or 'case'}")
    loop_input = build_loop_input(case, session_id=session_id, project_id=project_id)
    loop_result = run_pcb_intent_agent_loops(loop_input, intent_model)
    plan = controller._workflow_action_plan_from_loop_result(loop_input, loop_result)

    return {
        "name": str(case.get("name") or ""),
        "loop_input": {
            "user_text": loop_input.user_text,
            "workflow_id": loop_input.workflow_id,
            "workflow_state": loop_input.workflow_state,
            "allowed_actions": list(loop_input.allowed_actions),
            "explicit_fields": loop_input.explicit_fields,
            "hints": loop_input.hints,
        },
        "candidate_set": {
            "workflow": loop_result.candidate_set.workflow,
            "current_state": loop_result.candidate_set.current_state,
            "candidate_actions": [
                {
                    "action": item.action,
                    "confidence": item.confidence,
                    "entities": item.entities,
                    "reason": item.reason,
                    "source": item.source,
                }
                for item in loop_result.candidate_set.candidate_actions
            ],
            "model_source": loop_result.candidate_set.model_source,
        },
        "loop_result": {
            "accepted": loop_result.accepted,
            "final_action": loop_result.final_action,
            "feedback_reply": loop_result.feedback_reply,
            "stage": loop_result.stage,
            "rejection_feedback": list(loop_result.rejection_feedback),
            "votes": list(loop_result.votes),
            "policy": {
                "action": loop_result.policy.action,
                "confidence": loop_result.policy.confidence,
                "reason": loop_result.policy.reason,
                "requires_confirmation": loop_result.policy.requires_confirmation,
                "accepted_candidates": [
                    {
                        "action": item.action,
                        "confidence": item.confidence,
                        "entities": item.entities,
                        "reason": item.reason,
                        "source": item.source,
                    }
                    for item in loop_result.policy.accepted_candidates
                ],
                "rejected_candidates": [
                    {
                        "action": item.action,
                        "confidence": item.confidence,
                        "entities": item.entities,
                        "reason": item.reason,
                        "source": item.source,
                    }
                    for item in loop_result.policy.rejected_candidates
                ],
            },
        },
        "workflow_action_plan": {
            "workflow_id": plan.workflow_id,
            "workflow_state": plan.workflow_state,
            "allowed_actions": list(plan.allowed_actions),
            "action": plan.action,
            "phase": plan.phase,
            "reason": plan.reason,
            "accepted": plan.accepted,
            "entities": plan.entities,
            "immediate_reply": plan.immediate_reply,
            "stage": plan.stage,
            "rejection_feedback": list(plan.rejection_feedback),
            "votes": list(plan.votes),
            "debug": plan.debug,
        },
    }


def print_human_result(result: dict[str, Any]) -> None:
    print(f"== {result['name']} ==")
    print(f"text: {result['loop_input']['user_text']}")
    print(
        f"context: workflow={result['loop_input']['workflow_id']} "
        f"state={result['loop_input']['workflow_state']} "
        f"allowed={','.join(result['loop_input']['allowed_actions'])}"
    )
    print("candidateActions:")
    for item in result["candidate_set"]["candidate_actions"]:
        print(
            "  - "
            f"{item['action']} "
            f"(confidence={item['confidence']:.2f}, source={item['source']}, entities={json.dumps(item['entities'], ensure_ascii=False)}) "
            f"reason={item['reason']}"
        )
    loop_result = result["loop_result"]
    print(
        "loop: "
        f"accepted={loop_result['accepted']} "
        f"final_action={loop_result['final_action'] or '-'} "
        f"stage={loop_result['stage'] or '-'} "
        f"votes={loop_result['votes']}"
    )
    if loop_result["feedback_reply"]:
        print(f"feedback: {loop_result['feedback_reply']}")
    plan = result["workflow_action_plan"]
    print(
        "plan: "
        f"action={plan['action']} "
        f"phase={plan['phase']} "
        f"accepted={plan['accepted']} "
        f"reason={plan['reason']}"
    )
    if plan["entities"]:
        print(f"entities: {json.dumps(plan['entities'], ensure_ascii=False)}")
    if plan["immediate_reply"]:
        print(f"immediate_reply: {plan['immediate_reply']}")
    print()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate SWSD intent loops against the real tool-planning-chat model.")
    parser.add_argument("--cases", type=Path, help="Optional JSON file with case objects.")
    parser.add_argument("--timeout-s", type=float, default=8.0, help="Per model call timeout in seconds.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a human summary.")
    parser.add_argument("--project-prefix", default="loop-eval", help="Prefix used for generated project ids.")
    parser.add_argument("--trace-dir", type=Path, default=HERE / "artifacts" / "swsd_intent_traces", help="Directory for raw real-model trace JSON files.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    cases = load_cases(args.cases)
    controller = _make_controller()
    intent_model = ToolPlanningChatIntentModel(timeout_s=args.timeout_s, trace_sink=file_trace_sink(args.trace_dir))
    results = [
        evaluate_case(
            case,
            controller=controller,
            intent_model=intent_model,
            project_prefix=args.project_prefix,
        )
        for case in cases
    ]
    if args.json:
        print(json.dumps({"results": results}, ensure_ascii=False, indent=2))
        return 0
    for result in results:
        print_human_result(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
