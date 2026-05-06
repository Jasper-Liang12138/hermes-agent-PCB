---
name: pcb-reroute
version: 1.1.0
description: PCB local rip-up and reroute flow driven by net names from user text, with KiCad fill-in and DRC validation
prerequisites:
  commands: []
  python_packages: []
metadata:
  hermes:
    tags: [PCB, reroute, rip-up, 拆线重布, 局部重布, DRC, EDA]
    category: hardware
---

# PCB Reroute Skill - 局部拆线重布

## 目标

本技能用于局部拆线重布场景。用户在自然语言中明确给出需要处理的 net 名称，智能体从文本中提取这些 net，调用 EDA 侧 MOCK 拆线工具，再生成局部重布结果。

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
| `reroute` | 基于 `drop_net` 缓存的拆线后上下文生成局部重布结果；模型生成 KiCad patch 后会回填到原始版图副本并进行 hard DRC 校验 |

## 工作流程

1. 判断用户是否明确要求局部拆线重布。
2. 调用 `drop_net(userText, projectID)`。
   - `userText` 必须传用户原始请求。
   - `projectID` 从用户消息里的 `[projectid: ...]` 获取；没有也可以传空字符串。
   - 如果客户端返回 `droppedBoardDataFilePath`，工具会读取该文件内容作为拆线后版图数据。
   - 如果客户端返回 `originalBoardDataFilePath`，工具会把它作为拆线前原始版图地址；否则从 `localContext.boardDataFilePath` 或用户文本中的 `.kicad_pcb` 路径兜底提取。
3. 如果 `drop_net` 返回 `error` 或 `selectedNets` 为空，提示用户明确写出 net 名称。
4. 调用 `reroute()`，优先使用 `drop_net` 的 session 缓存。
5. `reroute` 会要求模型输出：
   - `rerouteResult`
   - `kicadPatch`
   - `checkReport`
   - `explanation`
6. `reroute` 将 `kicadPatch` 回填到原始版图副本，调用纯 DRC hard check。
7. 如果 DRC 通过，返回 `routedBoardDataFilePath`。
8. 如果 DRC 不通过，将失败摘要追加进下一轮 prompt，最多迭代 `maxDrcIterations` 轮，默认 5 轮。
9. 迭代耗尽仍失败时，返回拆线前原始文件地址作为 `routedBoardDataFilePath`，并说明失败原因。
10. 将 `rerouteResult`、`routedBoardDataFilePath`、`checkReport`、`explanation` 放入 `##PCB_FIELDS##` 返回。

## 输出格式

```
已完成局部拆线重布结果生成。

##PCB_FIELDS##
{
  "rerouteResult": {
    "type": "local_reroute",
    "mode": "selected_nets_after_drop",
    "selectedNets": ["net13", "net17"],
    "operations": [],
    "drcPassed": true,
    "drcIterations": 1,
    "routedBoardDataFilePath": "/path/to/.hermes_reroute/session_net13_iter1.kicad_pcb"
  },
  "routedBoardDataFilePath": "/path/to/.hermes_reroute/session_net13_iter1.kicad_pcb",
  "checkReport": {
    "passed": true,
    "checks": []
  },
  "explanation": "已生成局部重布结果并通过 DRC。"
}
##PCB_FIELDS_END##
```

## 约束

- 不要调用 `GetSelectedElements`。
- 不要调用 `getProjectData` 作为主流程入口；拆线后的版图数据由 `drop_net` 返回。
- 不要调用全局 BGA fanout 的 `route` 工具。
- `##PCB_FIELDS##` 内必须是合法 JSON。
- 当前项目内统一按 KiCad `.kicad_pcb` 文本处理；EDA 与 LLM 层之间的其他格式转换由外部仓库负责。
