from agent.swsd.action_candidates import ActionCandidate, IntentCandidateSet
from agent.swsd.pcb_intent_agent_loop import (
    IntentAgentLoopInput,
    ToolPlanningChatIntentModel,
    agent_arbit_loop,
    agent_confidence_loop,
    agent_feedback_loop,
    agent_proposal_loop,
    run_pcb_intent_agent_loops,
)
from tools import pcb_model_runtime


def _request(*, allowed=("rollback_checkpoint", "chat"), fallback=()):
    return IntentAgentLoopInput(
        user_text="回到上一步",
        workflow_id="pcb_escape_flow",
        workflow_state="review",
        allowed_actions=tuple(allowed),
        fallback_candidates=tuple(fallback),
    )


class RecordingIntentModel:
    def __init__(self):
        self.proposal_calls = 0
        self.judge_calls = 0
        self.revise_calls = 0
        self.feedback_calls = 0

    def propose_candidates(self, request, feedback=()):
        self.proposal_calls += 1
        if self.proposal_calls == 1:
            return IntentCandidateSet(request.workflow_id, request.workflow_state, ())
        return IntentCandidateSet(
            request.workflow_id,
            request.workflow_state,
            (ActionCandidate("rollback_checkpoint", 0.96, reason="previous step", source="intent_model"),),
            "intent_model",
        )

    def judge_candidates(self, request, candidate_set, policy_feedback=""):
        self.judge_calls += 1
        return True

    def revise_candidates(self, request, candidate_set, rejection_feedback):
        self.revise_calls += 1
        return IntentCandidateSet(
            request.workflow_id,
            request.workflow_state,
            (ActionCandidate("chat", 0.86, reason="repair to allowed action", source="intent_model"),),
            "intent_model",
        )

    def build_feedback_reply(self, request, candidate_set, rejection_feedback):
        self.feedback_calls += 1
        return "请补充你想回到哪一步。"


def test_proposal_loop_retries_until_candidate_set_is_valid():
    model = RecordingIntentModel()

    candidate_set, errors = agent_proposal_loop(_request(), model)

    assert not errors
    assert model.proposal_calls == 2
    assert candidate_set.candidate_actions[0].action == "rollback_checkpoint"


def test_confidence_loop_runs_six_checks_and_accepts_five_vote_majority():
    model = RecordingIntentModel()
    request = _request()
    candidate_set = IntentCandidateSet(
        request.workflow_id,
        request.workflow_state,
        (ActionCandidate("rollback_checkpoint", 0.96, source="intent_model"),),
    )

    policy, accepted, votes, feedback = agent_confidence_loop(request, candidate_set, model)

    assert accepted is True
    assert policy.action == "rollback_checkpoint"
    assert len(votes) == 6
    assert sum(votes) == 6
    assert feedback == ()


def test_arbit_loop_revises_entire_candidate_set_until_policy_accepts():
    model = RecordingIntentModel()
    request = _request(allowed=("chat",))
    rejected = IntentCandidateSet(
        request.workflow_id,
        request.workflow_state,
        (ActionCandidate("rollback_checkpoint", 0.96, source="intent_model"),),
    )

    revised, policy, accepted, feedback = agent_arbit_loop(request, rejected, model, ("action not allowed",))

    assert accepted is True
    assert revised.candidate_actions[0].action == "chat"
    assert policy.action == "chat"
    assert feedback == ("action not allowed",)


def test_feedback_loop_returns_readable_reply():
    model = RecordingIntentModel()
    request = _request()

    reply = agent_feedback_loop(request, IntentCandidateSet(request.workflow_id, request.workflow_state, ()), model, ("rejected",))

    assert "补充" in reply
    assert model.feedback_calls == 1


def test_orchestrator_returns_feedback_when_arbitration_cannot_accept():
    class RejectingModel(RecordingIntentModel):
        def propose_candidates(self, request, feedback=()):
            return IntentCandidateSet(
                request.workflow_id,
                request.workflow_state,
                (ActionCandidate("not_allowed", 0.99, source="intent_model"),),
            )

        def revise_candidates(self, request, candidate_set, rejection_feedback):
            return candidate_set

    request = _request(allowed=("rollback_checkpoint",))
    result = run_pcb_intent_agent_loops(request, RejectingModel())

    assert result.accepted is False
    assert result.stage == "feedback"
    assert result.feedback_reply


def test_tool_planning_chat_intent_model_uses_configured_stage(monkeypatch):
    calls = []

    def fake_chat_completion_text(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return (
                '{"workflow":"pcb_escape_flow","currentState":"review","candidateActions":[{"action":"rollback_checkpoint","confidence":0.96,"entities":{},"reason":"previous step"}],"modelSource":"tool_planning_chat"}',
                {"stage": kwargs["stage"]},
            )
        return '{"accept":true,"reason":"valid"}', {"stage": kwargs["stage"]}

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)

    result = run_pcb_intent_agent_loops(_request(), ToolPlanningChatIntentModel(timeout_s=1.0))

    assert result.accepted is True
    assert result.final_action == "rollback_checkpoint"
    assert calls
    assert all(call["stage"] == pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT for call in calls)
