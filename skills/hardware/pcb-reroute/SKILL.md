---
name: pcb-reroute
version: 1.3.0
description: PCB local selected-trace rip-up and reroute flow using one frontend delete/reroute-parameter call, model generation, and DRC validation
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

Use this skill when the user asks to rip up selected PCB traces and reroute them locally. The user should box-select the traces in the PCB frontend first; the frontend deletion tool returns the missing-route endpoints and post-delete board data needed by `reroute`.

This skill is separate from `hardware/pcb-intelligence`:
- BGA fanout / full escape routing uses `route` with `routerType` equal to `arc` or `135`.
- Local selected-trace rip-up / reroute uses `deleteTracesForRerouting` followed by `reroute`.
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
| `deleteTracesForRerouting` | Calls the PCB frontend one-shot synchronous tool. The frontend asks/uses the user's box selection, deletes selected traces and vias, exports board data, and returns `missing_routes` plus `projectData`. |
| `reroute` | Reads the cached missing routes and post-delete board data, generates local reroute output, check report, and public txt result file for EDA import. |
| `drop_net` | Compatibility alias only. Do not use it for the normal flow. |

Do not call the old three-step frontend deletion flow in the normal skill path:
- `getSelectedElements(PFindType="TRACES")`
- `deleteTracesById(ids)`
- `getProjectData()`

## Required Flow

1. Confirm the request is a local selected-trace rip-up/reroute request.
2. Call `deleteTracesForRerouting(userText, projectID)`.
   - `userText` is the original user request.
   - `projectID` comes from the incoming message `projectid` field if available.
   - The frontend tool is synchronous and returns a `tool-results` payload whose `result` is a JSON string.
   - The result must contain `missing_routes` and `projectData`.
   - `projectData` may be a board-data file path or board text.
   - The tool caches `missingRoutes`, `selectedNets`, `droppedBoardData`, and `localContext` for the current session.
3. Parse the JSON returned by `deleteTracesForRerouting`.
4. If it returns `error`, report that error to the user and stop. Do not call `reroute`.
5. Call `reroute()` only after `deleteTracesForRerouting` succeeds.
6. Parse the JSON returned by `reroute`.
7. Return final structured fields inside `##PCB_FIELDS##`.

## Failure Handling

- If the frontend reports no selected traces, non-BGA traces, more than 40 pins, delete failure, missing `missing_routes`, or unreadable `projectData`, report that error and stop.
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
    "selectedNets": ["NET_U1_B7"],
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

- Do not invent deletion targets. Deletion targets must come from the frontend `deleteTracesForRerouting` result.
- Do not call `route`.
- Do not ask the user to choose `arc` or `135`; those are BGA fanout routers, not this local reroute flow.
- Do not call `reroute` if `deleteTracesForRerouting` returns an error.
- `##PCB_FIELDS##` content must be valid JSON.
- Do not expose internal board file paths in visible text or `##PCB_FIELDS##`; only expose the txt/S-expression result path via `routedLayoutTxtFilePath`.
- Keep visible text short; large board data, patches, and DRC details belong in structured fields or output files.
