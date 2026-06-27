---
kind: swsd_jump_prior
workflow_id: pcb_escape_flow
state: layer_assign_escape_order
state_meaning: 正在生成层分配和逃逸顺序 / fanoutParams。
legal_actions:
  - modify_params
  - modify_router_choice
  - modify_order_lines
  - modify_constraints
  - change_target
  - rollback_checkpoint
  - cancel_flow
---

# pcb_escape_flow / layer_assign_escape_order

This state is generation-oriented. User changes should update generation inputs. Do not route until fanoutParams are generated and reviewed in `param_review`.

## modify_params / modify_router_choice / modify_order_lines / modify_constraints

```text
layer_assign_escape_order --modify_*--> layer_assign_escape_order
```

Triggers:

```text
线宽改成 3mil
改成 135+RL
逃逸顺序改一下
约束改一下
```

Expected:

```json
{"action":"modify_params","reason":"用户在参数生成阶段继续修改生成条件"}
```

If the change is specifically routerType, prefer `modify_router_choice`. If specifically constraints, prefer `modify_constraints`. If specifically orderLines, prefer `modify_order_lines`.

## change_target

```text
layer_assign_escape_order --change_target--> select_bga
```

Triggers:

```text
换成 U7
目标改成 U5
```

Expected:

```json
{"action":"change_target","reason":"用户在参数生成阶段切换目标 BGA"}
```

## rollback_checkpoint

```text
layer_assign_escape_order --rollback_checkpoint--> previous_checkpoint_state
```

Triggers:

```text
回到上一步
撤回
```

Expected:

```json
{"action":"rollback_checkpoint","reason":"用户要求回退参数生成阶段"}
```

## cancel_flow

```text
layer_assign_escape_order --cancel_flow--> idle
```

Triggers:

```text
取消
不做了
```

Expected:

```json
{"action":"cancel_flow","reason":"用户要求退出当前 fanout workflow"}
```
