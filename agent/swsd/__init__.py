"""Structured Workflow State Decomposition (SWSD) framework."""

from agent.swsd.action_candidates import (
    ActionCandidate,
    AgentAssistRequest,
    AgentAssistResult,
    IntentCandidateSet,
)
from agent.swsd.graph import (
    ActionType,
    Checkpoint,
    StateDef,
    Transition,
    WorkflowDef,
    WorkflowEvent,
    WorkflowSessionState,
)
from agent.swsd.registry import get_workflow, list_workflows
from agent.swsd.response_builder import SWSDResponseBuilder
from agent.swsd.runtime_bridge import WebSocketSWSDRuntimeBridge
from agent.swsd.state_manager import WorkflowStateManager
from agent.swsd.workflow_controller import SWSDTurnDecision, SWSDTurnEvent, WebSocketWorkflowController, WorkflowActionPlan

__all__ = [
    "ActionCandidate",
    "AgentAssistRequest",
    "AgentAssistResult",
    "IntentCandidateSet",
    "ActionType",
    "Checkpoint",
    "StateDef",
    "Transition",
    "WorkflowDef",
    "WorkflowEvent",
    "WorkflowSessionState",
    "WorkflowStateManager",
    "SWSDResponseBuilder",
    "WebSocketSWSDRuntimeBridge",
    "SWSDTurnDecision",
    "WorkflowActionPlan",
    "SWSDTurnEvent",
    "WebSocketWorkflowController",
    "get_workflow",
    "list_workflows",
]
