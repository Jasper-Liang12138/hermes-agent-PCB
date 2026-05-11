---
name: pcb-reroute
version: 1.2.0
description: PCB local rip-up and reroute flow driven by frontend-selected trace ids, deletion, refreshed project data, and DRC validation
prerequisites:
  commands: []
  python_packages: []
metadata:
  hermes:
    tags: [PCB, reroute, rip-up, local-reroute, DRC, EDA]
    category: hardware
---

# PCB Reroute Skill - Local Rip-Up And Reroute

## Goal

Use this skill when the user asks to rip up selected PCB traces and reroute them locally. The deletion target is never extracted from free-form user text. The selected trace ids must come from the PCB frontend selection.

## Trigger

Trigger only when the user clearly asks for local rip-up/reroute, deletion followed by reroute, reroute selected traces, or similar PCB routing work.

Do not call tools for conceptual questions, explanations, or requests that explicitly ask not to modify the board.

## Tool Chain

| Tool | Purpose |
|------|---------|
| `drop_net` | Compatibility entrypoint for the local rip-up stage. Internally calls `getSelectedElements`, `deleteTracesById`, then `getProjectData`. |
| `reroute` | Reads the cached selected trace ids and post-delete board data, then generates local reroute output, KiCad patch, check report, and DRC result. |

## Required Flow

1. Confirm the request is a local rip-up/reroute request.
2. Call `drop_net(userText, projectID)`.
   - `userText` is the original user request.
   - `projectID` comes from the incoming message `projectid` field if available.
   - `drop_net` must obtain selected trace ids from `getSelectedElements` with `PFindType="TRACES"`.
   - `drop_net` must reject an empty selection.
   - `drop_net` must reject selections with more than 40 ids and end the skill.
   - `drop_net` must call `deleteTracesById` only when the selected id count is between 1 and 40.
   - `drop_net` must call `getProjectData` after successful deletion to refresh board data.
3. If `drop_net` returns `error`, report that error to the user and stop. Do not call `reroute`.
4. Call `reroute()` only after `drop_net` succeeds.
5. Return final structured fields inside `##PCB_FIELDS##`.

## Output Format

```text
Local rip-up and reroute finished.
##PCB_FIELDS##
{
  "rerouteResult": {
    "type": "local_reroute",
    "mode": "selected_traces_after_delete",
    "selectedTraceIds": ["2386476278", "3424247826"],
    "operations": [],
    "drcPassed": true,
    "drcIterations": 1,
    "routedBoardDataFilePath": "/path/to/.hermes_reroute/session_iter1.kicad_pcb"
  },
  "routedBoardDataFilePath": "/path/to/.hermes_reroute/session_iter1.kicad_pcb",
  "checkReport": {
    "passed": true,
    "checks": []
  },
  "explanation": "Local reroute result generated and checked."
}
##PCB_FIELDS_END##
```

## Constraints

- Do not infer deletion targets from net names in user text.
- Do not call the global BGA fanout `route` tool.
- Do not call `reroute` if selected trace id count is 0 or greater than 40.
- Do not call `reroute` if `deleteTracesById` fails.
- `##PCB_FIELDS##` content must be valid JSON.
