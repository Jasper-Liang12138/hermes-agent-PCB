---
kind: swsd_cross_workflow_jump_prior
workflow_ids:
  - pcb_escape_flow
  - pcb_reroute_flow
usage: Retrieve when user asks to switch between fanout and reroute while a workflow is active.
legal_cross_workflow_jumps:
  - fanout_to_reroute_entry
  - reroute_to_fanout_entry
---

# SWSD Cross Workflow Jump Priors

This document defines the only legal cross-workflow jump scenarios.

Strict rules:

- Only two cross-workflow jumps are legal:
  - fanout process -> reroute start
  - reroute process -> fanout start
- Do not invent arbitrary cross-workflow state jumps.
- Cross-workflow jump always enters the target workflow from its entry/start state.
- Preserve enough source workflow context in state payload for rollback/debug, but do not continue executing the source workflow.
- If user asks a conceptual question about reroute/fanout, output `chat`, not cross-workflow jump.

## fanout process -> reroute start

Canonical transition:

```text
pcb_escape_flow / select_bga                 --reroute_entry--> pcb_reroute_flow / rip_up
pcb_escape_flow / layer_assign_escape_order  --reroute_entry--> pcb_reroute_flow / rip_up
pcb_escape_flow / param_review               --reroute_entry--> pcb_reroute_flow / rip_up
pcb_escape_flow / routing                    --reroute_entry--> pcb_reroute_flow / rip_up
pcb_escape_flow / review                     --reroute_entry--> pcb_reroute_flow / rip_up
pcb_escape_flow / import                     --reroute_entry--> pcb_reroute_flow / rip_up
```

Trigger examples:

```text
先不 fanout 了，拆线重布
这个结果不要，改做 reroute
现在去拆线重布
先停下，做局部 reroute
把选中的线拆了重布
```

Expected candidate:

```json
{"action":"reroute_entry","workflow_id":"pcb_reroute_flow","reason":"用户在 fanout 流程中明确切换到拆线重布入口"}
```

State control expectation:

```text
source workflow: pcb_escape_flow keeps checkpoint/history only
target workflow: pcb_reroute_flow starts at rip_up
target chain: deleteTracesForRerouting -> confirm -> reroute_llm -> drc_loop -> report
```

Do not map to:

```text
reroute_again
confirm_reroute
chat
```

unless user text is only explanatory or current workflow is already reroute.

## reroute process -> fanout start

Canonical transition:

```text
pcb_reroute_flow / rip_up       --pcb_entry--> pcb_escape_flow / select_bga
pcb_reroute_flow / confirm      --pcb_entry--> pcb_escape_flow / select_bga
pcb_reroute_flow / reroute_llm  --pcb_entry--> pcb_escape_flow / select_bga
pcb_reroute_flow / drc_loop     --pcb_entry--> pcb_escape_flow / select_bga
pcb_reroute_flow / report       --pcb_entry--> pcb_escape_flow / select_bga
pcb_reroute_flow / import       --pcb_entry--> pcb_escape_flow / select_bga
```

If user specifies target BGA, the fanout chain may skip target selection after project data is loaded:

```text
pcb_reroute_flow / report --pcb_entry + selectedBGA=U5--> pcb_escape_flow / layer_assign_escape_order
```

Trigger examples:

```text
先不 reroute 了，做全局 fanout
改成给 U5 做 fanout
切回 BGA 逃逸布线
别导入这个 reroute，给 U7 布线
```

Expected candidate:

```json
{"action":"pcb_entry","workflow_id":"pcb_escape_flow","entities":{"selectedBGA":"U5"},"reason":"用户在 reroute 流程中明确切换到 fanout 入口"}
```

State control expectation:

```text
source workflow: pcb_reroute_flow keeps checkpoint/history only
target workflow: pcb_escape_flow starts at select_bga or layer_assign_escape_order if target BGA is explicit
target chain: getProjectData -> pcb_extract_bga/select target -> layer_assign_escape_order -> param_review
```

Do not map to:

```text
reroute_again
confirm_import
restore_layout_checkpoint
```

unless the user explicitly asks to continue reroute or restore reroute layout.

## Negative examples

These are not cross-workflow jumps:

```text
reroute 是什么意思？ => chat
fanout 和 reroute 有什么区别？ => chat
这个结果为什么不好？ => chat
继续 => continue current workflow state, not cross-workflow jump
确认 => state-dependent confirm action, not cross-workflow jump
```
