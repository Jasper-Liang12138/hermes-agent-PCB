"""Live evaluation for SWSD jump intent loop.

This script intentionally calls the real [tool-planning-chat-model]. It is not
part of the default pytest suite because it depends on network/API credentials
and model stability.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.swsd.jump_intent_loop import JumpIntentLoopInput, run_jump_intent_loop
from agent.swsd.registry import list_workflows
from tools import pcb_model_runtime


def _compact_raw_outputs(raw_outputs):
    compact = []
    for item in raw_outputs:
        row = dict(item)
        raw = str(row.get("raw_output") or "")
        if len(raw) > 500:
            row["raw_output_head"] = raw[:500]
            row["raw_output_truncated"] = True
            row.pop("raw_output", None)
        compact.append(row)
    return compact


def _state_graph(active_workflow: str, active_state: str) -> dict:
    jump_action_types = {"user_jump", "rollback", "cancel"}
    return {
        "active": {"workflow_id": active_workflow, "state": active_state},
        "workflows": {
            workflow_id: {
                "states": list(workflow.states.keys()),
                "jump_transitions": [
                    {
                        "from": transition.from_state,
                        "to": transition.to_state,
                        "intent": transition.intent,
                        "action_type": transition.action_type.value,
                    }
                    for transition in workflow.transitions
                    if transition.action_type.value in jump_action_types
                ],
            }
            for workflow_id, workflow in list_workflows().items()
        },
    }


def _preflight() -> dict:
    runtime = pcb_model_runtime.resolve_model_runtime(pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT)
    public_runtime = {"model": runtime.get("model"), "base_url": runtime.get("base_url"), "api_key": "***" if runtime.get("api_key") else ""}
    try:
        content, meta = pcb_model_runtime.chat_completion_text(
            stage=pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT,
            messages=[
                {"role": "system", "content": "Return only JSON. No Markdown. No explanation."},
                {"role": "user", "content": "请只返回 {\"ok\": true}"},
            ],
            timeout_s=30,
            max_tokens=64,
            temperature=0,
            top_p=1,
            stream_until_json=True,
        )
        return {"ok": True, "runtime": public_runtime, "content": content, "meta": {k: meta.get(k) for k in ("model", "base_url", "stream_finish_reason", "response_id")}}
    except Exception as exc:
        return {"ok": False, "runtime": public_runtime, "error": str(exc)}


def main() -> int:
    preflight = _preflight()
    if not preflight["ok"]:
        print(json.dumps({"runtime_config_error": preflight}, ensure_ascii=False, indent=2, default=str))
        return 0
    cases = [
        ("pcb_escape_flow", "review", "重新fanout，要改线宽为3mil", "rerun_fanout", {"constraints": {"LineWidth": 3}}),
        ("pcb_escape_flow", "review", "重新选择 U7 再 fanout", "change_target", {"selectedBGA": "U7"}),
        ("pcb_escape_flow", "review", "拆线重布", "reroute_entry", {}),
        ("pcb_reroute_flow", "report", "给 U5 做 fanout", "pcb_entry", {"selectedBGA": "U5"}),
    ]
    results = []
    for workflow_id, state, text, candidate_action, entities in cases:
        result = run_jump_intent_loop(
            JumpIntentLoopInput(
                user_text=text,
                workflow_id=workflow_id,
                workflow_state=state,
                state_graph=_state_graph(workflow_id, state),
                candidate_action=candidate_action,
                entities=entities,
            )
        )
        results.append(
            {
                "input": text,
                "workflow": workflow_id,
                "state": state,
                "candidate_action": candidate_action,
                "accepted": result.accepted,
                "reason": result.reason,
                "clarification": result.clarification,
                "prior": result.prior.path if result.prior else "",
                "top5_scores": list(result.prior.debug_scores) if result.prior else [],
                "valid_votes": [vote.as_dict() for vote in result.valid_votes],
                "selected": result.plan.as_dict() if result.plan else None,
                "invalid_rounds": result.invalid_rounds,
                "invalid_feedback": list(result.invalid_feedback),
                "raw_outputs": _compact_raw_outputs(result.raw_outputs),
            }
        )
    print(json.dumps({"preflight": preflight, "cases": results}, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
