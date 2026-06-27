---
kind: swsd_jump_prior
workflow_id: pcb_escape_flow
state: import
state_meaning: 已进入导入阶段或等待导入结果。
legal_actions:
  - reject_import
  - modify_params
  - rerun_fanout
  - restore_params_version
  - restore_layout_checkpoint
  - change_target
  - rollback_checkpoint
  - cancel_flow
---

# pcb_escape_flow / import

## reject_import

```text
import --reject_import--> import
```

Triggers:

```text
不导入
取消导入
先别 import
```

Expected:

```json
{"action":"reject_import","reason":"用户拒绝导入当前 routing result"}
```

Do not resend report after reject_import.

## modify_params

```text
import --modify_params--> layer_assign_escape_order
```

Triggers:

```text
先别导入，把线距改一下
先不导入，线宽改成 4mil
```

Expected:

```json
{"action":"modify_params","reason":"用户在导入前要求修改参数"}
```

## rerun_fanout

```text
import --rerun_fanout--> layer_assign_escape_order
```

Triggers:

```text
先不导入，重新 fanout 一次
```

Expected:

```json
{"action":"rerun_fanout","reason":"用户在导入前要求重新 fanout"}
```

## restore_params_version

```text
import --restore_params_version--> param_review
```

Triggers:

```text
先别导入，恢复上一版 fanout 参数
```

Expected:

```json
{"action":"restore_params_version","reason":"用户要求恢复 fanoutParams 版本"}
```

## restore_layout_checkpoint

```text
import --restore_layout_checkpoint--> review
```

Triggers:

```text
导入前先恢复上一个 layout
```

Expected:

```json
{"action":"restore_layout_checkpoint","reason":"用户要求恢复 layout checkpoint"}
```

## change_target

```text
import --change_target--> select_bga
```

Triggers:

```text
先别导入，换成 U7
```

Expected:

```json
{"action":"change_target","entities":{"selectedBGA":"U7"},"reason":"用户要求切换目标 BGA"}
```

## rollback_checkpoint

```text
import --rollback_checkpoint--> previous_checkpoint_state
```

Triggers:

```text
导入前回退一步
```

Expected:

```json
{"action":"rollback_checkpoint","reason":"用户要求导入前回退"}
```

## cancel_flow

```text
import --cancel_flow--> idle
```

Triggers:

```text
取消
退出流程
```

Expected:

```json
{"action":"cancel_flow","reason":"用户要求退出当前 fanout workflow"}
```
