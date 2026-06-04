# PCB Agent 意图识别经验文档

更新时间：2026-05-30

本文记录 PCB Agent 在 WebSocket 场景下做意图识别、tool-call 选择和方案 B 文本工具调用适配时的经验。目标是提高开源模型接入后的稳定性，尤其是 BGA 扇出布线和局部拆线重布两条链路。

## 结论

可以利用 Hermes Agent 的 memory，但不能把准确率完全押在 memory 上。

推荐结构是：

1. WebSocket adapter 做第一层确定性路由：普通问答、支持排查、配置/日志问题不暴露 PCB 工具；明确 PCB 操作才进入 PCB agent loop。
2. Agent prompt / skill 描述业务流程和允许的工具。
3. 方案 B 文本 tool-call shim 负责把不支持 OpenAI tools 的模型输出解析成工具调用。
4. 对关键 PCB 状态机做确定性校正：模型输出漂移时，按已有工具历史强制修正下一步。
5. Memory 只保存稳定的意图识别经验、反例和用户偏好，作为少量 few-shot/规则提醒。

也就是说，memory 是“经验提示层”，不是“安全执行层”。必须 100% 正确的动作，例如确认后只能调用 `route`，要写成代码校正或状态机。

## 为什么不能只靠模型

在真实 `wishub-x5` 接口测试中，原始模型输出出现过这些漂移：

- `getProjectData` 返回后，模型自己编出 BGA 列表，没有调用 `pcb_extract_bga`。
- 用户选择 `U22 + arc` 后，模型调用了无关工具。
- 用户确认后，模型没有调用 `route`，反而重新调用 `generateFanoutParams`。
- 模型会臆造 `selectedBGA`、线宽/间距、网络名或约束字段。

这些不是 WebSocket 或布线器问题，而是开源模型在长 prompt、多工具、多轮历史下的工具选择不稳定。prompt 能缓解，但不能保证。

## 意图分类

### chat

普通聊天、概念解释、排查配置、查看日志、端口问题、打包交付、Git 操作等，归为 `chat`。

示例：

- “BGA 和 QFP 有什么区别？”
- “config.ini 没生效吗？”
- “7073 端口连不上”
- “怎么查看日志？”
- “帮我重新压缩交付包”

处理原则：

- 不暴露 PCB 布线工具。
- 不调用 `getProjectData`。
- 直接回答或执行本地工程操作。

### bga_fanout

用户明确要求对当前 PCB 版图做 BGA 逃逸、扇出、布线，归为 `bga_fanout`。

示例：

- “帮我进行 BGA 逃逸布线”
- “对 U22 做 fanout”
- “#全局fanout”
- “#布线”
- “选择 U22，arc + 北科大”
- “确认执行布线”

主链路：

```text
getProjectData -> pcb_extract_bga -> generateFanoutParams -> route
```

关键规则：

- 没有 `getProjectData` 结果时，第一步只能是 `getProjectData`。
- 有 `getProjectData` 结果但没有 `pcb_extract_bga` 结果时，下一步只能是 `pcb_extract_bga(board_text="__CACHED_PROJECT_DATA__")`。
- `selectedBGA` 和 `routerType` 都确定后，调用 `generateFanoutParams`。
- 已有 `fanoutParams` 且用户确认后，调用 `route`。
- 不能让模型自己分析 raw board data。
- 不能让模型臆造 BGA、网络、层、线宽、间距、文件路径。

### selected_trace_reroute

用户明确要求做删除、拆线后重布、局部重布，归为 `selected_trace_reroute`。

示例：

- “把我选中的线拆掉重布”
- “局部重布当前选中的几根线”
- “删除选中走线后 reroute”
- “#拆线重布”
- “#reroute”

主链路：

```text
deleteTracesForRerouting -> reroute
```

关键规则：

- 删除对象只能来自前端选中 traces。
- 不要从文本里臆造 trace id 或 net。
- 用户可以先进入拆线重布流程；如果没有选中对象，`deleteTracesForRerouting` 应由前端返回错误，Agent 停止并提示用户先在前端框选。

### 流程中的中途意图

PCB 流程进行中要区分三种意图：

1. 临时 chat：例如“解释一下 RL 是什么意思”。应临时按 chat 回复，并保留原 `flow_state`，不要清掉 BGA fanout 或 reroute 流程。
2. 取消流程：例如“取消”“退出”“中止当前流程”。应 reset 当前 PCB flow，回到普通聊天。
3. 切换任务：例如“取消当前，#拆线重布”或“先拆线重布”。应清掉当前 BGA flow，并进入 reroute flow；`#全局fanout` / `#布线` 则强制进入 BGA fanout flow。

## 方案 B 的推荐实现

方案 B 适用于模型接口不支持 OpenAI native tools 的情况。

### 1. 不发送 `tools`

对已知不兼容 endpoint，不发送 OpenAI `tools` 字段，而是在 system message 里注入工具列表和文本 tool-call 协议。

模型需要输出：

```xml
<tool_call>{"name":"getProjectData","arguments":{}}</tool_call>
```

Agent 解析后合成 OpenAI 风格的 `tool_calls`，再走原有工具执行逻辑。

### 2. 历史工具调用要转成文本

非 native tools 模型看不懂历史里的 `role=tool`。需要把历史转换成：

