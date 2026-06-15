# PCB Intent 数据集构建需求

数据集：`F:\doctor\hermes-agent\邮件\intent_training_500.jsonl`  
用途：为 SWSD3 / PCB workflow 构建下一版更贴近真实使用方式的意图数据集

## 一、目标

当前 500 条数据集已经能较好评估：

- `idle` 状态下的入口意图识别
- PCB 流程内少量当前步骤判断
- `chat / pcb / unclear / cancel` 的基础边界

下一版数据集需要重点补足：

- 流程中途意图切换
- fallback / 失败后换方案
- 回退到上一步
- 重新执行某一步
- 对结果不满意后的重来

核心判断：

- 现有 500 条更像“当前这句话应该怎么路由”
- 下一版需要更像“流程跑到一半时，用户如何控制 workflow”

## 二、当前 500 条已覆盖的类型

| 类型 | 说明 | 例子 |
|---|---|---|
| 普通 chat | 不进入 PCB workflow 的普通表达 | `你好，我想问个问题。` |
| 概念咨询 | 询问 PCB / BGA / reroute 概念 | `什么是 BGA fanout？` |
| 分析 / 仅说明 | 分析、比较、讲步骤，不要求执行 | `拆线重布一般分几步？先别执行。` |
| fanout 入口 | 进入 escape / fanout workflow | `对 U23 做 BGA fanout。` |
| reroute 入口 | 进入 reroute workflow | `把选中的 traces 删除后重新布线。` |
| cancel | 取消当前动作或流程 | `先停一下，这轮取消。` |
| unclear | 信息不足，不能直接推进 | `帮忙看看？` |
| flow_select | 流程内选择目标 | `U55。` |
| flow_router | 流程内选择 router 类型 | `用 135。` |
| flow_confirm | 流程内确认继续执行 | `开始执行。` |
| flow_modify | 流程内改参数 | `目标还是 U23，但改成 4 层。` |
| flow_invalid | 当前状态下的非法输入 | `谢谢你真棒。` |

## 三、当前数据集的主要缺口

当前数据集缺的不是更多入口句，而是 workflow 中途控制样本。

这些缺口的共同特点：

- 多发生在非 `idle` 状态
- 强依赖前一步结果和 workflow memory
- 需要验证 checkpoint / rollback / rerun / state transition

## 四、当前 SWSD3 已能支持的中途 QA 操作

下面这些能力在当前 SWSD3 里已经有明确支持，下一版数据集应该保留并继续覆盖：

| 能力 | 问题示例 | 当前期望处理 |
|---|---|---|
| 流程内选择目标 | `U55。` | 如果当前在 `wait_selection`，识别为 `pcb_select_target`，继续 PCB workflow，不切回 chat。 |
| 流程内选择 router | `用 135。` | 如果当前在 `wait_router_type`，识别为 `pcb_followup`，继续当前 router 选择步骤。 |
| 流程内强确认 | `开始执行。` | 如果当前在 `wait_confirm`，识别为 `pcb_confirm_route`，允许继续后续执行工具链。 |
| 流程内弱确认拦截 | `好的。` | 如果当前在 `wait_confirm`，识别为 `unclear`，保持 `route_mode=pcb`，不直接调用执行工具。 |
| 流程内改参数 | `目标还是 U23，但改成 4 层。` | 保持当前 workflow，不新开流程，更新参数并进入当前步骤的重新计算。 |
| 流程内取消 | `先停一下，这轮取消。` | 识别为 `cancel`，退出当前执行流，回到 chat。 |
| 流程内非法输入保上下文 | `谢谢你真棒。` | 如果当前处于 `wait_selection / wait_router_type / wait_confirm`，不误推进工具调用，保留 PCB 上下文并等待澄清。 |
| 工具禁用型解释请求 | `先别执行，只告诉我下一步该怎么做。` | 在入口或流程内保持解释模式，本轮不应调用 `getProjectData`、`route`、`importLines`。 |

当前 SWSD3 也已经具备底层支持，但尚未被这 500 条充分验证的方向包括：

- rollback / 回到上一步
- 局部 rerun / 只重跑某一步
- workflow 中途切换
- fallback / 失败后换方案

## 五、下一版必须补的类型

### P0：优先补

