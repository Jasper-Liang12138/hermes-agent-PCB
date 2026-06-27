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

    def vote_action(self, request, candidate_set, evidence_set, *, model_stage="tool_planning_chat", negative_feedback=()):
        self.judge_calls += 1
        return {"selected_action": evidence_set.top_candidates[0].action, "confidence": 0.9, "reason": "test vote"}

    def refine_evidence(self, request, candidate_set, evidence_set, rejection_feedback):
        self.revise_calls += 1
        return evidence_set

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


def test_confidence_loop_runs_six_action_votes_and_accepts_four_vote_majority():
    model = RecordingIntentModel()
    request = _request()
    candidate_set = IntentCandidateSet(
        request.workflow_id,
        request.workflow_state,
        (ActionCandidate("rollback_checkpoint", 0.96, source="intent_model"),),
    )

    policy, accepted, votes, feedback, debug = agent_confidence_loop(request, candidate_set, model)

    assert accepted is True
    assert policy.action == "rollback_checkpoint"
    assert len(votes) == 6
    assert votes == ("rollback_checkpoint",) * 6
    assert feedback == ()


def test_arbit_loop_refines_evidence_and_revotes_until_majority():
    class RefiningModel(RecordingIntentModel):
        def vote_action(self, request, candidate_set, evidence_set, *, model_stage="tool_planning_chat", negative_feedback=()):
            self.judge_calls += 1
            action = "chat" if self.revise_calls == 0 else "rollback_checkpoint"
            return {"selected_action": action, "confidence": 0.9, "reason": "test vote"}

    model = RefiningModel()
    request = _request(allowed=("rollback_checkpoint", "chat"))
    candidate_set = IntentCandidateSet(
        request.workflow_id,
        request.workflow_state,
        (
            ActionCandidate("rollback_checkpoint", 0.96, source="intent_model"),
            ActionCandidate("chat", 0.8, source="intent_model"),
        ),
    )

    revised, policy, accepted, feedback, votes, debug = agent_arbit_loop(request, candidate_set, model, ("expert_b_no_majority",))

    assert accepted is True
    assert revised is candidate_set
    assert policy.action == "rollback_checkpoint"
    assert votes == ("rollback_checkpoint",) * 6
    assert len(debug["arbit_rounds"]) == 1


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
        return '{"selected_action":"rollback_checkpoint","confidence":0.91,"reason":"valid"}', {"stage": kwargs["stage"]}

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)

    result = run_pcb_intent_agent_loops(_request(), ToolPlanningChatIntentModel(timeout_s=1.0))

    assert result.accepted is True
    assert result.final_action == "rollback_checkpoint"
    assert calls
    assert calls[0]["stage"] == pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT
    assert {call["stage"] for call in calls[1:]} == {pcb_model_runtime.STAGE_TOOL_PLANNING_CHAT, pcb_model_runtime.STAGE_REROUTE}


def test_local_rule_fallback_recognizes_rollback_without_fallback_candidates():
    request = _request(fallback=())
    result = run_pcb_intent_agent_loops(request, None)

    assert result.accepted is True
    assert result.final_action == "rollback_checkpoint"
    assert result.candidate_set.candidate_actions[0].source == "local_rule_intent_model"


def test_local_rule_fallback_recognizes_reroute_entry():
    request = IntentAgentLoopInput(
        user_text="拆线重布",
        workflow_id="pcb_reroute_flow",
        workflow_state="idle",
        allowed_actions=("reroute_entry", "chat"),
    )

    result = run_pcb_intent_agent_loops(request, None)

    assert result.accepted is True
    assert result.final_action == "reroute_entry"


