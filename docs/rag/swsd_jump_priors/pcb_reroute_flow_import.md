---
kind: swsd_jump_prior
workflow_id: pcb_reroute_flow
state: import
state_meaning: 正在导入 reroute 结果或等待导入结果。
legal_actions:
  - reject_import
  - reroute_again
  - rollback_checkpoint
  - restore_layout_checkpoint
  - cancel_flow
---

# pcb_reroute_flow / import

## reject_import

```text
import --reject_import--> import
```

Triggers:

```text
取消导入
不导入
先别 import
```

Expected:

```json
{"action":"reject_import","reason":"用户拒绝导入 rerouteResult"}
```

Do not resend report. Ask whether to reroute again or switch workflow.

## reroute_again

```text
import --reroute_again--> rip_up
```

Triggers:

```text
先别导入，再 reroute 一次
```

Expected:

```json
{"action":"reroute_again","reason":"用户要求导入前重新 reroute"}
```

## rollback_checkpoint

```text
import --rollback_checkpoint--> report
```

Triggers:

```text
导入前回退一步
```

Expected:

```json
{"action":"rollback_checkpoint","reason":"用户要求导入前回退"}
```

## restore_layout_checkpoint

```text
import --restore_layout_checkpoint--> report
```

Triggers:

```text
先别导入，恢复上一版版图
```

Expected:

```json
{"action":"restore_layout_checkpoint","reason":"用户要求恢复 layout checkpoint"}
```

## cancel_flow

```text
import --cancel_flow--> idle
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
