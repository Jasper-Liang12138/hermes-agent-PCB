# Fanout Rerun Prior

适用范围：`pcb_escape_flow` 中用户对已经生成的 fanout 参数、routing result 或 import 前后的结果不满意，要求重新 fanout、重新扇出、重跑布线，或在重跑时修改线宽、线距、routerType、顺序等参数。

## 合规 jump

从这些状态可以跳转：

```text
pcb_escape_flow / param_review
pcb_escape_flow / routing
pcb_escape_flow / review
pcb_escape_flow / import
```

默认目标：

```text
action=rerun_fanout
target_state=layer_assign_escape_order
```

如果用户明确说“重新选择 BGA / 换一个 BGA / 选 U7 / 重新选目标器件”，则目标改为：

```text
action=change_target
target_state=select_bga
```

## 重要约束

第一版不自动回退 clean board path。用户通常会在 PCB 软件里手动清除线，再提出重新 fanout 请求，因此 SWSD 后续执行统一读取当前状态中的 projectData 路径。

如果用户在 jump 请求里带了参数，必须保存在 `entities`：

```json
{
  "constraints": {"LineWidth": 3, "LineSpacing": 3},
  "raw_constraints": {"line_width": "3mil", "line_spacing": "3mil"},
  "routerType": "135+RL"
}
```

## 面向模型的例子

```text
当前 state=review
用户：“重新 fanout”
输出：action=rerun_fanout, target_state=layer_assign_escape_order
```

```text
当前 state=review
用户：“重新 fanout，要改线宽为 3mil”
输出：action=rerun_fanout, target_state=layer_assign_escape_order, entities.constraints.LineWidth=3
```

```text
当前 state=import
用户：“结果不满意，重新扇出，线宽线距都改成 4mil”
输出：action=rerun_fanout, target_state=layer_assign_escape_order, entities.constraints.LineWidth=4, entities.constraints.LineSpacing=4
```

```text
当前 state=review
用户：“重新选择 U7 再 fanout”
输出：action=change_target, target_state=select_bga, entities.selectedBGA="U7"
```
