from agent.swsd.action_candidates import ActionCandidate, AgentAssistRequest, IntentCandidateSet
from agent.swsd.decision_policy import WorkflowContext, decide_workflow_action
from agent.swsd.response_builder import SWSDResponseBuilder


def test_intent_candidate_set_parses_action_candidates():
    parsed = IntentCandidateSet.from_mapping(
        {
            "workflow": "pcb_escape_flow",
            "currentState": "review",
            "candidateActions": [
                {
                    "action": "modify_params",
                    "confidence": 0.88,
                    "entities": {"LineWidth": 30},
                    "reason": "user changed line width",
                }
            ],
        }
    )

    assert parsed.workflow == "pcb_escape_flow"
    assert parsed.current_state == "review"
    assert parsed.candidate_actions[0].action == "modify_params"
    assert parsed.candidate_actions[0].entities == {"LineWidth": 30}


def test_decision_policy_accepts_only_legal_high_confidence_candidate():
    decision = decide_workflow_action(
        workflow_context=WorkflowContext(
            workflow_state="review",
            current_node="review",
            allowed_transitions=("modify_params", "confirm_import"),
        ),
        candidates=[
            ActionCandidate("reroute_again", 0.99, source="intent_model"),
            ActionCandidate("modify_params", 0.88, source="intent_model"),
        ],
    )

    assert decision.action == "modify_params"
    assert decision.reason == "candidate_accepted"
    assert decision.rejected_candidates[0].action == "reroute_again"


def test_decision_policy_requires_confirmation_for_low_confidence_candidate():
    decision = decide_workflow_action(
        workflow_context=WorkflowContext(
            workflow_state="review",
            current_node="review",
            allowed_transitions=("modify_params",),
        ),
        candidates=[ActionCandidate("modify_params", 0.54, source="agent_assist")],
    )

    assert decision.action == ""
    assert decision.requires_confirmation is True


def test_tool_result_action_has_priority_when_allowed():
    decision = decide_workflow_action(
        workflow_context=WorkflowContext(
            workflow_state="rip_up",
            current_node="rip_up",
            allowed_transitions=("complete_reroute",),
        ),
        candidates=[ActionCandidate("chat", 0.99, source="intent_model")],
        tool_result_action="complete_reroute",
    )

    assert decision.action == "complete_reroute"
    assert decision.reason == "tool_result_priority"


def test_agent_assist_request_forbids_side_effects_by_default():
    request = AgentAssistRequest(
        purpose="explanation",
        workflow_id="pcb_reroute_flow",
        workflow_state="report",
        facts={"rerouteResult": {"status": "local_completion_passed"}},
    )

    assert "call_tool" in request.forbidden_actions
    assert "change_workflow_state" in request.forbidden_actions
    assert "invent_structured_fields" in request.forbidden_actions


def test_response_builder_guarantees_reroute_terminal_fields():
    fields = SWSDResponseBuilder.reroute_final(
        {"rerouteResult": {"status": "local_completion_passed"}},
        visible_text="DRC 第 1 轮通过，已完成局部布线完善。",
    )

    assert fields["rerouteResult"]["status"] == "local_completion_passed"
    assert fields["checkReport"]["passed"] is True
    assert fields["explanation"]

