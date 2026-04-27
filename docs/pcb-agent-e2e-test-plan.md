# PCB Agent 端到端测试规范

本文档用于规范 `dist/agent` 打包产物的 WebSocket + PCB 智能体链路测试，避免测试 prompt、环境变量、mock 回包不一致导致结果不可复现。

## 测试目标

验证从 PCB 前端 WebSocket 消息到 Agent 工具调用、结构化字段返回、用户确认、布线结果返回的完整链路：

1. WebSocket 服务可启动并监听端口。
2. 普通聊天不会误触发 PCB 工具。
3. PCB 操作请求会进入 PCB 主链路。
4. Agent 首轮应调用 `getProjectData`。
5. 前端回包版图数据后，Agent 应返回 `selection`。
6. 用户选择 BGA 后，Agent 应返回 `fanoutParams`。
7. 用户确认后，Agent 应调用 `route`。
8. 布线完成后，Agent 应返回 `routingResult`。

## 测试环境

默认打包目录：

```powershell
F:\doctor\hermes-agent\hermes-agent-PCB\dist\agent
```

启动命令：

```powershell
cd /d F:\doctor\hermes-agent\hermes-agent-PCB\dist\agent
agent.exe --gateway
```

客户端命令：

```powershell
cd /d F:\doctor\hermes-agent\hermes-agent-PCB\dist\agent
test-client.exe --port 8765 --project-id proj-test
```

自动化或本地联调时建议显式设置：

```powershell
$env:GATEWAY_ALLOW_ALL_USERS = "true"
```
测试使用的版图信息样例：F:\doctor\hermes-agent\608Pin_10BGA_12L_SD_01121724.txt

否则未配置 allowlist 时，网关会拒绝 WebSocket 用户消息。

## 配置检查

测试前必须确认以下配置一致：

1. 外层配置：`dist/agent/config.ini`
2. PyInstaller 内部配置：`dist/agent/_internal/config.ini`

重点字段：

```ini
[model]
api_key  = <真实 key>
model    = <目标模型>
base_url = <OpenAI-compatible endpoint>
board_data_use_file_path = 0

[router]
cmd      = router.exe
work_dir = .

[server]
host = 0.0.0.0
port = 8765
```

注意：如果 `start-gateway.bat` 检查的是外层 `config.ini`，但 `agent.exe` 实际读取 `_internal/config.ini`，会造成“脚本不让启动 / exe 实际配置不同”的问题。

## 本次实际测试 Prompt

以下是本次人工自动化验证中实际使用过的 prompt。

### 普通聊天探针

用于确认普通聊天不应触发 PCB 工具：

```text
你好，告诉我什么是BGA逃逸布线？
```

期望：

- 返回普通 `message`
- 不出现 `tool-calls`
- 不包含 `selection` / `fanoutParams` / `routingResult`

### PCB 主链路 Prompt 1

```text
帮我进行 BGA 逃逸布线。
```

期望：

- 路由为 PCB 模式
- 自动注入 `hardware/pcb-intelligence` skill
- 首轮收到 `tool-calls`
- 工具名为 `getProjectData`

实际结果：

- 未收到 `tool-calls`
- Agent 返回了 BGA 原理说明类内容
- 主链路未进入

### PCB 主链路 Prompt 2

```text
请立即执行 PCB BGA 逃逸布线操作：先调用 getProjectData 获取当前版图数据，再提取 BGA 列表并通过 selection 返回。不要解释 BGA 原理。
```

期望：

- 首轮强制调用 `getProjectData`
- 不输出说明文档

实际结果：

- 未收到 `tool-calls`
- Agent 返回了 PCB BGA 路由流程说明
- 主链路未进入

风险点：

- 该 prompt 包含“解释”“原理”“说明”等词，可能命中 WebSocket 路由中的聊天意图规则。
- 后续标准测试不要在操作 prompt 中加入“不要解释”“不要说明”“原理”等否定短语，因为简单关键词路由可能不会理解否定语义。

### 后续轮次 Prompt

收到 `selection` 后：

```text
选择 FPGA1
```

或：

```text
选择 U27
```

收到 `fanoutParams` 后：

```text
确认，开始布线。
```

## 标准化测试 Prompt

后续建议固定使用以下 prompt，避免混入聊天意图关键词。

