---
kind: swsd_workflow_chat_resume_prior
workflow_ids:
  - pcb_escape_flow
  - pcb_reroute_flow
usage: Retrieve when user asks a chat/explanation question inside an active workflow, or says continue after workflow_inner_chat.
---

# SWSD Workflow Chat And Resume Priors

This document defines workflow-inner chat behavior and how to resume the original chain.

Strict rules:

- Workflow-inner chat is not a workflow jump.
- Chat must not execute PCB tools.
- Chat must not migrate workflow state.
- Chat must preserve current workflow_id, current_state, entities, and state_payload.
- After chat, user may say “继续 / 好的继续 / 按刚才继续”. This should continue from the preserved current_state.

## Workflow-inner chat

Canonical behavior:

```text
active workflow/state --chat--> same workflow/state
```

Examples:

```text
pcb_escape_flow / param_review --chat--> pcb_escape_flow / param_review
pcb_escape_flow / review       --chat--> pcb_escape_flow / review
pcb_reroute_flow / confirm     --chat--> pcb_reroute_flow / confirm
pcb_reroute_flow / report      --chat--> pcb_reroute_flow / report
```

Trigger examples:

```text
这个参数是什么意思？
为什么这样走线？
135+RL 是什么？
这个 DRC 报告怎么看？
为什么要先确认？
```

Expected candidate:

```json
{"action":"chat","reason":"用户在 workflow 内提出解释性问题，不应迁移 state 或执行工具"}
```

State control expectation:

```text
current workflow state remains unchanged
no PCB tool call
no routingResult/fanoutParams/rerouteResult generation
```

## Resume after chat: fanout param_review

Preserved state:

```text
pcb_escape_flow / param_review
```

User says:

```text
继续
好的继续
按刚才的参数继续
就这样执行
```

Expected action:

```json
{"action":"confirm_route","reason":"用户在 param_review 的 workflow-inner chat 后要求继续执行 route"}
```

State transition:

```text
pcb_escape_flow / param_review --confirm_route--> pcb_escape_flow / routing
```

## Resume after chat: fanout review

Preserved state:

```text
pcb_escape_flow / review
```

User says:

```text
继续
确认导入
那就导入
```

Expected action if user clearly means import:

```json
{"action":"confirm_import","reason":"用户在 routing result review 后要求继续导入"}
```

State transition:

```text
pcb_escape_flow / review --confirm_import--> pcb_escape_flow / import
```

If user only says “继续” and import intent is not clear, prefer clarify or chat-safe follow-up, because `review` can also mean user wants to inspect more.

## Resume after chat: reroute confirm

Preserved state:

```text
pcb_reroute_flow / confirm
```

User says:

```text
继续
确认
按刚才继续
开始重布
```

Expected action:

```json
{"action":"confirm_reroute","reason":"用户在 reroute confirm 的 workflow-inner chat 后要求继续生成 reroute"}
```

State transition:

```text
pcb_reroute_flow / confirm --confirm_reroute--> pcb_reroute_flow / reroute_llm
```

## Resume after chat: reroute report

Preserved state:

```text
pcb_reroute_flow / report
```

User says:

```text
继续
确认导入
导入吧
```

Expected action if user clearly means import:

```json
{"action":"confirm_import","reason":"用户在 reroute report 后要求继续导入"}
```

State transition:

```text
pcb_reroute_flow / report --confirm_import--> pcb_reroute_flow / import
```

If user only says “继续” and import intent is unclear, prefer clarify:

```json
{"action":"chat","reason":"用户说继续但未明确是导入还是重新 reroute，需要追问"}
```

## Plain chat outside workflow

If no active workflow exists:

```text
idle --chat--> idle
```

Trigger examples:

```text
fanout 是什么？
reroute 是什么？
PCB 逃逸布线怎么理解？
```

Expected candidate:

```json
{"action":"chat","reason":"无 active workflow，用户提出普通问题"}
```
