import json

from agent.swsd.action_candidates import ActionCandidate, IntentCandidateSet
from agent.swsd.decision_policy import SWSDDecision
from agent.swsd.pcb_intent_agent_loop import IntentAgentLoopResult
from agent.swsd.workflow_controller import WorkflowActionPlan
from tests.evaluate_pcb_intent_agent_loops_impl import (
    build_loop_input,
    evaluate_case,
    load_cases,
)


def test_build_loop_input_preserves_case_contract():
    loop_input = build_loop_input(
        {
            "user_text": "拆线重布",
            "workflow_id": "pcb_reroute_flow",
            "workflow_state": "idle",
            "allowed_actions": ["reroute_entry", "chat"],
            "explicit_fields": {"body": {"role": "user", "content": "拆线重布"}},
            "fallback_candidates": [
                {
                    "action": "reroute_entry",
                    "confidence": 0.96,
                    "entities": {"selection": "traces"},
                    "reason": "explicit reroute request",
                }
            ],
        },
        session_id="sess-1",
        project_id="proj-1",
    )

    assert loop_input.user_text == "拆线重布"
    assert loop_input.workflow_id == "pcb_reroute_flow"
    assert loop_input.allowed_actions == ("reroute_entry", "chat")
    assert loop_input.fallback_candidates[0].action == "reroute_entry"
    assert loop_input.fallback_candidates[0].entities == {"selection": "traces"}


def test_evaluate_case_returns_loop_result_and_plan(monkeypatch):
    class DummyController:
        def _workflow_action_plan_from_loop_result(self, loop_input, loop_result):
            return WorkflowActionPlan(
                workflow_id=loop_input.workflow_id,
                workflow_state=loop_input.workflow_state,
                allowed_actions=loop_input.allowed_actions,
                action="rollback_checkpoint",
                phase="jump",
                reason="confidence",
                accepted=True,
                entities={"target_step": "previous"},
                candidate_set=loop_result.candidate_set,
                stage=loop_result.stage,
                votes=loop_result.votes,
                debug={"policy_action": loop_result.policy.action},
            )

    class DummyIntentModel:
        pass

    def fake_run(loop_input, intent_model):
        candidate = ActionCandidate("rollback_checkpoint", 0.95, {"target_step": "previous"}, "rollback", "intent_model")
        return IntentAgentLoopResult(
            candidate_set=IntentCandidateSet("pcb_escape_flow", "review", (candidate,), "intent_model"),
            policy=SWSDDecision(
                action="rollback_checkpoint",
                confidence=0.95,
                accepted_candidates=(candidate,),
                reason="candidate_accepted",
            ),
            accepted=True,
            final_action="rollback_checkpoint",
            stage="confidence",
            votes=(True, True, True, True, True, True),
        )

    monkeypatch.setattr(
        "tests.evaluate_pcb_intent_agent_loops_impl.run_pcb_intent_agent_loops",
        fake_run,
    )

    result = evaluate_case(
        {
            "name": "rollback_review",
            "user_text": "回到上一步",
            "workflow_id": "pcb_escape_flow",
            "workflow_state": "review",
            "allowed_actions": ["rollback_checkpoint", "chat"],
        },
        controller=DummyController(),
        intent_model=DummyIntentModel(),
        project_prefix="eval",
    )

    assert result["candidate_set"]["candidate_actions"][0]["action"] == "rollback_checkpoint"
    assert result["loop_result"]["accepted"] is True
    assert result["workflow_action_plan"]["phase"] == "jump"


def test_load_cases_accepts_wrapped_object(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text(
        json.dumps(
            {
                "cases": [
                    {
                        "name": "chat",
                        "user_text": "这个布线为什么这样走？",
                        "workflow_id": "pcb_escape_flow",
                        "workflow_state": "review",
                        "allowed_actions": ["chat"],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    rows = load_cases(path)

    assert len(rows) == 1
    assert rows[0]["name"] == "chat"