```xml
<tool_call>{"name":"getProjectData","arguments":{}}</tool_call>
<tool_response>
{"tool_call_id":"call_xxx","name":"getProjectData","content":"..."}
</tool_response>
```

`tool_response` 必须包含 `name`，否则模型很容易不知道哪个工具已经返回。

### 3. 对 PCB 主链路加确定性校正

即使模型输出了错误 tool-call，Agent 也应根据历史状态校正：

| 当前状态 | 用户输入 | 最终工具 |
|---|---|---|
| 无 `getProjectData` 结果 | BGA 操作请求 | `getProjectData` |
| 有 `getProjectData`，无 `pcb_extract_bga` | 任意后续 | `pcb_extract_bga` |
| 有 BGA 提取结果，用户给出 BGA + 算法 | `选择 U22，arc` | `generateFanoutParams` |
| 有 `fanoutParams`，用户确认 | `确认` | `route` |

这层校正应只在 PCB toolset 存在、且已进入 PCB agent loop 时启用，避免影响普通聊天。

## Memory 的使用建议

Hermes 的 built-in memory 默认会进入 system prompt，但有长度限制。它适合保存短小、稳定、跨会话有效的经验。

推荐保存：

- 常见误判反例，例如“config.ini、端口、日志、打包、Git 不是 PCB 布线操作”。
- 用户偏好的术语映射，例如“北科大模块”指层分配/逃逸顺序生成模块，不等于走线算法。
- 已验证的工具链顺序，例如 BGA 主链路和 reroute 主链路。
- 模型容易犯错的禁止项，例如不要从 raw board data 里自己编 BGA。

不推荐保存：

- 当前项目的 `projectid`、`sessionId`。
- API key、绝对私密路径、客户数据。
- 某一次版图里的 BGA 列表、网络名、pin 信息。
- 必须实时判断的状态，例如当前是否已有 `fanoutParams`。

建议 memory 内容保持短句规则，不要写成长篇文档。例如：

```text
PCB intent rule: config.ini, port, logs, package, Git, and frontend debug questions are support-chat; do not call getProjectData for them.
PCB fanout rule: BGA fanout flow must be getProjectData -> pcb_extract_bga -> generateFanoutParams -> route.
PCB fanout rule: after getProjectData, never let the model inspect raw board data; call pcb_extract_bga with board_text="__CACHED_PROJECT_DATA__".
PCB fanout rule: after fanoutParams exist and the user confirms, route is the only allowed next tool.
PCB reroute rule: selected-trace reroute uses deleteTracesForRerouting -> reroute; deletion targets and missing routes must come from the frontend result, not text guesses.
```

## 如何把本文经验接入 Agent

短期：

- 保留 WebSocket adapter 的 route gate。
- 保留方案 B 文本 tool-call shim。
- 保留 PCB 主链路 deterministic override。
- 将上面的 memory 短句写入 Hermes memory 或 profile memory。

## 源码和 exe 交付方式

默认 PCB 意图识别 memory 交付源文件放在：

```text
.github/delivery/memories/intention_memory.md
```

源码 demo 交付时保留该文件即可。用户如果用源码运行，可以将它复制到：

```text
%USERPROFILE%\.hermes\memories\MEMORY.md
```

exe delivery 交付时，`scripts/package-delivery-windows.ps1` 会把默认 memory 复制到交付包：

```text
PCB-AGENT\memories\intention_memory.md
```

用户首次运行 `install.bat` 时，会把它安装到：

```text
%USERPROFILE%\.hermes\memories\MEMORY.md
```

注意：`intention_memory.md` 是交付包中的默认规则文件名；`MEMORY.md` 是 Hermes built-in memory 运行时固定读取的文件名。

安装脚本不会覆盖用户已有的 `MEMORY.md`。如果客户机器上已经有 memory，需要人工合并新增规则，避免覆盖用户自己的长期记忆。

交付包的 `template-config.yaml` 需要启用 built-in memory：

```yaml
memory:
  memory_enabled: true
  user_profile_enabled: true
```

Hermes memory 是 session 启动时注入 system prompt 的快照。安装或修改 `MEMORY.md` 后，需要重启 gateway/agent，或开启新 session，规则才会进入模型上下文。

中期：

- 将意图分类抽成独立函数，输出 `chat`、`bga_fanout`、`selected_trace_reroute`、`unknown`。
- 为每类意图建立正例/反例测试集。
- 对每次误判记录：用户原文、暴露工具、模型原始输出、最终校正输出。

长期：

- 用真实联调日志构建小型 eval 集。
- 每次改 prompt、skill、shim 或工具 schema 后跑 eval。
- 对开源模型保留状态机兜底；不要假设模型会稳定遵守工具协议。

## 判断标准

意图识别准确率不能只看模型第一反应，要看最终 Agent 行为：

- 普通问题是否没有暴露或调用 PCB 工具。
- BGA 操作是否严格进入 `getProjectData -> pcb_extract_bga -> generateFanoutParams -> route`。
- 选中走线重布是否严格进入 `deleteTracesForRerouting -> reroute`。
- 用户确认后是否不会回退到上一步。
- 是否没有臆造 BGA、网络、层、线宽、间距、文件路径。

当前测试说明：方案 B 能让不兼容 native tools 的接口跑通，但提升稳定性的关键不是“让模型更聪明”，而是让 Agent 在关键业务链路上有明确、可测试、可回放的状态约束。
