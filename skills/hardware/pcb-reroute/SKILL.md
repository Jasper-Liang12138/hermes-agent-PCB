---
name: pcb-reroute
version: 1.0.0
description: PCB local rip-up and reroute flow driven by net names from user text
prerequisites:
  commands: []
  python_packages: []
metadata:
  hermes:
    tags: [PCB, reroute, rip-up, 拆线重布, 局部重布, EDA]
    category: hardware
---

# PCB Reroute Skill - 局部拆线重布

## 目标

本技能用于局部拆线重布场景。用户在自然语言中明确给出需要处理的 net 名称，智能体从文本中提取这些 net，调用 EDA 侧 MOCK 拆线工具，再生成局部重布结果包返回给 EDA。

本流程不调用 `GetSelectedElements`，不读取框选对象，只依赖用户文本中的 net 名称。

## 触发条件

当用户明确要求对指定 net 执行拆线重布、删除后重走、重新布线、reroute 时触发，例如：

- `请帮我针对 BGA U2 中的 net13、net17 拆线后重新布线`
- `把 net_A1 和 net_B2 删除后重走`
- `reroute net13`

概念咨询、原理解释、只讨论方案时不要调用工具。

## 工具链路

| 工具名 | 功能 |
|--------|------|
| `drop_net` | 从用户文本中提取 net 名称，并通过 WebSocket 请求 EDA 执行 `drop_net_mock` 拆线；支持客户端返回 `droppedBoardData` 或 `droppedBoardDataFilePath` |
| `reroute` | 基于 `drop_net` 缓存的拆线后上下文生成局部重布结果包与轻量检查报告；有拆线后版图文本时复用 PCB 分块模块构造长上下文 |

## 工作流程

1. 判断用户是否明确要求局部拆线重布。
2. 调用 `drop_net(userText, projectID)`。
   - `userText` 必须传用户原始请求。
   - `projectID` 从用户消息里的 `[projectid: ...]` 获取；没有也可以传空字符串。
   - 如果客户端返回 `droppedBoardDataFilePath`，工具会读取该文件内容作为拆线后版图数据。
3. 如果 `drop_net` 返回 `error` 或 `selectedNets` 为空，提示用户明确写出 net 名称。
4. 调用 `reroute()`，优先使用 `drop_net` 的 session 缓存。`reroute` 会在可用时对拆线后版图进行分块并调用模型生成结果；不可用时回退到结构化结果包。
5. 将 `rerouteResult`、`checkReport`、`explanation` 放入 `##PCB_FIELDS##` 返回。

## 输出格式

```
已完成局部拆线重布结果生成。

##PCB_FIELDS##
{
  "rerouteResult": {
    "type": "local_reroute",
    "mode": "selected_nets_after_drop",
    "selectedNets": ["net13", "net17"],
    "operations": []
  },
  "checkReport": {
    "passed": true,
    "checks": []
  },
  "explanation": "已基于用户文本中的 net 名称和 EDA 拆线结果生成局部重布结果包。"
}
##PCB_FIELDS_END##
```

## 约束

- 不要调用 `GetSelectedElements`。
- 不要调用 `getProjectData` 作为主流程入口；拆线后的版图数据由 `drop_net` 返回。
- 不要调用全局 BGA fanout 的 `route` 工具。
- `##PCB_FIELDS##` 内必须是合法 JSON。
