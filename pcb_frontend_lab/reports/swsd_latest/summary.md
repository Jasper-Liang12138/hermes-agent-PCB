# SWSD PCB Frontend Lab Comparison

Generated: 2026-06-11 21:56:23

## Pass/Fail

| Case | Baseline | SWSD | Baseline tools | SWSD tools |
| --- | --- | --- | --- | --- |
| concept_chat | PASS | PASS | - | - |
| fanout_selection | PASS | PASS | getProjectData | getProjectData |
| fanout_route_from_frontend_params | PASS | PASS | getProjectData, importLines | getProjectData, importLines |
| reroute_frontend_error | PASS | PASS | - | - |
| fanout_then_reroute_switch | PASS | PASS | getProjectData | getProjectData |

## SWSD Persistence

- workflow_sessions: 10
- workflow_events: 63
- workflow_checkpoints: 15
- observed states: `{"pcb_escape_flow": {"idle": 29, "import": 1, "review": 2, "routing": 1, "select_bga": 15}, "pcb_reroute_flow": {"idle": 9, "report": 4, "rip_up": 2}}`

## Protocol Diffs

- No tool/body/error diffs between baseline and SWSD.

## Board Text Leak Check

- baseline agent outbound transcript leak: False
- SWSD agent outbound transcript leak: False
- SWSD DB payload leak: False