### T1 普通聊天

```text
今天星期几？
```

期望：

- 不触发 `getProjectData`
- 不进入 PCB 模式
- 返回普通文本

### T2 概念咨询

```text
BGA 和 QFP 有什么区别？
```

期望：

- 不触发 PCB 工具
- 返回概念解释

### T3 PCB 主链路入口

```text
开始 PCB BGA 逃逸布线，获取当前版图数据并返回可选 BGA 列表。
```

期望：

- 首轮收到 `tool-calls`
- `body.content.name == "getProjectData"`
- `body.content.arguments` 可为空或包含兼容字段 `projectID`

禁止在该 prompt 中加入：

- `解释`
- `说明`
- `原理`
- `是什么`
- `区别`
- `不要解释`
- `不要说明`

### T4 选择阶段

```text
选择 FPGA1
```

期望：

- Agent 接受 selection 列表中的非 `U+数字` 位号
- 返回结构化字段 `fanoutParams`

### T5 确认布线

```text
确认，开始布线。
```

期望：

- 收到 `tool-calls`
- `body.content.name == "route"`
- `body.content.arguments.userData` 是合法 JSON 字符串
- 最终返回结构化字段 `routingResult`

### T6 选择阶段误确认

前置：已收到 `selection`，但尚未选择器件。

```text
确认
```

期望：

- 不进入 route
- 返回纠偏提示，例如“当前还在选择阶段，请先回复器件”

### T7 取消流程

```text
取消
```

期望：

- 清理 PCB flow state
- 会话回到普通聊天模式

## Mock 工具回包

### getProjectData

```text
(pcb_data (project "proj-e2e")
  (component (name "FPGA1") (package "BGA-1156") (pins 1156) (pitch "1.0mm"))
  (component (name "U27") (package "BGA-256") (pins 256) (pitch "0.8mm"))
  (stackup
    (layer (name "SIG03") (type "signal"))
    (layer (name "SIG04") (type "signal")))
  (net (name "GND") (pins "FPGA1-A1" "FPGA1-B1"))
  (net (name "VCC") (pins "FPGA1-A2" "FPGA1-B2"))
  (net (name "DDR_D0") (pins "FPGA1-C1" "U27-A1")))
```

### GetSelectedElements

```json
{"ids": []}
```

### route

当前实现中 `route` 是 Agent 本地调用 `router.exe`，不是前端 WebSocket 工具回包主路径。若需要在纯协议 adapter 测试中模拟 route 回包，可使用：

```json
{
  "routingResult": "(routes (route (net \"GND\") (layer \"SIG03\") (status ok)))",
  "report": "布线连通率: 100%"
}
```

若测试真实打包 exe 的本地 `route`，需要提供可执行 `router.exe`，并确保输出：

- `routing_input.txt`
- `data.txt`

## 判定标准

### 通过

完整 PCB 链路通过必须同时满足：

1. 首轮 PCB prompt 收到 `getProjectData`。
2. `getProjectData` mock 回包后收到 `selection`。
3. `选择 FPGA1` 后收到 `fanoutParams`。
4. `确认，开始布线。` 后触发 `route` 或本地 router 执行。
5. 最终消息包含 `routingResult`。
6. 普通聊天和概念咨询不会触发 PCB 工具。

### 失败

出现以下任一情况即判定失败：

1. PCB 操作 prompt 只返回解释说明，没有 `tool-calls`。
2. 普通聊天触发 `getProjectData`。
3. selection 阶段不能接受 `FPGA1` 这类非 `U+数字` 位号。
4. 未选择器件时回复“确认”却直接进入 route。
5. route 完成后没有返回 `routingResult` 字段。

## 本次测试结论

本次针对新 `dist/agent` 包的测试结论：

1. `agent.exe --gateway` 可启动，WebSocket 可连接。
2. `GATEWAY_ALLOW_ALL_USERS=true` 是本地自动化测试的必要条件。
3. PCB prompt 未触发 `getProjectData`，Agent 返回了说明性文本。
4. 因未出现 `tool-calls`，完整链路未进入 `selection -> fanoutParams -> route -> routingResult`。
5. 后续应优先修正“PCB 操作 prompt 必须触发工具调用”的问题，再跑完整链路。

