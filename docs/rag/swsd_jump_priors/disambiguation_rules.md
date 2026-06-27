---
kind: swsd_jump_disambiguation_rules
usage: Retrieve with any SWSD jump prior when user text is ambiguous.
---

# SWSD Jump Disambiguation Rules

## Confirmation depends on state

```text
pcb_escape_flow / param_review + “确认” => confirm_route
pcb_escape_flow / review + “确认导入” => confirm_import
pcb_reroute_flow / confirm + “确认” => confirm_reroute
pcb_reroute_flow / report + “确认导入” => confirm_import
```

Never infer `confirm_import` from `param_review`.
Never infer `confirm_reroute` outside `pcb_reroute_flow / confirm`.

## Reject depends on state

```text
pcb_escape_flow / review + “不接受这个结果” => reject_route
pcb_escape_flow / import + “不导入” => reject_import
pcb_reroute_flow / report + “不导入” => reject_import
pcb_reroute_flow / import + “取消导入” => reject_import
```

After `reject_import`, do not resend report.

## “重新” depends on target object

```text
重新 fanout / 重新生成参数 => rerun_fanout
重新拆线重布 / 再 reroute => reroute_again
重新导入 => confirm_import only if importable result exists
```

## Parameter changes should regenerate parameters

```text
param_review + 修改线宽/线距/routerType/orderLines
=> modify_params / modify_router_choice / modify_constraints / modify_order_lines
=> target state layer_assign_escape_order
=> regenerate fanoutParams
=> then param_review
```

Do not go directly from parameter modification to `routing`.

## Chat is not jump

These should normally be `chat`, not jump:

```text
为什么这样走线？
这个参数是什么意思？
能解释一下 DRC 报告吗？
135+RL 是什么？
```

## Execute is not jump

New workflow entry should be execute, not jump:

```text
fanout / 给 U5 布线 => pcb_escape_flow execute entry
拆线重布 / reroute => pcb_reroute_flow execute entry
```
