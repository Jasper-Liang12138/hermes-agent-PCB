---
kind: swsd_jump_prior
workflow_id: pcb_reroute_flow
state: drc_loop
state_meaning: 正在进行 DRC 检查和修正循环。
legal_actions:
  - reroute_again
  - restore_layout_checkpoint
  - cancel_flow
---

# pcb_reroute_flow / drc_loop

## reroute_again

```text
drc_loop --reroute_again--> rip_up
```

Triggers:

```text
重新拆线重布
别继续修了，重新 reroute
```

Expected:

```json
{"action":"reroute_again","reason":"用户要求从 DRC loop 重新进入 reroute"}
```

## restore_layout_checkpoint

```text
drc_loop --restore_layout_checkpoint--> report
```

Triggers:

```text
恢复 reroute 前的版图
恢复上一个 layout checkpoint
```

Expected:

```json
{"action":"restore_layout_checkpoint","reason":"用户要求恢复 reroute layout checkpoint"}
```

## cancel_flow

```text
drc_loop --cancel_flow--> idle
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
