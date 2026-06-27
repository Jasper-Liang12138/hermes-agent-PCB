---
kind: swsd_jump_prior_index
workflow_ids:
  - pcb_escape_flow
  - pcb_reroute_flow
usage: Retrieve by workflow_id + current state before SWSD jump arbitration.
---

# SWSD Jump Priors RAG Index

Use these documents as retrieval units for SWSD jump arbitration.

Retrieval key should include:

```text
workflow_id
current_state
user_text
candidate action if available
```

Recommended retrieval order:

1. Exact `workflow_id + state` document.
2. `cross_workflow_jumps.md` if user asks to switch between fanout and reroute.
3. `workflow_chat_resume.md` if user asks an explanatory question inside workflow or says “继续” after chat.
4. `disambiguation_rules.md` for ambiguous confirm/reject/retry wording.
5. Adjacent state document only if exact state has no matching action.

Documents:

```text
pcb_escape_flow_param_review.md
pcb_escape_flow_review.md
pcb_escape_flow_import.md
pcb_escape_flow_layer_assign_escape_order.md
pcb_reroute_flow_confirm.md
pcb_reroute_flow_report.md
pcb_reroute_flow_import.md
pcb_reroute_flow_drc_loop.md
cross_workflow_jumps.md
workflow_chat_resume.md
disambiguation_rules.md
```
