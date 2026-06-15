"""Built-in SWSD workflow registry."""

from __future__ import annotations

from agent.swsd.graph import ActionType, StateDef, Transition, WorkflowDef, states


PCB_ESCAPE_FLOW = WorkflowDef(
    workflow_id="pcb_escape_flow",
    description="PCB BGA escape/fanout routing workflow.",
    initial_state="select_bga",
    terminal_states=("idle", "import"),
    states=states(
        [
            StateDef("idle", "No active PCB workflow."),
            StateDef("select_bga", "Select target BGA.", recommended_tools=("getProjectData", "pcb_extract_bga")),
            StateDef("layer_assign", "Assign escape layers.", recommended_tools=("generate_fanout_params",)),
            StateDef("escape_order", "Generate escape order.", recommended_tools=("generate_fanout_params",)),
            StateDef("routing", "Run router.", recommended_tools=("route",)),
            StateDef("review", "Review routing result."),
            StateDef("import", "Import routed lines.", recommended_tools=("importLines",)),
        ]
    ),
    transitions=(
        Transition("select_bga", "layer_assign", "select_target"),
        Transition("layer_assign", "escape_order", "layer_assigned"),
        Transition("escape_order", "routing", "confirm_route"),
        Transition("routing", "review", "route_complete"),
        Transition("review", "import", "confirm_import"),
        Transition("import", "idle", "complete"),
        Transition("routing", "escape_order", "route_failed", ActionType.FALLBACK),
        Transition("review", "layer_assign", "modify_params", ActionType.USER_JUMP),
        Transition("escape_order", "select_bga", "change_target", ActionType.USER_JUMP),
        Transition("layer_assign", "select_bga", "change_target", ActionType.USER_JUMP),
    ),
)

PCB_REROUTE_FLOW = WorkflowDef(
    workflow_id="pcb_reroute_flow",
    description="PCB selected-trace rip-up and reroute workflow.",
    initial_state="rip_up",
    terminal_states=("idle", "import"),
    states=states(
        [
            StateDef("idle", "No active PCB workflow."),
            StateDef("rip_up", "Rip up selected traces.", recommended_tools=("deleteTracesForRerouting", "drop_net")),
            StateDef("confirm", "Confirm reroute context."),
            StateDef("reroute_llm", "Generate reroute patch with reroute model.", recommended_tools=("reroute",)),
            StateDef("drc_loop", "Run DRC and iterate reroute model.", recommended_tools=("reroute",)),
            StateDef("report", "Report reroute result."),
            StateDef("import", "Import reroute result.", recommended_tools=("importLines",)),
        ]
    ),
    transitions=(
        Transition("rip_up", "confirm", "ripup_complete"),
        Transition("confirm", "reroute_llm", "confirm_reroute"),
        Transition("reroute_llm", "drc_loop", "model_generated"),
        Transition("drc_loop", "reroute_llm", "drc_failed", ActionType.FALLBACK),
        Transition("drc_loop", "report", "drc_passed"),
        Transition("report", "import", "confirm_import"),
        Transition("import", "idle", "complete"),
    ),
)

_WORKFLOWS = {
    PCB_ESCAPE_FLOW.workflow_id: PCB_ESCAPE_FLOW,
    PCB_REROUTE_FLOW.workflow_id: PCB_REROUTE_FLOW,
}

for _workflow in _WORKFLOWS.values():
    _workflow.validate()


def get_workflow(workflow_id: str) -> WorkflowDef | None:
    return _WORKFLOWS.get(workflow_id)


def list_workflows() -> dict[str, WorkflowDef]:
    return dict(_WORKFLOWS)
