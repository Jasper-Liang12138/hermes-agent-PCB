# PCB Agent 双链路接口指南

更新时间：2026-05-13

本文面向前端、PCB Builder 和联调人员，说明当前 PCB Agent 的 WebSocket 协议和两条业务链路：

1. BGA 扇出布线链路：`getProjectData` → `selection` → `fanoutParams` → 本地 `route` → `routingResult`
2. 选中走线拆线重布链路：`getSelectedElements` → `deleteTracesById` → `getProjectData` → `reroute` → `rerouteResult`

当前实现基于“前端工具调用 + Agent 结构化字段下发”的方式。BGA 扇出布线的最终 `routingResult` 是输出文件路径，不是 S-expression 正文。

## 统一 WebSocket 消息 envelope

用户消息：

```json
{
  "sessionId": "ws_xxx",
  "projectid": "project-001",
  "type": "message",
  "body": {
    "role": "user",
    "content": "帮我做 BGA 逃逸布线"
  }
}
```

Agent 普通回复或结构化回复：

```json
{
  "sessionId": "ws_xxx",
  "projectid": "project-001",
  "type": "message",
  "body": {
    "role": "agent",
    "msgId": "msg_xxx",
    "content": "可见文本",
    "thinking": "",
    "isFinal": true
  }
}
```

Agent 调前端工具：

```json
{
  "sessionId": "ws_xxx",
  "projectid": "project-001",
  "type": "tool-calls",
  "body": {
    "role": "agent",
    "content": {
      "id": "call_xxx",
      "name": "getProjectData",
      "arguments": {}
    }
  }
}
```

前端返回工具结果：

```json
{
  "sessionId": "ws_xxx",
  "projectid": "project-001",
  "type": "tool-results",
  "body": {
    "role": "tool",
    "content": {
      "id": "call_xxx",
      "result": "..."
    }
  }
}
```

结构化字段会被 Agent 放在 `body` 顶层，而不是只放在 `content` 字符串里。前端应优先读取这些字段：

```json
{
  "type": "message",
  "body": {
    "role": "agent",
    "content": "已生成扇出参数，请确认。",
    "isFinal": true,
    "fanoutParams": {...}
  }
}
```

当前可能出现的 PCB 结构化字段：

| 字段 | 类型 | 链路 | 含义 |
| --- | --- | --- | --- |
| `selection` | array | BGA 扇出 | 可选 BGA 列表 |
| `boardSummary` | object/string | BGA 扇出 | 板级摘要 |
| `fanoutContext` | object/string | BGA 扇出 | 生成扇出参数的上下文 |
| `fanoutParams` | object | BGA 扇出 | 待用户确认的扇出参数 |
| `routingResult` | string | BGA 扇出 | `routing_input.txt` 绝对路径 |
| `rerouteResult` | object | 拆线重布 | 局部重布结构化结果 |
| `routedLayoutTxtFilePath` | string | 拆线重布 | DRC 通过后可导入 EDA 的 txt/S 表达式结果文件路径 |
| `checkReport` | object | 拆线重布 | 检查/DRC 报告 |
| `explanation` | string | 拆线重布 | 给用户看的结果说明 |

## 链路一：BGA 扇出布线接口

### 触发条件

用户明确要求 PCB/BGA/逃逸/扇出/布线等执行动作时进入本链路。例如：

```text
帮我做 BGA 逃逸布线
对 U22 做扇出
使用 135 给 U22 布线
```

如果用户只是问概念，例如“BGA 和 QFP 有什么区别”，不进入 PCB 工具链。

### 步骤 1：Agent 调用 getProjectData

Agent 发出：

```json
{
  "type": "tool-calls",
  "body": {
    "content": {
      "id": "call_get_project",
      "name": "getProjectData",
      "arguments": {
        "projectID": "project-001"
      }
    }
  }
}
```

前端返回：

```json
{
  "type": "tool-results",
  "body": {
    "content": {
      "id": "call_get_project",
      "result": "F:\\path\\to\\board_data.txt"
    }
  }
}
```

`result` 可以是版图 S-expression 字符串，也可以是文件路径。若 `board_data_use_file_path=1`，Agent 会把文件路径读取为版图内容。

### 步骤 2：Agent 返回 selection

如果识别到多个 BGA，Agent 返回：

```json
{
  "type": "message",
  "body": {
    "role": "agent",
    "content": "请选择一个 BGA 进行布线。",
    "isFinal": true,
    "selection": [
      {"label": "U22", "detail": "BGA, 402 pins"},
      {"label": "U27", "detail": "BGA, 608 pins"}
    ]
  }
}
```

前端需要把 `selection` 渲染为可选择项，用户选择后发送普通用户消息，例如：

```json
{
  "type": "message",
  "body": {
    "role": "user",
    "content": "选择 U22"
  }
}
```

### 步骤 3：用户选择 routerType

Agent 会要求用户选择：

```text
arc
135
```

前端不需要单独工具接口，只需要把用户文本发给 Agent。

说明：

