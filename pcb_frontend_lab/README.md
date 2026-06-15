# PCB 前端虚拟实验框架

这是启云方 PCB Agent WebSocket 协议的黑盒虚拟前端测试工具。

这个实验框架 **不导入 Hermes/Agent 模块**，不调用私有函数，也不 monkeypatch 工具。它只连接一个已经启动的 Agent WebSocket 服务，像真实 PCB 前端一样发送用户消息、根据 JSONL fixture 响应 `tool-calls`、记录完整通信轨迹，并且只基于 WebSocket 可观察帧做结构化断言。

## 运行方式

先启动 Agent 服务，再运行：

```powershell
python pcb_frontend_lab/runner.py `
  --ws-url ws://127.0.0.1:7073 `
  --cases pcb_frontend_lab/cases.example.jsonl `
  --out pcb_frontend_lab/report.jsonl
```

常用参数：

```powershell
--timeout 120
--session-prefix lab
--stop-on-fail
--verbose-frames
```

含义：

- `--ws-url`：已启动 Agent 的 WebSocket 地址。
- `--cases`：JSONL 测试集路径。
- `--out`：JSONL 结果报告路径。
- `--timeout`：每轮用户输入等待 Agent 响应的超时时间，单位秒。
- `--session-prefix`：自动生成 `sessionId` 时使用的前缀。
- `--stop-on-fail`：遇到第一个失败用例后停止。
- `--verbose-frames`：运行时打印每个 WebSocket 帧摘要。

## JSONL 用例格式

每一行是一个测试用例：

```json
{
  "id": "fanout_basic_u27",
  "projectid": "proj-demo",
  "turns": ["帮我对 U27 做 BGA 逃逸布线", "arc + 北科大", "确认"],
  "tool_results": {
    "getProjectData": "F:\\fixtures\\board_u27.sexpr",
    "importLines": {"success": true, "message": "import finished"}
  },
  "expect": {
    "tool_calls": ["getProjectData", "importLines"],
    "body_fields": ["fanoutParams", "routingResult", "report"],
    "no_error": true
  }
}
```

字段说明：

- `id`：用例 ID。
- `projectid`：模拟前端传给 Agent 的当前工程 ID。
- `turns`：多轮用户输入，按顺序发送。
- `tool_results`：虚拟前端对 Agent `tool-calls` 的返回。
- `expect`：结构化断言。

`tool_results` 的 key 支持：

- 工具名：`getProjectData`
- 工具名加第几次调用：`getProjectData#2`
- 精确 call id
- `*` 兜底匹配

如果某个 `tool_results` 值是数组，重复调用同一个 key 时会按顺序消费。

## 支持的断言

- `tool_calls`：期望的工具调用序列，要求完全一致。
- `no_tool_calls`：如果 Agent 发送任何工具调用，则失败。
- `no_importLines`：如果 Agent 调用 `importLines`，则失败。
- `body_fields`：要求至少一次 Agent `message.body` 中出现这些字段。
- `absent_body_fields`：要求 Agent `message.body` 中不出现这些字段。
- `error`：`true` 表示期望出现 error 帧；`false` 表示不允许出现 error 帧。
- `no_error`：不允许出现 error 帧。
- `message_contains`：要求任意一条 Agent 消息文本包含指定字符串。
- `tool_call_arguments`：检查指定工具调用的 `arguments`。

## 报告输出

`--out` 指定的报告是 JSONL，每行对应一个用例结果，包含：

- 用例 ID
- 是否通过
- 失败原因列表
- 实际工具调用序列
- 实际观察到的 `message.body` 字段
- 最后一条 Agent 帧摘要
- `transcript_path`，指向该用例完整 WebSocket 通信轨迹文件
- 内联 `transcript`，方便脚本直接处理

同时会在报告文件旁生成一个 `<报告名>_transcripts/` 目录，保存每个用例的完整帧记录。

## 适合测试什么

- 意图识别的外部效果：哪些输入会触发 PCB 流程，哪些只应该聊天。
- WebSocket 协议兼容性：`type`、`sessionId`、`projectid`、tool-call id 配对、`body.role` 等。
- 工具调用顺序：例如 `getProjectData`、`deleteTracesForRerouting`、`importLines` 是否按预期出现。
- 前端结构化字段：`selection`、`fanoutParams`、`routingResult`、`report`、`rerouteResult` 是否出现在 `message.body` 顶层。
- 多轮流程：BGA 选择、算法选择、参数确认、中途聊天、取消、fanout/reroute 切换。

## 不测试什么

- 不测试 Agent 内部私有状态或私有函数。
- 不测试真实 QML UI。
- 不启动 PCB_Builder。
- 不验证真实 Pdsl 接口或 EDA 内核导入效果。
- 默认不负责启动 Agent 服务。
