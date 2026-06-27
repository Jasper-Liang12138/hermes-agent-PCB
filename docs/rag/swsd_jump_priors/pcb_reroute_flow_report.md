---
kind: swsd_jump_prior
workflow_id: pcb_reroute_flow
state: report
state_meaning: rerouteResult/checkReport 已生成，等待用户确认导入、拒绝或重新 reroute。
legal_actions:
  - confirm_import
  - reject_import
  - reroute_again
  - rollback_checkpoint
  - restore_layout_checkpoint
  - cancel_flow
---

# pcb_reroute_flow / report

## confirm_import

```text
report --confirm_import--> import
```

Triggers:

```text
确认导入 reroute 结果
导入这个重布结果
可以 import
```

Expected:

```json
{"action":"confirm_import","reason":"用户确认导入 rerouteResult"}
```

## reject_import

```text
report --reject_import--> report
```

Triggers:

```text
不导入
先不 import
取消导入
```

Expected:

```json
{"action":"reject_import","reason":"用户拒绝导入 rerouteResult"}
```

Do not resend report. Ask whether to reroute again or switch workflow.

## reroute_again

```text
report --reroute_again--> rip_up
```

Triggers:

```text
这个 reroute 结果不行，重新拆线重布
再 reroute 一次
```

Expected:

```json
{"action":"reroute_again","reason":"用户要求重新 reroute"}
```

## rollback_checkpoint

```text
report --rollback_checkpoint--> previous_checkpoint_state
```

Triggers:

```text
回到上一步
撤回这次 reroute
```

Expected:

```json
{"action":"rollback_checkpoint","reason":"用户要求恢复 reroute checkpoint"}
```

## restore_layout_checkpoint

```text
report --restore_layout_checkpoint--> report
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
report --cancel_flow--> idle
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
