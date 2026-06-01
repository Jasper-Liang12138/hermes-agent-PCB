# PCB Agent EXE 前端接入说明

本文档面向前端/EDA 客户端开发同事，说明如何启动交付版 `agent.exe`，以及前端需要按什么 WebSocket 协议和 Agent 交互。

## 1. 启动方式

交付目录示例：

```text
PCB-AGENT/
├─ agent.exe
├─ start.bat
├─ install.bat
├─ config.ini
├─ routers/
├─ router_work/
├─ skills/
├─ memories/
├─ logs/
└─ _internal/
```

首次使用：

```text
1. 双击 install.bat
2. 编辑 config.ini，填写模型配置和 WebSocket 端口
3. 双击 start.bat
```

日常启动：

```text
双击 start.bat
```

`start.bat` 会先把交付目录外层的 `config.ini` 同步到 `_internal/config.ini`，再启动 `agent.exe`。因此修改配置后需要重启 `start.bat` 才生效。

## 2. WebSocket 地址

前端连接地址由 `config.ini` 决定：

```ini
[server]
host = 0.0.0.0
port = 7073
```

本机联调通常连接：

```text
ws://127.0.0.1:7073
```

如果现场 `config.ini` 里端口不是 `7073`，以前端和 Agent 约定的实际端口为准。端口改完后必须重启 `start.bat`。

## 3. 前端发送用户消息

前端向 Agent 发送普通用户消息：

```json
{
  "sessionId": "ws_001",
  "projectid": "402Pin_08BGA_8L_S_01141700",
  "type": "message",
  "body": {
    "role": "user",
    "content": "帮我对 U22 做 BGA 逃逸布线"
  }
}
```

字段约定：

| 字段 | 说明 |
| --- | --- |
| `sessionId` | 当前对话 ID。建议前端生成并在同一轮链路中保持不变。 |
| `projectid` | 当前版图工程 UUID。Agent 会原样透传并用于日志定位。 |
| `type` | 用户消息固定为 `message`。 |
| `body.role` | 用户消息固定为 `user`。 |
| `body.content` | 用户输入文本。 |

如果前端不传 `sessionId`，Agent 会按当前 WebSocket 连接生成临时 session，但不建议这样做，因为断线重连后不利于恢复链路状态。

## 4. Agent 返回普通消息

Agent 返回给前端的普通消息：

```json
{
  "sessionId": "ws_001",
  "projectid": "402Pin_08BGA_8L_S_01141700",
  "type": "message",
  "body": {
    "msgId": "9f4a1c2d3e4f",
    "role": "agent",
    "content": "已完成逃逸参数配置，请确认",
    "isFinal": true,
    "fanoutParams": "{\"selectedBGA\":\"U22\",\"routerType\":\"rl_arc\",\"orderLines\":[],\"constraints\":{}}"
  }
}
```

字段约定：

| 字段 | 说明 |
| --- | --- |
| `body.msgId` | Agent 生成的消息 ID。 |
| `body.role` | Agent 消息固定为 `agent`。 |
| `body.content` | 前端展示给用户的文本。 |
| `body.isFinal` | `false` 表示中间帧，`true` 表示这一轮最终帧。 |
| `body.selection` | 可选，BGA 候选列表。 |
| `body.fanoutParams` | 可选，逃逸参数 JSON 字符串。注意这里是字符串，不是对象。 |
| `body.routingResult` | 可选，BGA 布线结果文件路径。 |
| `body.importLinesFilePath` | 可选，实际建议导入的布线器原始结果文件路径。 |
| `body.rerouteResult` | 可选，拆线重布链路结果。 |
| `body.routedLayoutTxtFilePath` | 可选，拆线重布输出 txt 文件路径。 |
| `body.report` | 可选，布线或导入报告。 |
| `body.thinking` | 可选，模型思考内容；是否出现取决于配置和模型能力。 |

前端应以 `body.isFinal` 判断一轮消息是否结束。流式输出时可能收到多帧 `isFinal=false`，最后一帧为 `isFinal=true`。

## 5. Agent 请求前端工具调用

Agent 需要前端执行 EDA 侧动作时，会发送 `tool-calls`：

```json
{
  "sessionId": "ws_001",
  "projectid": "402Pin_08BGA_8L_S_01141700",
  "type": "tool-calls",
  "body": {
    "role": "agent",
    "content": {
      "id": "call_abc123",
      "name": "getProjectData",
      "arguments": {
        "projectID": "402Pin_08BGA_8L_S_01141700"
      }
    }
  }
}
```