| 类型 | 说明 | 问题示例 | 期望答案示例 |
|---|---|---|---|
| 中途意图切换 | 从 fanout 切到 reroute，或反过来 | `先别做 fanout 了，改成把刚才选中的线 reroute 一下。` | `这是中途意图切换。应停止继续 fanout 相关工具链，不再生成 fanoutParams / route；改为进入 reroute workflow，调用 reroute 所需工具。` |
| 回退到上一步 | 恢复到前一节点重新选择 | `回到选 BGA 那一步，我重新选。` | `这是回退请求。应恢复到 wait_selection 或对应 checkpoint，不继续当前步骤工具调用，不应直接 route。` |
| 重新执行某一步 | 只重跑局部步骤 | `重新生成 fanoutParams，其他步骤先别动。` | `这是局部 rerun。应只重跑 fanoutParams 生成，不重启整个 workflow，也不提前调用后续 route / importLines。` |
| 继续刚才步骤 | 依赖上下文继续推进 | `继续。` | `这是上下文续接。应根据当前 state 继续下一步工具调用；如果当前在 wait_confirm，可进入执行；如果上下文不足，不应盲调工具。` |
| 局部修正后重算 | 保留主任务，仅改一个参数 | `目标还是 U23，但把层数改成 6 层，然后重新算。` | `这是当前 workflow 内的参数修改后重算。应保留目标 U23，更新参数，重新调用当前步骤相关工具，不应新开会话。` |

### P1：第二批补

| 类型 | 说明 | 问题示例 | 期望答案示例 |
|---|---|---|---|
| fallback / 失败后换方案 | 当前方案失败后走备选路径 | `这个 router 不行就换 arc 再试一次。` | `这是 fallback。若当前方案失败，应切换到 arc 并重跑对应工具步骤；不应直接结束，也不应沿用失败结果继续 import。` |
| 不满意重来 | 对结果不满意后整体重做 | `这个 fanout 结果我不满意，重来一遍。` | `这是结果不满意后的重做。应丢弃当前结果，重新执行该 workflow 主步骤；如已有导入结果，不应默认复用。` |
| 结果后修正 | 看完结果再提出修正要求 | `这次还是太挤了，换个 router 再跑。` | `这是结果后修正。应基于已有上下文修改 router 参数并重跑 route，不应重新获取项目数据，也不应直接 import 旧结果。` |

### P2：增强补样

| 类型 | 说明 | 问题示例 | 期望答案示例 |
|---|---|---|---|
| 多意图混合表达 | 同一句里同时有分析、条件和执行 | `先比较 135 和 arc，如果差别不大就直接执行 arc。` | `这是分析 + 条件执行混合样本。应先进入分析或澄清阶段，不应立刻调用执行工具，除非条件已满足且策略明确。` |
| 禁止工具但保留任务 | 保持任务上下文，但不能真执行 | `先别调工具，只告诉我下一步该怎么做。` | `这是工具禁用约束。应保留当前 workflow 上下文，但本轮不得调用 getProjectData、route、importLines 等工具。` |
| 用户要求每步确认 | 把自动 workflow 改成人工确认式 | `后面每一步都先问我，再执行。` | `这是执行策略约束。应切换为逐步确认模式；后续每个关键工具调用前都需要等待用户确认。` |

## 六、建议的数据集组织方式

下一版不要只按 `intent` 分类，还需要显式保留状态信息。

建议每条样本至少包含：

- `text`
- `flow_state`
- `previous_step` 或等价上下文
- `expected_intent`
- `expected_route_mode`
- `expected_state_transition`
- `should_call_tool`
- `should_rollback`
- `should_rerun_step`

重点不是让模型只判断“这是什么意图”，而是让评测能回答：

- 该不该继续当前 workflow
- 该不该切到另一个 workflow
- 该不该 rollback
- 该不该只重跑局部步骤
- 该不该进入 fallback 分支

## 七、构建要求

新数据集应满足：

- 至少一半样本来自非 `idle` 状态
- P0 类型优先覆盖完整
- 每种缺口类型至少准备 20 条以上不同表述
- 样本要包含短句、口语句、礼貌句、含糊句、条件句
- 同一类型既要有明确表达，也要有轻微歧义表达

不建议继续只补：

- 更多 fanout 入口同义句
- 更多 reroute 入口同义句
- 更多静态概念问答

## 八、交付标准

构建完成后，新的数据集应能回答这几个问题：

- SWSD3 是否真的能处理流程中途改主意
- SWSD3 是否能利用状态管理做 rollback / rerun / fallback
- 系统在非 `idle` 状态下是否还能稳定区分 `chat / execute / unclear / cancel`
- 当前高分是否来自真实 workflow 能力，而不只是入口识别能力
