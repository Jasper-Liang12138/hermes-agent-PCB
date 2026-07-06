# 长短期记忆、Skill 注入与采集标志位技术路线

本文档用于技术交接和后续研发规划，说明当前 PCB Agent LangGraph 项目在记忆机制、Skill 注入、Fanout/Reroute 数据采集标志位三方面的现状与可扩展路线。

## 1. 当前项目整体状态

当前项目已经具备一个基础的有状态 LangGraph 工作流：

- `PCBLangGraphAgent` 负责会话续接和图调用。
- `PCBState` 是节点之间传递的统一状态。
- `intermediate_cache` 保存 fanout/reroute 流程中的关键中间结果。
- `PCBPlanner` 在 `plan` 节点根据状态决定下一步工具调用或回复。
- `build_tool_registry()` 固定注册前端工具、外部程序工具和分析工具。

但当前项目还没有实现以下能力：

- 没有接入 LangGraph 官方 checkpointer。
- 没有接入 LangGraph store 或其他长期 memory 存储。
- 没有 skill registry。
- 没有动态 skill 注入。
- WebSocket 最终消息没有完整透出 `task_type`、`workflow_id`、`workflow_state`、`current_stage`、`tool_history` 等采集字段。

因此，目前项目可以概括为：

```text
有状态 LangGraph workflow
+ 会话内短期状态
+ 流程中间缓存
- 持久化长期 memory
- 动态 skill 注入
- 标准化 telemetry 输出
```

## 2. 短期记忆与长期记忆

### 2.1 当前已有的短期记忆

当前项目已有两类“短期记忆”。

第一类是会话状态：

```text
PCBLangGraphAgent._session_states
```

它按 `session_id` 保存上一轮 `PCBState`，让同一个会话中的多轮对话可以继续。

例如用户上一轮已经选择了 BGA，下一轮回复“确认”时，Agent 可以从上轮状态中恢复当前流程位置。

第二类是流程缓存：

```text
PCBState.intermediate_cache
```

它保存流程推进需要的中间结果，例如：

- `projectData`
- `boardData`
- `bgaCandidates`
- `fanoutEntities`
- `fanoutParams`
- `layerAssignResult`
- `escapeOrderResult`
- `fanout_routeResult`
- `deleteTracesResult`
- `rerouteContext`
- `rerouteResult`
- `drcResult`
- `explainabilityReport`
- `importLinesResult`

这类缓存更像 workflow cache，作用是让 fanout/reroute 可以分阶段推进。

### 2.2 当前没有的长期记忆

当前项目没有 LangGraph 官方意义上的长期 memory：

- `build_graph()` 中当前是直接 `graph.compile()`。
- 没有传入 `checkpointer`。
- 没有使用 `store` 或 `BaseStore`。
- 没有跨服务重启保存 `PCBState`。
- 没有跨 session 保存用户偏好、项目偏好或经验知识。

这意味着：

- 服务进程还在时，同一 `session_id` 可以继续。
- 服务重启后，内存里的 `_session_states` 会丢失。
- 不同会话之间不会共享历史经验。

### 2.3 未来的长期记忆路线

未来可以按两个阶段实现。

第一阶段：接入 LangGraph checkpointer。

目标：

- 保存 thread-level 图状态。
- 支持服务重启后的流程恢复。
- 支持长流程 fanout/reroute 的可靠续跑。

推荐做法：

- 把 `session_id` 映射为 LangGraph `thread_id`。
- 在 `graph.compile()` 时接入 checkpointer。
- 开发阶段可用内存或 SQLite。
- 生产阶段建议使用 Postgres 等持久化后端。

第二阶段：接入长期 memory store。

目标：

- 保存跨会话、跨项目的可复用知识。
- 在 `plan` 节点规划前检索相关记忆。
- 把记忆作为 Planner 的上下文，而不是无条件塞进所有 prompt。

建议长期 memory 保存这些内容：

| 类型 | 示例 |
|---|---|
| 用户偏好 | 常用 router 类型、默认线宽线距、是否偏好自动导入 |
| 项目偏好 | 某项目常用 BGA、板层规则、设计约束 |
| 成功参数 | 某 BGA/约束组合下成功的 fanout 参数 |
| DRC 经验 | 某类 DRC 失败对应的 reroute 修复策略 |
| 工具经验 | 某外部工具的失败原因和处理建议 |

推荐注入位置：

