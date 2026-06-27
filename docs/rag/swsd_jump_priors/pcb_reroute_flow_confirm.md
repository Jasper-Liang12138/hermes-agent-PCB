---
kind: swsd_jump_prior
workflow_id: pcb_reroute_flow
state: confirm
state_meaning: rip-up 已完成，等待用户确认是否继续生成 reroute。
legal_actions:
  - confirm_reroute
  - reroute_again
  - cancel_flow
---

# pcb_reroute_flow / confirm

## confirm_reroute

```text
confirm --confirm_reroute--> reroute_llm
```

Triggers:

```text
确认
继续
开始重布
按这个选择继续 reroute
```

Expected:

```json
{"action":"confirm_reroute","reason":"用户确认 rip-up 上下文，进入 reroute 生成"}
```

Important: confirm in this state is not `confirm_import`.

## reroute_again

```text
confirm --reroute_again--> rip_up
```

Triggers:

```text
重新拆线
刚才选错了，重新来
```

Expected:

```json
{"action":"reroute_again","reason":"用户要求重新进入拆线准备"}
```

## cancel_flow

```text
confirm --cancel_flow--> idle
```

Triggers:

```text
取消
退出拆线重布
```

Expected:

```json
{"action":"cancel_flow","reason":"用户要求退出 reroute workflow"}
```