- `arc`：圆弧走线。
- `135`：135 度折角走线。
- 只有这两个布线器。
- 未选择 `routerType` 时，Agent 不应输出 `fanoutParams`。

### 步骤 4：Agent 返回 fanoutParams

```json
{
  "type": "message",
  "body": {
    "role": "agent",
    "content": "已生成扇出参数，请确认。",
    "isFinal": true,
    "fanoutParams": {
      "selectedBGA": "U22",
      "routerType": "135",
      "orderLines": [
        {"net": "GND_SIGNAL", "layer": "Top", "order": 1},
        {"net": "PWR_LEDEN", "layer": "Top", "order": 2},
        {"net": "SUCLK", "layer": "Art03", "order": 3}
      ],
      "constraints": {
        "LineWidth": 4,
        "LineSpacing": 3
      }
    }
  }
}
```

前端可展示参数供用户确认或修改。用户回复“确认”后，Agent 会直接调用本地 `route` 工具，不再向前端发 `route` 工具调用。

### 步骤 5：Agent 本地调用 route

这是 Agent 内部动作，前端不会收到 `name=route` 的 `tool-calls`。

`route` 的输入核心字段：

```json
{
  "selectedBGA": "U22",
  "routerType": "135",
  "orderLines": [
    {"net": "GND_SIGNAL", "layer": "Top", "order": 1}
  ],
  "constraints": {
    "LineWidth": 4,
    "LineSpacing": 3
  }
}
```

Agent adapter 会按布线器 README 要求生成工作目录文件，并调用：

- `routerType="arc"`：`routers\arc`
- `routerType="135"`：`routers\135`

两个布线器输出都会被归一化为：

```text
router_work\routing_input.txt
```

### 步骤 6：Agent 返回 routingResult

```json
{
  "type": "message",
  "body": {
    "role": "agent",
    "content": "布线完成（无详细报告）。完整布线数据已通过结构化字段发送给前端。",
    "isFinal": true,
    "routingResult": "F:\\PCB_QYF\\PCB_Builder\\cust_tools\\PCBCopilot_dev\\PCB-AGENT\\router_work\\routing_input.txt"
  }
}
```

前端处理要求：

1. 读取 `body.routingResult`。
2. 把它当作文件路径导入 PCB Builder。
3. 不要把它当作 S-expression 字符串。
4. 如果 PCB Builder busy，应等待可导入状态后再导入，或给出失败提示。

## 链路二：选中走线拆线重布接口

### 触发条件

用户明确要求局部拆线重布、选中走线删除后重走、reroute selected traces 等。例如：

```text
拆线重布
把我框选的走线拆掉后重布
reroute selected traces
```

当前 PCB 会话中，即使用户只说“拆线重布”，也会进入本链路，并打断旧的 BGA fanout 确认状态。

### 步骤 0：前端选择要求

用户必须先在 PCB 前端框选走线。Agent 不从自然语言里推断要删除的对象 ID。

选择对象类型必须是 traces。当前建议前端实现：

```json
{
  "PFindType": "TRACES"
}
```

### 步骤 1：Agent 调用 getSelectedElements

```json
{
  "type": "tool-calls",
  "body": {
    "content": {
      "id": "call_get_selected",
      "name": "getSelectedElements",
      "arguments": {
        "PFindType": "TRACES",
        "projectID": "project-001"
      }
    }
  }
}
```

前端返回：

```json
{
  "type": "tool-results",
  "body": {
    "content": {
      "id": "call_get_selected",
      "result": ["2386476278", "3424247826"]
    }
  }
}
```

兼容返回字符串形式：

```json
"[\"2386476278\", \"3424247826\"]"
```

约束：

- 0 条：Agent 停止并提示用户先框选走线。
- 1 到 40 条：继续。
- 超过 40 条：Agent 停止并提示缩小选择范围。

### 步骤 2：Agent 调用 deleteTracesById

```json
{
  "type": "tool-calls",
  "body": {
    "content": {
      "id": "call_delete",
      "name": "deleteTracesById",
      "arguments": {
        "ids": ["2386476278", "3424247826"],
        "projectID": "project-001"
      }
    }
  }
}
```

前端返回：

```json
{
  "type": "tool-results",
  "body": {
    "content": {
      "id": "call_delete",
      "result": {
        "success": true,
        "deleted": ["2386476278", "3424247826"]
      }
    }
  }
}
```

如果删除失败，Agent 不会继续调用 `reroute`。

### 步骤 3：Agent 调用 getProjectData

删除成功后，Agent 再次调用：

```json
{
  "type": "tool-calls",
  "body": {
    "content": {
      "id": "call_get_project_after_delete",
      "name": "getProjectData",
      "arguments": {
        "projectID": "project-001"
      }
    }
  }
}
```

前端应返回删除后的新版图数据或文件路径。Agent 会把该数据缓存为 reroute 上下文。

### 步骤 4：Agent 本地调用 reroute

这是 Agent 内部工具，不是前端工具。`reroute` 会读取前面缓存的：

- `selectedTraceIds`
- `droppedBoardData`
- `droppedObjects`
- `localContext`