```text
用户输入 + 当前 PCBState
-> 检索 user/project/routing memory
-> 拼入 Planner 状态摘要
-> PCBPlanner 生成下一步计划
```

## 3. Skill 注入机制

### 3.1 当前项目状态

当前项目没有 skill registry 或动态 skill 注入。

当前工具注册是固定的：

```text
build_tool_registry(config, frontend_sender)
```

所有工具在启动时统一注册，包括：

- `getProjectData`
- `importLines`
- `deleteTracesForRerouting`
- `layer_assign`
- `escape_order`
- `fanout_route`
- `reroute`
- `compress_reroute_context`
- `help_planner`
- `pcb_extra_bga`
- `drc_check`
- `explainability_report`

当前 Planner 的很多业务规则写在 `planner.py` 中，而不是由外部 skill 配置提供。

### 3.2 Skill 在 LangGraph 里的作用

Skill 不替代 LangGraph 的节点和边。

在这个项目里，推荐理解为：

```text
Graph = 流程骨架
Node = 执行位置
State = 上下文背包
Tool = 真正执行能力
Skill = 某类任务的专业说明书、工具范围、规则和示例
```

因此，Skill 最适合注入到 `plan` 节点，影响 `PCBPlanner` 如何规划下一步。

推荐运行方式：

```text
plan 节点读取 PCBState
-> 根据 task_type / workflow_id / user_input 选择 skill
-> 注入 skill prompt、允许工具、流程规则、示例
-> PCBPlanner 生成 tool_calls 或 response
-> execute_tools 执行工具
```

### 3.3 未来 Skill 目录结构

未来可新增：

```text
skills/
  fanout_skill/
    skill.json
    prompt.md
    examples.md
  reroute_skill/
    skill.json
    prompt.md
    examples.md
  drc_explain_skill/
    skill.json
    prompt.md
    examples.md
```

每个 `skill.json` 建议包含：

```json
{
  "name": "fanout_skill",
  "description": "Global fanout workflow skill for BGA escape routing.",
  "intents": ["global_fanout"],
  "workflows": ["pcb_escape_flow"],
  "allowed_tools": [
    "getProjectData",
    "pcb_extra_bga",
    "layer_assign",
    "escape_order",
    "fanout_route",
    "importLines"
  ],
  "entry_conditions": [
    "user mentions fanout, escape routing, BGA, or 扇出/逃逸"
  ]
}
```

### 3.4 推荐优先实现的 Skill

第一批建议实现三个：

| Skill | 目标 |
|---|---|
| `fanout_skill` | 封装 Global Fanout 的工具链、参数确认规则、导入确认规则 |
| `reroute_skill` | 封装拆线重布、DRC 反馈循环 |
| `drc_explain_skill` | 封装 DRC 检查、可解释性报告和问答说明 |

这样可以把 `planner.py` 中一部分业务知识外移，后续新增 PCB 能力时不必频繁改主图。

## 4. Fanout/Reroute 采集标志位

### 4.1 当前后端已有可读字段

后端 `PCBState` 中已有可用于判断流程状态的字段：

- `task_type`
- `workflow_id`
- `workflow_state`
- `current_stage`
- `tool_history`
- `report_payload`
- `intermediate_cache`

这些字段在 Agent 内部可读，但当前 WebSocket 最终消息没有完整透出。

当前 WebSocket 主要透出：

- `content`
- `markdownReport`
- `reportPayload`
- `selection`
- `fanoutParams`
- `routingResult`
- `importLinesFilePath`
- `workDir`

因此，如果同事要做稳定的数据采集，建议后续新增标准化 telemetry 字段，而不是只靠文本或报告内容推断。

### 4.2 Fanout 开始和结束判断

Fanout 开始标志：

```text
task_type == "global_fanout"
workflow_id == "pcb_escape_flow"
```

Fanout 过程状态：

```text
workflow_state == "select_bga"
workflow_state == "wait_router_type"
workflow_state == "param_review"
workflow_state == "review"
```

Fanout 等待导入：

```text
workflow_state == "review"
```

或：

```text
report_payload.stage == "import_pending"
```

Fanout 成功结束标志：

```text
workflow_state == "result_review"
intermediate_cache.importLinesResult exists
```

Fanout 失败标志：

```text
workflow_state == "error"
```

### 4.3 Reroute 开始和结束判断

Reroute 开始标志：

