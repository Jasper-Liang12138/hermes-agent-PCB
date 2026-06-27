---
kind: swsd_jump_prior
workflow_id: pcb_escape_flow
state: review
state_meaning: routingResult 已生成，等待用户检查、拒绝、修改或确认导入。
legal_actions:
  - confirm_import
  - reject_route
  - modify_params
  - modify_router_choice
  - modify_order_lines
  - modify_constraints
  - rerun_fanout
  - restore_params_version
  - restore_layout_checkpoint
  - change_target
  - rollback_checkpoint
  - cancel_flow
---

# pcb_escape_flow / review

This state means routingResult exists. Do not treat plain “确认” as route execution unless user clearly means reroute/route again. “确认导入” maps to import.

## confirm_import

```text
review --confirm_import--> import
```

Triggers:

```text
确认导入
导入这个结果
可以 import
把这个布线结果导入
```

Expected:

```json
{"action":"confirm_import","reason":"用户在 routing result review 阶段确认导入"}
```

## reject_route

```text
review --reject_route--> review
```

Triggers:

```text
不接受这个布线结果
这个结果不要
先不导入这个 route 结果
```

Expected:

```json
{"action":"reject_route","reason":"用户拒绝当前 routingResult"}
```

## modify_params

```text
review --modify_params--> layer_assign_escape_order
```

Triggers:

```text
这个布线结果不满意，线宽改 4mil 后重跑
线距改成 3mil 重新生成
```

Expected:

```json
{"action":"modify_params","reason":"用户要求修改参数并重新生成 fanoutParams"}
```

## modify_router_choice

```text
review --modify_router_choice--> layer_assign_escape_order
```

Triggers:

```text
这次结果不好，换 135+RL 再生成
改成 arc 重跑
```

Expected:

```json
{"action":"modify_router_choice","entities":{"routerType":"135+RL"},"reason":"用户要求修改 routerType 后重跑"}
```

## modify_order_lines

```text
review --modify_order_lines--> layer_assign_escape_order
```

Triggers:

```text
这个走线顺序不对，调整逃逸顺序后重跑
```

Expected:

```json
{"action":"modify_order_lines","reason":"用户要求修改逃逸顺序后重跑"}
```

## modify_constraints

```text
review --modify_constraints--> layer_assign_escape_order
```

Triggers:

```text
DRC 风险有点高，把约束改宽松后重跑
```

Expected:

```json
{"action":"modify_constraints","reason":"用户要求修改约束后重跑"}
```

## rerun_fanout

```text
review --rerun_fanout--> layer_assign_escape_order
```

Triggers:

```text
结果不行，重新 fanout
重新生成 fanout
```

Expected:

```json
{"action":"rerun_fanout","reason":"用户要求重新 fanout"}
```

## restore_params_version

```text
review --restore_params_version--> param_review
```

Triggers:

```text
这个布线结果不好，恢复上一版参数
```

Expected:

```json
{"action":"restore_params_version","reason":"用户要求恢复参数版本并回到 param_review"}
```

## restore_layout_checkpoint

```text
review --restore_layout_checkpoint--> review
```

Triggers:

```text
恢复上一版版图结果
恢复 layout checkpoint
```

Expected:

```json
{"action":"restore_layout_checkpoint","reason":"用户要求恢复 layout checkpoint"}
```

## change_target

```text
review --change_target--> select_bga
```

Triggers:

```text
这个结果先不要了，换 U7 重新来
```

Expected:

```json
{"action":"change_target","entities":{"selectedBGA":"U7"},"reason":"用户要求切换目标 BGA"}
```

## rollback_checkpoint

```text
review --rollback_checkpoint--> previous_checkpoint_state
```

Triggers:

```text
这个结果不对，回退一步
```

Expected:

```json
{"action":"rollback_checkpoint","reason":"用户要求恢复上一个 checkpoint"}
```

## cancel_flow

```text
review --cancel_flow--> idle
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