然后生成局部重布结果、检查报告和可选的新板文件。

### 步骤 5：Agent 返回 rerouteResult

```json
{
  "type": "message",
  "body": {
    "role": "agent",
    "content": "局部拆线重布已完成。",
    "isFinal": true,
    "rerouteResult": {
      "type": "local_reroute",
      "mode": "selected_traces_after_delete",
      "selectedTraceIds": ["2386476278", "3424247826"],
      "operations": [
        {
          "action": "reroute_selected_traces",
          "scope": "local",
          "preserveOtherNets": true
        }
      ],
      "drcPassed": true,
      "drcIterations": 1,
      "routedLayoutTxtFilePath": "F:\\project\\.hermes_reroute\\txt\\session_iter1.txt"
    },
    "routedLayoutTxtFilePath": "F:\\project\\.hermes_reroute\\txt\\session_iter1.txt",
    "checkReport": {
      "passed": true,
      "checks": []
    },
    "explanation": "已基于选中走线完成局部重布并通过 DRC。"
  }
}
```

前端处理要求：

1. 优先读取 `body.routedLayoutTxtFilePath`。
2. 如果该字段存在，调用 `importLines` 或按文件路径导入/回填新版图。
3. 如果没有该字段，读取 `body.rerouteResult` 中的 `operations` 或 `kicadPatch`，根据前端能力处理。
4. 展示 `checkReport` 和 `explanation` 给用户。

## 两条链路的关键差异

| 项目 | BGA 扇出布线 | 选中走线拆线重布 |
| --- | --- | --- |
| 意图 | 全局/目标 BGA 逃逸扇出 | 局部选中走线删除后重走 |
| Skill | `hardware/pcb-intelligence` | `hardware/pcb-reroute` |
| 用户前置动作 | 不需要框选走线 | 必须先框选 traces |
| 前端工具 | `getProjectData` | `getSelectedElements`、`deleteTracesById`、`getProjectData` |
| 本地工具 | `route` | `reroute` |
| 是否选择 `arc`/`135` | 是 | 否 |
| 最终字段 | `routingResult` | `rerouteResult`、`routedLayoutTxtFilePath`、`checkReport`、`explanation` |
| 输出语义 | `routing_input.txt` 文件路径 | DRC 通过后的 txt/S 表达式导入文件路径 |

## isFinal 约定

前端应以 `body.isFinal` 判断一轮消息是否结束。

- `fanoutParams` 最终确认帧通常 `isFinal=true`。
- `routingResult` 最终结果帧必须 `isFinal=true`。
- `rerouteResult` 最终结果帧应为 `isFinal=true`。
- 流式中间帧可能 `isFinal=false` 或 `null`，但如果包含完整结构化字段，前端也可以先缓存字段，最终以最后一帧为准。

## trace 日志

delivery 包默认会写 PCB 结构化下发 trace：

```text
logs\pcb_websocket_trace.jsonl
```

典型 BGA 布线完成记录：

```json
{
  "direction": "outbound",
  "delivered": true,
  "fieldKeys": ["routingResult"],
  "isFinal": true,
  "routingResult": "F:\\...\\router_work\\routing_input.txt",
  "sessionId": "ws_xxx",
  "projectid": "project-001"
}
```

如果用户说“拆线重布”后 trace 中仍出现 `fanoutParams` 或 `routingResult`，说明意图识别或会话状态错误；正确链路应出现 `rerouteResult` 或对应前端工具调用。

## 前端联调检查清单

BGA 扇出布线：

- 能收到 `getProjectData` 工具调用。
- 能返回版图数据或文件路径。
- 能渲染 `selection`。
- 能发送用户选择的 BGA。
- 能发送用户选择的 `arc` 或 `135`。
- 能渲染 `fanoutParams` 并让用户确认。
- 能读取 `routingResult` 文件路径并导入。

拆线重布：

- 用户框选 traces 后，`getSelectedElements(PFindType="TRACES")` 返回正确 ID。
- `deleteTracesById(ids)` 能删除选中走线并返回成功。
- 删除后 `getProjectData` 返回新版图数据。
- 能读取 `rerouteResult`。
- 能读取并导入 `routedLayoutTxtFilePath`。
- 能展示 `checkReport` 和 `explanation`。

## 常见错误定位

### 收到 135 adapter 不可用

通常说明请求走到了 BGA 扇出链路，而不是拆线重布链路。检查用户话术和 trace：

- BGA 链路会出现 `fanoutParams`、`routingResult`。
- reroute 链路应出现 `getSelectedElements`、`deleteTracesById`、`rerouteResult`。

### routingResult 收到了但版图无变化

Agent 已经完成布线器调用并下发路径。继续检查：

- `routingResult` 文件是否存在。
- PCB Builder 是否执行导入。
- PCB Builder 是否 busy。
- 前端是否误把路径当成文件内容。

### reroute 没有删除走线

检查：

- 前端是否支持 `getSelectedElements(PFindType="TRACES")`。
- 返回 ID 是否为空。
- ID 是否超过 40 条。
- `deleteTracesById` 是否返回失败。