```text
task_type == "reroute"
workflow_id == "pcb_reroute_flow"
```

Reroute 过程状态：

```text
workflow_state == "confirm"
workflow_state == "report"
```

Reroute DRC 阶段完成：

```text
intermediate_cache.drcResult exists
```

DRC 是否通过：

```text
report_payload.drcPassed == true
```

Reroute 成功结束标志：

```text
workflow_state == "import"
intermediate_cache.importLinesResult exists
```

Reroute 失败标志：

```text
workflow_state == "error"
```

### 4.4 推荐新增 workflowTelemetry

后续可以在 WebSocket 最终 `message.body` 中新增：

```json
{
  "workflowTelemetry": {
    "taskType": "global_fanout",
    "workflowId": "pcb_escape_flow",
    "workflowState": "result_review",
    "currentStage": "finished",
    "started": true,
    "completed": true,
    "failed": false,
    "toolOrder": [
      "getProjectData",
      "pcb_extra_bga",
      "layer_assign",
      "escape_order",
      "fanout_route",
      "importLines"
    ]
  }
}
```

建议字段含义：

| 字段 | 含义 |
|---|---|
| `taskType` | 当前任务类型 |
| `workflowId` | 当前业务流 |
| `workflowState` | 当前业务状态 |
| `currentStage` | 当前 LangGraph 节点阶段 |
| `started` | 是否已经进入 fanout/reroute 流程 |
| `completed` | 是否成功完成并导入或生成最终结果 |
| `failed` | 是否进入错误状态 |
| `toolOrder` | 本轮/本流程工具调用顺序 |

## 5. 推荐技术演进路线

推荐按以下顺序推进：

1. 标准化 telemetry 输出。
2. 接入 LangGraph checkpointer。
3. 增加长期 memory store。
4. 实现 skill registry。
5. 在 `plan` 节点注入 memory 和 skill。
6. 将部分硬编码 planner 规则迁移到 skill 配置。

优先级建议：

| 优先级 | 能力 | 原因 |
|---|---|---|
| P0 | `workflowTelemetry` | 同事数据采集马上可用，改动小 |
| P1 | checkpointer | 解决服务重启后流程丢失 |
| P2 | long-term memory store | 让项目和用户经验可复用 |
| P3 | skill registry | 降低 Planner 硬编码，支持能力扩展 |
| P4 | memory + skill 联合注入 | 提升规划质量和可迁移性 |

## 6. 后续开发的落地建议

### 6.1 数据采集团队

短期建议：

- 如果不改代码，只能从工具调用消息、最终 `reportPayload` 和文本结果间接判断流程。
- 更稳定的方式是新增 `workflowTelemetry`。
- 采集时应同时记录 `sessionId`、`projectId`、`msgId` 和 `toolOrder`。

推荐采集事件：

- `workflow_started`
- `workflow_waiting_user`
- `tool_called`
- `tool_finished`
- `workflow_completed`
- `workflow_failed`

### 6.2 Agent 开发团队

短期建议：

- 保留当前 `PCBState` 和 `intermediate_cache` 设计。
- 不要把长期 memory 直接塞进所有 prompt。
- 在 `plan` 节点集中做 memory 检索和 skill 注入。

中期建议：

- 把 `planner.py` 中稳定的 fanout/reroute 规则逐步沉淀为 skill 配置。
- 保留规则兜底，避免模型输出异常导致流程不可控。
- 给每个 skill 配置 allowed tools，减少模型误调用工具。

长期建议：

- 建立项目级 memory，例如同一 PCB 项目常用 BGA、布线约束、历史 DRC 问题。
- 建立经验级 memory，例如 DRC 失败类型和修复策略。
- 结合 evaluation/replay 数据，把成功案例沉淀为 skill examples 或 memory recipes。

## 7. 总结

当前项目已经具备短期会话状态和流程缓存，适合支撑 fanout/reroute 这类多步骤 PCB 工作流。

后续增强建议分三条线：

- 记忆线：从 `_session_states` 和 `intermediate_cache` 升级到 checkpointer + long-term store。
- Skill 线：从固定工具注册和硬编码规则升级到 skill registry + plan 节点动态注入。
- 采集线：从间接判断升级到标准化 `workflowTelemetry`。

这三条线互相配合后，项目会从“有状态工作流智能体”进一步演进为“可恢复、可学习、可扩展、可观测的 PCB 工程智能体框架”。
