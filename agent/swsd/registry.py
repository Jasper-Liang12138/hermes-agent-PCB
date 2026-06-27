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
            StateDef("layer_assign_escape_order", "Assign escape layers and generate escape order.", recommended_tools=("generate_fanout_params",)),
            StateDef("param_review", "Review generated fanout parameters before routing."),
            StateDef("routing", "Run router.", recommended_tools=("route",)),
            StateDef("review", "Review routing result."),
            StateDef("import", "Import routed lines.", recommended_tools=("importLines",)),
        ]
    ),
    transitions=(
        Transition("select_bga", "layer_assign_escape_order", "select_target"),
        Transition("layer_assign_escape_order", "param_review", "fanout_params_generated"),
        Transition("param_review", "routing", "confirm_route"),
        Transition("routing", "review", "route_complete"),
        Transition("review", "import", "confirm_import"),
        Transition("import", "idle", "complete"),
        Transition("routing", "layer_assign_escape_order", "route_failed", ActionType.FALLBACK),
        Transition("param_review", "layer_assign_escape_order", "modify_params", ActionType.USER_JUMP),
        Transition("review", "layer_assign_escape_order", "modify_params", ActionType.USER_JUMP),
        Transition("import", "layer_assign_escape_order", "modify_params", ActionType.USER_JUMP),
        Transition("layer_assign_escape_order", "layer_assign_escape_order", "modify_constraints", ActionType.USER_JUMP),
        Transition("param_review", "layer_assign_escape_order", "modify_constraints", ActionType.USER_JUMP),
        Transition("review", "layer_assign_escape_order", "modify_constraints", ActionType.USER_JUMP),
        Transition("import", "layer_assign_escape_order", "modify_constraints", ActionType.USER_JUMP),
        Transition("layer_assign_escape_order", "layer_assign_escape_order", "modify_router_choice", ActionType.USER_JUMP),
        Transition("param_review", "layer_assign_escape_order", "modify_router_choice", ActionType.USER_JUMP),
        Transition("review", "layer_assign_escape_order", "modify_router_choice", ActionType.USER_JUMP),
        Transition("import", "layer_assign_escape_order", "modify_router_choice", ActionType.USER_JUMP),
        Transition("layer_assign_escape_order", "layer_assign_escape_order", "modify_order_lines", ActionType.USER_JUMP),
        Transition("param_review", "layer_assign_escape_order", "modify_order_lines", ActionType.USER_JUMP),
        Transition("review", "layer_assign_escape_order", "modify_order_lines", ActionType.USER_JUMP),
        Transition("import", "layer_assign_escape_order", "modify_order_lines", ActionType.USER_JUMP),
        Transition("layer_assign_escape_order", "layer_assign_escape_order", "restore_params_version", ActionType.USER_JUMP),
        Transition("param_review", "layer_assign_escape_order", "restore_params_version", ActionType.USER_JUMP),
        Transition("routing", "layer_assign_escape_order", "restore_params_version", ActionType.USER_JUMP),
        Transition("review", "layer_assign_escape_order", "restore_params_version", ActionType.USER_JUMP),
        Transition("import", "layer_assign_escape_order", "restore_params_version", ActionType.USER_JUMP),
        Transition("layer_assign_escape_order", "review", "restore_layout_checkpoint", ActionType.USER_JUMP),
        Transition("param_review", "review", "restore_layout_checkpoint", ActionType.USER_JUMP),
        Transition("routing", "review", "restore_layout_checkpoint", ActionType.USER_JUMP),
        Transition("review", "review", "restore_layout_checkpoint", ActionType.USER_JUMP),
        Transition("import", "review", "restore_layout_checkpoint", ActionType.USER_JUMP),
        Transition("layer_assign_escape_order", "select_bga", "change_target", ActionType.USER_JUMP),
        Transition("param_review", "select_bga", "change_target", ActionType.USER_JUMP),
        Transition("routing", "select_bga", "change_target", ActionType.USER_JUMP),
        Transition("review", "select_bga", "change_target", ActionType.USER_JUMP),
        Transition("import", "select_bga", "change_target", ActionType.USER_JUMP),
        Transition("review", "routing", "route_again", ActionType.USER_JUMP),
        Transition("import", "routing", "route_again", ActionType.USER_JUMP),
        Transition("layer_assign_escape_order", "layer_assign_escape_order", "rerun_fanout", ActionType.USER_JUMP),
        Transition("param_review", "layer_assign_escape_order", "rerun_fanout", ActionType.USER_JUMP),
        Transition("routing", "layer_assign_escape_order", "rerun_fanout", ActionType.USER_JUMP),
        Transition("review", "layer_assign_escape_order", "rerun_fanout", ActionType.USER_JUMP),
        Transition("import", "layer_assign_escape_order", "rerun_fanout", ActionType.USER_JUMP),
        Transition("review", "review", "reject_route", ActionType.USER_JUMP),
        Transition("import", "import", "reject_import", ActionType.USER_JUMP),
        Transition("review", "import", "confirm_import", ActionType.USER_JUMP),
        Transition("import", "idle", "cancel_flow", ActionType.CANCEL),
        Transition("review", "idle", "cancel_flow", ActionType.CANCEL),
        Transition("param_review", "idle", "cancel_flow", ActionType.CANCEL),
        Transition("routing", "idle", "cancel_flow", ActionType.CANCEL),
        Transition("layer_assign_escape_order", "idle", "cancel_flow", ActionType.CANCEL),
        Transition("select_bga", "idle", "cancel_flow", ActionType.CANCEL),
    ),
)
PCB_REROUTE_FLOW = WorkflowDef(
    workflow_id="pcb_reroute_flow",
    description="PCB selected-trace rip-up and reroute workflow.",
    initial_state="idle",
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
        Transition("idle", "rip_up", "reroute_entry"),
        Transition("rip_up", "confirm", "ripup_complete"),
        Transition("confirm", "reroute_llm", "confirm_reroute"),
        Transition("reroute_llm", "drc_loop", "model_generated"),
        Transition("drc_loop", "reroute_llm", "drc_failed", ActionType.FALLBACK),
        Transition("drc_loop", "report", "drc_passed"),
        Transition("report", "import", "confirm_import"),
        Transition("import", "idle", "complete"),
        Transition("report", "rip_up", "reroute_again", ActionType.USER_JUMP),
        Transition("import", "rip_up", "reroute_again", ActionType.USER_JUMP),
        Transition("drc_loop", "rip_up", "reroute_again", ActionType.USER_JUMP),
        Transition("confirm", "rip_up", "reroute_again", ActionType.USER_JUMP),
        Transition("report", "report", "restore_layout_checkpoint", ActionType.USER_JUMP),
        Transition("import", "report", "restore_layout_checkpoint", ActionType.USER_JUMP),
        Transition("drc_loop", "report", "restore_layout_checkpoint", ActionType.USER_JUMP),
        Transition("report", "report", "rollback_checkpoint", ActionType.ROLLBACK),
        Transition("import", "report", "rollback_checkpoint", ActionType.ROLLBACK),
        Transition("drc_loop", "drc_loop", "reject_import", ActionType.USER_JUMP),
        Transition("report", "report", "reject_import", ActionType.USER_JUMP),
        Transition("report", "import", "confirm_import", ActionType.USER_JUMP),
        Transition("report", "idle", "cancel_flow", ActionType.CANCEL),
        Transition("import", "idle", "cancel_flow", ActionType.CANCEL),
        Transition("drc_loop", "idle", "cancel_flow", ActionType.CANCEL),
        Transition("confirm", "idle", "cancel_flow", ActionType.CANCEL),
        Transition("rip_up", "idle", "cancel_flow", ActionType.CANCEL),
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