前端收到后按 `body.content.name` 执行对应工具，并用同一个 `id` 回传 `tool-results`。

## 6. 前端回传工具结果

前端执行完工具后返回：

```json
{
  "sessionId": "ws_001",
  "projectid": "402Pin_08BGA_8L_S_01141700",
  "type": "tool-results",
  "body": {
    "role": "tool",
    "content": {
      "id": "call_abc123",
      "result": "F:\\PCB_QYF\\board_data\\project_001.sexpr"
    }
  }
}
```

关键点：

- `body.content.id` 必须等于收到的 tool-call id。
- `result` 可以是字符串、JSON 字符串或对象，具体取决于工具。
- 当前 `config.ini` 中 `board_data_use_file_path = 1` 时，`getProjectData` 的 `result` 应返回版图数据文件路径；Agent 会自动读取文件内容。
- 如果 `board_data_use_file_path = 0`，`getProjectData` 的 `result` 应直接返回版图数据字符串。

## 7. 前端需要支持的工具

### getProjectData

用途：Agent 获取当前版图工程数据。

前端返回：

```json
{
  "type": "tool-results",
  "body": {
    "role": "tool",
    "content": {
      "id": "call_xxx",
      "result": "F:\\...\\project_data.sexpr"
    }
  }
}
```

如果 `board_data_use_file_path = 1`，建议返回文件路径，避免大版图数据直接塞进 WebSocket。

### getSelectedElements

用途：拆线重布链路中，Agent 获取用户当前框选的走线或元素。

前端返回内容应包含选中 traces/nets 的信息。若没有选中对象，应返回空结果，Agent 会提示用户先框选。

### deleteTracesById

用途：拆线重布链路中，Agent 请求前端删除指定 trace。

前端执行删除后返回成功/失败信息即可。

### importLines

用途：BGA 布线或拆线重布完成后，Agent 请求前端导入布线器生成的线文件。

Agent 发给前端的格式：

```json
{
  "type": "tool-calls",
  "body": {
    "role": "agent",
    "content": {
      "id": "import_lines_12345678",
      "name": "importLines",
      "arguments": {
        "filePath": "F:\\PCB_QYF\\PCB_Builder\\cust_tools\\PCBCopilot_dev\\PCB-AGENT\\router_work\\line.out",
        "successPins": [],
        "failedPins": []
      }
    }
  }
}
```

前端需要读取 `arguments.filePath` 并导入。导入完成后返回：

```json
{
  "type": "tool-results",
  "body": {
    "role": "tool",
    "content": {
      "id": "import_lines_12345678",
      "result": {
        "success": true,
        "message": "importLines finished"
      }
    }
  }
}
```

## 8. BGA fanout 链路

用户示例：

```text
帮我对 U22 做 BGA 逃逸布线
```

预期链路：

```text
用户请求
  -> Agent 发送 tool-calls:getProjectData
  -> 前端返回 tool-results:getProjectData
  -> Agent 返回 selection 或直接进入参数配置
  -> 用户选择 BGA，例如：选择 U22
  -> Agent 要求选择走线算法和层分配模块
  -> 用户回复：135 + RL / arc + RL / 135 + 北科大 / arc + 北科大
  -> Agent 返回 fanoutParams，content 为“已完成逃逸参数配置，请确认”
  -> 用户回复：确认
  -> Agent 本地调用布线器
  -> Agent 返回 routingResult/importLinesFilePath/report
  -> Agent 自动发送 tool-calls:importLines
  -> 前端导入 filePath 并回传 tool-results
```

注意：

- 前端不会收到 `name=route` 的 tool-call；`route` 是 Agent 本地动作。
- `fanoutParams` 是发给前端唤起逃逸参数配置界面的结构化字段。
- `fanoutParams` 字段是 JSON 字符串，前端需要 `JSON.parse()` 后渲染。
- 135 系布线器导入文件通常是 `line.out`。
- arc 系布线器导入文件通常是 `ARC_output.txt`。
- 前端导入时以 `importLines` 的 `arguments.filePath` 为准。

## 9. 拆线重布链路

用户示例：

```text
把我框选的线删除后重新布线
```

预期链路：

```text
用户请求
  -> Agent 发送 tool-calls:getSelectedElements
  -> 前端返回当前框选 traces/nets
  -> Agent 发送 tool-calls:deleteTracesById
  -> 前端删除对应 traces 并返回结果
  -> Agent 根据上下文执行 reroute
  -> Agent 返回 rerouteResult/routedLayoutTxtFilePath/checkReport/report
  -> 如满足导入条件，Agent 发送 tool-calls:importLines
```

