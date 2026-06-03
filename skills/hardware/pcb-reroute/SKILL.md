---
name: pcb-reroute
version: 1.3.0
description: PCB local selected-trace rip-up and reroute flow using frontend selection, deletion, refreshed project data, model generation, and DRC validation
prerequisites:
  commands: []
  python_packages: []
metadata:
  hermes:
    tags: [PCB, reroute, rip-up, selected-traces, local-reroute, DRC, EDA]
    category: hardware
---

# PCB Reroute Skill - Selected Trace Rip-Up And Local Reroute

## Goal

Use this skill when the user asks to rip up selected PCB traces and reroute them locally. Prefer trace ids from the PCB frontend selection. If the user explicitly names net targets such as `net13` or `NET_A1`, pass the original text to `drop_net` so those names can seed `selectedNets`.

This skill is separate from `hardware/pcb-intelligence`:
- BGA fanout / full escape routing uses `route` with `routerType` equal to `arc` or `135`.
- Local selected-trace rip-up / reroute uses `drop_net` followed by `reroute`.
- Do not call the global BGA fanout `route` tool from this skill.

## Trigger

Trigger only when the user clearly asks for local rip-up/reroute, selected trace deletion followed by reroute, reroute selected traces, or similar PCB routing work.

Examples:
- "把我框选的走线拆掉后重布"
- "reroute selected traces"
- "对选中的几根线 delete 后重新走"
- "局部拆线重布"

Do not call tools for conceptual questions, explanations, router selection for BGA fanout, or requests that explicitly ask not to modify the board.

## Tools

| Tool | Purpose |
|------|---------|
| `drop_net` | Compatibility entrypoint for the local rip-up stage. Internally calls `getSelectedElements` with `PFindType="TRACES"`, then `deleteTracesById`, then `getProjectData`. |
| `reroute` | Reads the cached selected trace ids and post-delete board data, generates local reroute output, check report, and public txt result file for EDA import. |

Frontend tools are called by `drop_net`; do not call them manually unless explicitly debugging:
- `getSelectedElements(PFindType="TRACES")`
- `deleteTracesById(ids)`
- `getProjectData()`

## Required Flow

1. Confirm the request is a local selected-trace rip-up/reroute request.
2. Call `drop_net(userText, projectID)`.
   - `userText` is the original user request.
   - `projectID` comes from the incoming message `projectid` field if available.
   - `drop_net` obtains selected trace ids from `getSelectedElements` with `PFindType="TRACES"`.
   - If no trace ids are selected but the user explicitly named nets, `drop_net` can proceed with `selectedNets` and refreshed project data.
   - `drop_net` rejects an empty selection only when no explicit net names are present.
   - `drop_net` rejects selections with more than 40 ids and ends the skill.
   - `drop_net` calls `deleteTracesById` only when the selected id count is between 1 and 40.
   - `drop_net` calls `getProjectData` after successful deletion to refresh board data.
   - `drop_net` caches `selectedTraceIds`, `droppedBoardData`, `droppedObjects`, and `localContext` for the current session.
3. Parse the JSON returned by `drop_net`.
4. If `drop_net` returns `error`, report that error to the user and stop. Do not call `reroute`.
5. Call `reroute()` only after `drop_net` succeeds.
6. Parse the JSON returned by `reroute`.
7. Return final structured fields inside `##PCB_FIELDS##`.

## Failure Handling

- If no selected traces are returned and the user did not explicitly name nets, tell the user to box-select the traces in the PCB frontend first.
- If more than 40 traces are selected, tell the user to reduce the selection and rerun.
- If `deleteTracesById` fails, report the delete result and stop.
- If `reroute` returns `checkReport.passed=false`, include the explanation and the returned file path if present.
- Do not invent trace ids, net ids, or file paths.

## Output Format

```text
局部拆线重布已完成。

##PCB_FIELDS##
{
  "rerouteResult": {
    "type": "local_reroute",
    "mode": "selected_traces_after_delete",
    "selectedTraceIds": ["2386476278", "3424247826"],
    "operations": [],
    "drcPassed": true,
    "drcIterations": 1,
    "routedLayoutTxtFilePath": "F:\\project\\.hermes_reroute\\txt\\session_iter1.txt"
  },
  "routedLayoutTxtFilePath": "F:\\project\\.hermes_reroute\\txt\\session_iter1.txt",
  "checkReport": {
    "passed": true,
    "checks": []
  },
  "explanation": "已基于选中走线完成局部重布并通过 DRC。"
}
##PCB_FIELDS_END##
```

## Constraints

- Do not invent deletion targets. Only use net names that the user explicitly wrote, or trace ids returned by the frontend selection.
- Do not call `route`.
- Do not ask the user to choose `arc` or `135`; those are BGA fanout routers, not this local reroute flow.
- Do not call `reroute` if selected trace id count is 0 or greater than 40.
- Do not call `reroute` if `deleteTracesById` fails.
- `##PCB_FIELDS##` content must be valid JSON.
- Do not expose internal board file paths in visible text or `##PCB_FIELDS##`; only expose the txt/S-expression result path via `routedLayoutTxtFilePath`.
- Keep visible text short; large board data, patches, and DRC details belong in structured fields or output files.