def test_tool_planning_chat_intent_model_parses_relaxed_candidate_shapes(monkeypatch):
    calls = []

    def fake_chat_completion_text(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return (
                '```json\n{"actionCandidates":[{"intent":"rollback_checkpoint","confidence":"0.96","entities":"{\\"target_step\\":\\"previous\\"}","why":"previous"}],"modelSource":"tool_planning_chat"}\n```',
                {"stage": kwargs["stage"]},
            )
        return '{"selected_action":"rollback_checkpoint","confidence":0.96,"reason":"valid"}', {"stage": kwargs["stage"]}

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)

    result = run_pcb_intent_agent_loops(_request(), ToolPlanningChatIntentModel(timeout_s=1.0))

    assert result.accepted is True
    assert result.final_action == "rollback_checkpoint"
    assert result.candidate_set.candidate_actions[0].entities == {"target_step": "previous"}

def test_tool_planning_chat_intent_model_normalizes_entity_only_router_output(monkeypatch):
    calls = []

    def fake_chat_completion_text(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return '{"routerType":"135+RL"}', {"stage": kwargs["stage"]}
        return '{"selected_action":"layer_assigned","confidence":0.91,"reason":"valid router choice"}', {"stage": kwargs["stage"]}

    monkeypatch.setattr(pcb_model_runtime, "chat_completion_text", fake_chat_completion_text)
    request = IntentAgentLoopInput(
        user_text="135 + RL",
        workflow_id="pcb_escape_flow",
        workflow_state="layer_assign_escape_order",
        allowed_actions=("layer_assigned", "modify_params", "chat"),
    )

    result = run_pcb_intent_agent_loops(request, ToolPlanningChatIntentModel(timeout_s=1.0))

    assert result.accepted is True
    assert result.final_action == "layer_assigned"
    assert result.candidate_set.candidate_actions[0].entities == {"routerType": "135+RL"}


def test_confidence_loop_requires_four_of_six_action_votes():
    class SplitVoteModel(RecordingIntentModel):
        def __init__(self):
            super().__init__()
            self.actions = ["rollback_checkpoint", "chat", "rollback_checkpoint", "rollback_checkpoint", "chat", "rollback_checkpoint"]

        def vote_action(self, request, candidate_set, evidence_set, *, model_stage="tool_planning_chat", negative_feedback=()):
            self.judge_calls += 1
            return {"selected_action": self.actions[self.judge_calls - 1], "confidence": 0.9, "reason": "split vote"}

    model = SplitVoteModel()
    request = _request(allowed=("rollback_checkpoint", "chat"))
    candidate_set = IntentCandidateSet(
        request.workflow_id,
        request.workflow_state,
        (
            ActionCandidate("rollback_checkpoint", 0.96, source="intent_model"),
            ActionCandidate("chat", 0.8, source="intent_model"),
        ),
    )

    policy, accepted, votes, feedback, debug = agent_confidence_loop(request, candidate_set, model)

    assert accepted is True
    assert policy.action == "rollback_checkpoint"
    assert len(votes) == 6
    assert votes.count("rollback_checkpoint") == 4
    assert model.judge_calls == 6
    assert debug["vote_counts"]["rollback_checkpoint"] == 4


def test_review_rerun_fanout_beats_generic_pcb_entry():
    request = IntentAgentLoopInput(
        user_text="\u91cd\u65b0fanout，\u8981\u6539\u7ebf\u5bbd\u4e3a3mil",
        workflow_id="pcb_escape_flow",
        workflow_state="review",
        allowed_actions=("rerun_fanout", "modify_params", "chat"),
        fallback_candidates=(
            ActionCandidate("pcb_entry", 0.94, reason="generic fanout", source="intent_model"),
            ActionCandidate("rerun_fanout", 0.9, reason="rerun fanout", source="intent_model"),
            ActionCandidate("modify_params", 0.88, reason="constraint change", source="intent_model"),
        ),
    )

    result = run_pcb_intent_agent_loops(request, None)

    assert result.accepted is True
    assert result.final_action in {"rerun_fanout", "modify_params"}
    assert result.final_action != "pcb_entry"