注意：

- 删除目标必须来自前端选中对象，Agent 不应只根据文本臆造 trace id。
- 如果用户没有框选对象，前端应返回空选择，Agent 会提示用户先框选。

## 10. 意图识别边界

当前第一跳意图识别在 WebSocket adapter 中通过硬规则完成：

| 用户输入 | 预期 |
| --- | --- |
| `BGA 和 QFP 有什么区别？` | 普通聊天，不发工具调用 |
| `不要布线，只解释一下逃逸布线原理` | 普通聊天，不发工具调用 |
| `帮我做 BGA 逃逸布线` | 进入 BGA fanout 链路 |
| `U22 用 135，层分配用 RL` | 进入/继续 BGA fanout 配置 |
| `确认`，且已有 fanoutParams | 执行本地 route |
| `把我框选的线删除后重新布线` | 进入拆线重布链路 |
| `#全局fanout` 或 `#布线` | 强制进入 BGA fanout 链路 |
| `#拆线重布` 或 `#reroute` | 强制进入拆线重布链路 |
| `取消` | 退出当前 PCB 流程 |

前端侧不需要自己判断这些业务意图，只需要展示消息、执行 tool-call、回传 tool-results。

流程中途的特殊行为：

- 临时聊天：用户在 BGA fanout 或拆线重布流程中问“解释一下 RL 是什么意思”这类问题时，Agent 会临时按 chat 回复，并保留原 PCB 流程状态。
- 取消流程：用户明确说“取消 / 退出 / 中止 / 停止”时，Agent 会退出当前 PCB 流程。
- 切换任务：用户在 BGA fanout 中途说“#拆线重布”或明确要求拆线重布时，Agent 会清掉当前 BGA 流程并进入 reroute 链路。

拆线重布不要求用户已经提前框选。前端可以先让用户发送“#拆线重布”或“拆线重布”，Agent 进入 reroute 链路后会通过 `getSelectedElements` 获取当前框选；如果没有框选，Agent 会提示用户先框选需要拆线重布的走线。

## 11. 日志位置

交付目录启动时，WebSocket 关键日志会写到：

```text
PCB-AGENT/logs/pcb_websocket_full.jsonl
PCB-AGENT/logs/pcb_websocket_trace.jsonl
PCB-AGENT/logs/pcb_websocket_tool_results.jsonl
PCB-AGENT/logs/pcb_captures/
```

说明：

- `pcb_websocket_full.jsonl`：完整前后端 WebSocket 收发消息。
- `pcb_websocket_trace.jsonl`：压缩版 outbound 结构化摘要。
- `pcb_websocket_tool_results.jsonl`：前端回传工具结果记录。
- `pcb_captures/`：大版图数据或工具结果的完整落盘文件。

Hermes 运行日志还可能写到：

```text
%USERPROFILE%\.hermes\logs\
```

排查前端是否收到 `fanoutParams`、是否发出 `importLines`，优先看 `pcb_websocket_full.jsonl`。

## 12. 常见问题

### 连接不上 WebSocket

检查：

```text
1. start.bat 是否已启动，Gateway 窗口是否仍在运行
2. config.ini 的 [server].port 是否和前端连接端口一致
3. 端口是否被占用
4. 修改 config.ini 后是否重启了 start.bat
```

### 前端没唤起逃逸参数配置界面

检查最后一条 Agent message 是否包含：

```json
{
  "type": "message",
  "body": {
    "role": "agent",
    "content": "已完成逃逸参数配置，请确认",
    "fanoutParams": "{\"selectedBGA\":\"U22\",\"routerType\":\"rl_arc\"}"
  }
}
```

如果 `fanoutParams` 不在 `body` 顶层，而只出现在 `content` 文本里，前端不应当唤起配置界面，应看 Agent 结构化字段提取是否异常。

### 收到 routingResult 但版图没有变化

优先检查：

```text
1. 是否收到了 tool-calls:importLines
2. importLines.arguments.filePath 是否存在
3. 前端是否对该 filePath 执行了导入
4. 前端是否回传了对应 id 的 tool-results
5. logs/pcb_websocket_full.jsonl 中 importLines 的收发是否完整
```

### getProjectData 返回很大导致卡顿

建议配置：

```ini
[model]
board_data_use_file_path = 1
```

前端返回版图数据文件路径，让 Agent 本地读取文件内容。

### 修改配置不生效

`config.ini` 修改后必须重启 `start.bat`。启动时 `sync_config.ps1` 会把外层 `config.ini` 同步到 `_internal/config.ini`。
