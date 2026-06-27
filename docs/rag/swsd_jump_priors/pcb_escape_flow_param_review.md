---
kind: swsd_jump_prior
workflow_id: pcb_escape_flow
state: param_review
state_meaning: fanoutParams 已生成，等待用户确认或修改参数。
legal_actions:
  - confirm_route
  - modify_params
  - modify_router_choice
  - modify_order_lines
  - modify_constraints
  - rerun_fanout
  - rollback_checkpoint
  - restore_params_version
  - restore_layout_checkpoint
  - change_target
  - cancel_flow
---

# pcb_escape_flow / param_review

This state means fanoutParams already exist. User confirmation should start routing; parameter changes should go back to parameter generation.

## confirm_route

```text
param_review --confirm_route--> routing
```

Trigger examples:

```text
确认
开始布线
按这个参数执行
继续 route
就按这版参数跑
```

Expected candidate:

```json
{"action":"confirm_route","reason":"用户在 param_review 确认 fanoutParams，准备进入 routing"}
```

Do not map this to `confirm_import`. `param_review` has parameters, not routingResult.

## modify_params

```text
param_review --modify_params--> layer_assign_escape_order
```

Trigger examples:

```text
线宽改成 3mil
线距也改成 3mil
参数不对，重新生成一下
```

Expected candidate:

```json
{"action":"modify_params","entities":{"constraints":{"LineWidth":3,"LineSpacing":3}},"reason":"用户要求修改 fanout 参数或约束"}
```

After modification, regenerate fanoutParams and return to `param_review`. Do not route directly.

## modify_router_choice

```text
param_review --modify_router_choice--> layer_assign_escape_order
```

Trigger examples:

```text
改成 135+RL
不用 arc，用 135
routerType 换成 rl_arc
```

Expected candidate:

```json
{"action":"modify_router_choice","entities":{"routerType":"135+RL"},"reason":"用户要求修改 routerType 或布线策略"}
```

## modify_order_lines

```text
param_review --modify_order_lines--> layer_assign_escape_order
```

Trigger examples:

```text
逃逸顺序改一下
先走内层再走外层
把 orderLines 调整为...
```

Expected candidate:

```json
{"action":"modify_order_lines","reason":"用户要求修改 escape order / orderLines"}
```

## modify_constraints

```text
param_review --modify_constraints--> layer_assign_escape_order
```

Trigger examples:

```text
线宽/线距都改成 3mil
via 间距收紧一点
约束改一下
```

Expected candidate:

```json
{"action":"modify_constraints","entities":{"constraints":{"LineWidth":3,"LineSpacing":3}},"reason":"用户要求修改布线约束"}
```

## rerun_fanout

```text
param_review --rerun_fanout--> layer_assign_escape_order
```

Trigger examples:

```text
重新生成 fanout
再跑一轮 fanout 参数
重新来一次层分配
```

Expected candidate:

```json
{"action":"rerun_fanout","reason":"用户要求基于当前上下文重新生成 fanout"}
```

## restore_params_version

```text
param_review --restore_params_version--> param_review
```

Trigger examples:

```text
恢复上一个参数版本
用 version 3 的 fanout 参数
恢复昨天那版线宽线距
```

Expected candidate:

```json
{"action":"restore_params_version","reason":"用户要求恢复 fanoutParams 版本，恢复后仍需在 param_review 等待确认"}
```

## restore_layout_checkpoint

```text
param_review --restore_layout_checkpoint--> review
```

Trigger examples:

```text
恢复上一版版图结果
恢复 layout checkpoint
```

Expected candidate:

```json
{"action":"restore_layout_checkpoint","reason":"用户要求恢复 layout/routing checkpoint，恢复后进入结果 review"}
```

## change_target

```text
param_review --change_target--> select_bga
```

Trigger examples:

```text
换成 U7
目标 BGA 改成 U5
不要 U3 了，给 U9 做 fanout
```

Expected candidate:

```json
{"action":"change_target","entities":{"selectedBGA":"U7"},"reason":"用户要求切换目标 BGA"}
```

## rollback_checkpoint

```text
param_review --rollback_checkpoint--> previous_checkpoint_state
```

Trigger examples:

```text
回到上一步
撤回刚才的参数
rollback
```

Expected candidate:

```json
{"action":"rollback_checkpoint","reason":"用户要求恢复上一个 fanout checkpoint"}
```

## cancel_flow

```text
param_review --cancel_flow--> idle
```

Trigger examples:

```text
取消
退出流程
不做了
```

Expected candidate:

```json
{"action":"cancel_flow","reason":"用户要求退出当前 fanout workflow"}
```
