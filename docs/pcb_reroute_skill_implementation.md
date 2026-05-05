# PCB 局部拆线重布 Skill 实施说明

## 目标

基于 `docs/design_doc_v1.3.2.md` 3.5.3 的局部拆线重布流程，在 Hermes 框架内实现一条精简闭环：

- 不调用 `GetSelectedElements`；
- 从用户文本中提取需要拆线重布的 net；
- 通过 WebSocket 请求 EDA/客户端执行 mock 拆线；
- 客户端返回拆线后的版图数据或版图数据文件路径；
- Hermes 读取拆线后版图，复用 PCB 分块上下文能力；
- `reroute` 优先调用配置的大模型生成局部重布结果；
- 模型不可用时回退到结构化结果包；
- 最终通过 WebSocket 返回 `rerouteResult`、`checkReport`、`explanation`。

## 最终实现

### Skill

新增 skill：

```text
skills/hardware/pcb-reroute/SKILL.md
```

该 skill 定义局部拆线重布流程：

1. 判断用户是否明确要求对指定 net 执行拆线重布、删除后重走、重新布线、reroute。
2. 调用 `drop_net(userText, projectID)`。
3. 如果 `drop_net` 返回错误或未识别到 net，要求用户明确写出 net 名称。
4. 调用 `reroute()`，优先使用 `drop_net` 写入的 session 缓存。
5. 将 `rerouteResult`、`checkReport`、`explanation` 放入 `##PCB_FIELDS##` 结构化返回。

### 工具

核心工具位于：

```text
tools/pcb_tools.py
```

新增/扩展内容：

- `extract_reroute_nets(user_text)`：从用户文本中提取 `net13`、`NET_A1` 等 net 名称。
- `drop_net(userText, projectID)`：
  - 只在 PCB mode 下执行；
  - 从用户文本提取 net；
  - 通过 WebSocket 向客户端发送 `drop_net_mock`；
  - 支持客户端返回 `droppedBoardData`；
  - 支持客户端返回 `droppedBoardDataFilePath`，工具会读取文件内容并缓存为 `droppedBoardData`。
- `reroute(userData="")`：
  - 优先读取 `drop_net` 缓存；
  - 支持从 `userData` 显式传入 `selectedNets`、`droppedBoardData`、`droppedBoardDataFilePath`、`droppedObjects`、`localContext`、`constraints`；
  - 有拆线后版图文本时，复用 `tools/pcb_chunking_tool.py` 的 `_build_board_context()`；
  - 配置的大模型可用时调用大模型生成 `rerouteResult/checkReport/explanation`；
  - 分块模块、模型配置或模型调用失败时，回退到结构化 fallback 结果。

### Toolset

`drop_net` 和 `reroute` 已加入 PCB WebSocket toolset：

```text
toolsets.py
```

涉及 toolset：

- `hermes-websocket`
- `hermes-websocket-pcb`

### WebSocket 字段

WebSocket adapter 已支持透传以下结构化字段：

```text
gateway/platforms/websocket.py
```

字段：

- `rerouteResult`
- `checkReport`
- `explanation`

当最终回复中包含 `##PCB_FIELDS## ... ##PCB_FIELDS_END##` 时，adapter 会解析其中 JSON，并把字段提升到 WebSocket message 的 `body` 中返回给客户端。

## 触发机制

### 自动触发

WebSocket 平台收到用户消息后，会先进行意图识别。

当前策略是：

1. 安全硬规则优先：取消、中止、只解释、不执行等请求不会进入拆线重布流程。
2. 模型意图识别优先：辅助模型高置信输出 `pcb_reroute_selected` 时，触发 `pcb-reroute`。
3. 关键词兜底：当模型不可用、无有效输出或置信度不足时，才使用关键词判断。

模型期望输出示例：

```json
{
  "intent": "pcb_reroute_selected",
  "route_mode": "pcb",
  "confidence": 0.88,
  "should_call_get_project_data": false
}
```

关键词兜底需要同时满足两类词：

- 局部重布动作词：`拆线`、`删除 net`、`重布`、`重新布线`、`重走`、`reroute`、`ripup`；
- PCB 领域词：`pcb`、`版图`、`bga`、`布线`、`走线`、`net`、`route`。

触发后 gateway 会：

1. 切换 session 到 `pcb` mode；
2. 启用 `hermes-websocket-pcb` toolset；
3. 自动加载 `hardware/pcb-reroute` skill；
4. 不触发 `getProjectData` bootstrap；
5. 由 agent 按 skill 调用 `drop_net` 和 `reroute`。

### 手动触发

支持 skill 命令的入口也可以显式使用：

```text
/pcb-reroute 请帮我把 net13、net17 拆线后重新布线
```

## 执行流程

整体链路如下：

```text
客户端发送用户 JSON message
  -> gateway 做模型意图识别
  -> 判定为 pcb_reroute_selected
  -> 自动加载 hardware/pcb-reroute skill
  -> agent 调用 drop_net
  -> drop_net 通过 WebSocket 请求客户端 drop_net_mock
  -> 客户端返回 droppedBoardData 或 droppedBoardDataFilePath
  -> drop_net 读取并缓存拆线后版图数据
  -> agent 调用 reroute
  -> reroute 对拆线后版图分块
  -> reroute 调用配置的大模型生成局部重布 JSON
  -> 失败时回退到结构化 fallback 结果
  -> agent 用 ##PCB_FIELDS## 返回结构化字段
  -> gateway 把 rerouteResult/checkReport/explanation 放入 WebSocket body
```

## `reroute` 内部机制

`reroute` 不是客户端工具，也不调用外部全局布线器。

它的内部机制是：

1. 检查当前 session 是否为 PCB mode。
2. 解析可选 `userData`。
3. 如果 `userData` 为空，则读取 `drop_net` 缓存。
4. 获取：
   - `selectedNets`
   - `droppedBoardData`
   - `droppedBoardDataFilePath`
   - `droppedObjects`
   - `localContext`
   - `constraints`
5. 如果只有 `droppedBoardDataFilePath`，则读取该文件内容。
6. 构造基础 `checkReport`。
7. 调用 `_generate_reroute_with_model(...)`。

模型生成流程：

1. 复用 `tools.pcb_chunking_tool`。
2. 读取当前模型配置，例如 `deepseek-chat` + `https://api.deepseek.com`。
3. 创建 OpenAI-compatible chat adapter。
4. 调用 `_build_board_context(droppedBoardData, token_counter=...)` 构造分块上下文。
5. 构造专用 system prompt 和 user prompt。
6. 要求模型只输出合法 JSON：

```json
{
  "rerouteResult": {
    "type": "local_reroute",
    "mode": "selected_nets_after_drop",
    "selectedNets": [],
    "operations": []
  },
  "checkReport": {
    "passed": true,
    "checks": []
  },
  "explanation": "简短中文说明"
}
```

7. 从模型输出中抽取第一个 JSON object。
8. 将模型结果与 fallback payload 合并。

如果模型生成不可用，fallback 会返回意图级 operations，例如：

```json
{
  "action": "reroute_net",
  "net": "net13",
  "scope": "local",
  "preserveOtherNets": true
}
```

因此当前实现是“模型优先 + 结构化 fallback”。它验证的是流程闭环和接口结构，不等同于真实几何布线器或 DRC 求解器。

## WebSocket JSON 示例

以下为 DeepSeek 源闭环测试中的关键 JSON object。WebSocket text 载荷均为 JSON object。中间流式增量帧较多，本文只保留代表帧。

### 1. 客户端首次发送用户消息

```json
{
  "sessionId": "reroute-mock-6104bf3b",
  "projectid": "proj-reroute-mock",
  "type": "message",
  "body": {
    "role": "user",
    "content": "请帮我重布线 net13，版图数据文件地址为 /mnt/e/Program/hermes-agent-PCB/test_client/mock_reroute_board.s_expr",
    "boardDataFilePath": "/mnt/e/Program/hermes-agent-PCB/test_client/mock_reroute_board.s_expr"
  }
}
```

### 2. 服务端确认收到

```json
{
  "sessionId": "reroute-mock-6104bf3b",
  "projectid": "proj-reroute-mock",
  "type": "message",
  "body": {
    "msgId": "528828067396",
    "role": "agent",
    "content": "已收到，正在处理...",
    "isFinal": false
  }
}
```

### 3. 服务端流式决策片段

```json
{
  "sessionId": "reroute-mock-6104bf3b",
  "projectid": "proj-reroute-mock",
  "type": "message",
  "body": {
    "msgId": "528828067396",
    "role": "agent",
    "content": "好的，我来执行局部拆线重布流程。用户明确要求对 net13 进行重布线，我调用 `drop_net` 从文本中提取 net 名称并请求 EDA 拆线。",
    "isFinal": false
  }
}
```

该帧来自流式输出。真实运行时可能出现多条 `isFinal: false` 的 message 帧，客户端可以选择展示或隐藏。

### 4. 服务端发起 `drop_net_mock`

```json
{
  "sessionId": "reroute-mock-6104bf3b",
  "projectid": "proj-reroute-mock",
  "type": "tool-calls",
  "body": {
    "role": "agent",
    "content": {
      "id": "call_76a1287b",
      "name": "drop_net_mock",
      "arguments": {
        "projectID": "proj-reroute-mock",
        "nets": [
          "net13"
        ],
        "userText": "请帮我重布线 net13，版图数据文件地址为 /mnt/e/Program/hermes-agent-PCB/test_client/mock_reroute_board.s_expr"
      }
    }
  }
}
```

### 5. 客户端返回工具结果

```json
{
  "sessionId": "reroute-mock-6104bf3b",
  "projectid": "proj-reroute-mock",
  "type": "tool-results",
  "body": {
    "role": "tool",
    "content": {
      "id": "call_76a1287b",
      "result": {
        "droppedBoardDataFilePath": "/mnt/e/Program/hermes-agent-PCB/test_client/mock_reroute_board.s_expr",
        "droppedObjects": [
          {
            "net": "net13",
            "mockRemoved": true
          }
        ],
        "localContext": {
          "source": "reroute_mock_client",
          "boardDataFilePath": "/mnt/e/Program/hermes-agent-PCB/test_client/mock_reroute_board.s_expr",
          "note": "MOCK 客户端暂时把原版图文件作为拆线后版图返回"
        }
      }
    }
  }
}
```

### 6. 服务端继续执行 `reroute`

```json
{
  "sessionId": "reroute-mock-6104bf3b",
  "projectid": "proj-reroute-mock",
  "type": "message",
  "body": {
    "msgId": "528828067396",
    "role": "agent",
    "content": "`drop_net` 已成功执行，识别到 net13 并完成拆线。现在调用 `reroute` 生成局部重布结果。",
    "isFinal": false
  }
}
```

### 7. 工具进度帧

```json
{
  "sessionId": "reroute-mock-6104bf3b",
  "projectid": "proj-reroute-mock",
  "type": "message",
  "body": {
    "msgId": "528828067396",
    "role": "agent",
    "content": "drop_net...\nreroute...",
    "isFinal": false
  }
}
```

### 8. 最终结构化结果

```json
{
  "sessionId": "reroute-mock-6104bf3b",
  "projectid": "proj-reroute-mock",
  "type": "message",
  "body": {
    "msgId": "528828067396",
    "role": "agent",
    "content": "`drop_net` 已成功执行，识别到 net13 并完成拆线。现在调用 `reroute` 生成局部重布结果。已完成局部拆线重布结果生成。",
    "isFinal": null,
    "rerouteResult": {
      "type": "local_reroute",
      "mode": "selected_nets_after_drop",
      "selectedNets": [
        "net13"
      ],
      "operations": [
        {
          "type": "add_track",
          "net": "net13",
          "layer": "F.Cu",
          "width": 0.25,
          "points": [
            {
              "x": 100.0,
              "y": 100.0
            },
            {
              "x": 110.0,
              "y": 100.0
            },
            {
              "x": 110.0,
              "y": 110.0
            }
          ]
        },
        {
          "type": "add_via",
          "net": "net13",
          "at": {
            "x": 110.0,
            "y": 110.0
          },
          "size": 0.6,
          "drill": 0.3,
          "layers": [
            "F.Cu",
            "B.Cu"
          ]
        },
        {
          "type": "add_track",
          "net": "net13",
          "layer": "B.Cu",
          "width": 0.25,
          "points": [
            {
              "x": 110.0,
              "y": 110.0
            },
            {
              "x": 120.0,
              "y": 110.0
            }
          ]
        }
      ],
      "constraints": {},
      "droppedObjects": [
        {
          "net": "net13",
          "mockRemoved": true
        }
      ],
      "localContext": {
        "source": "reroute_mock_client",
        "boardDataFilePath": "/mnt/e/Program/hermes-agent-PCB/test_client/mock_reroute_board.s_expr"
      }
    },
    "checkReport": {
      "passed": true,
      "checks": [
        {
          "name": "net_continuity",
          "status": "pass",
          "detail": "net13 已通过新增线段和过孔连通"
        },
        {
          "name": "clearance",
          "status": "pass",
          "detail": "未检测到间距冲突（基于 mock 数据）"
        },
        {
          "name": "layer_consistency",
          "status": "pass",
          "detail": "线段和过孔层设置一致"
        }
      ]
    },
    "explanation": "基于拆线后版图，为 net13 新增一条从 (100,100) 到 (120,110) 的走线，包含一个过孔换层，确保连通性。"
  }
}
```

## Mock 客户端

新增文件：

```text
test_client/reroute_mock_client.py
test_client/mock_reroute_board.s_expr
```

客户端行为：

- 连接 `ws://127.0.0.1:8765`；
- 所有发送内容均为 JSON object，再编码成 WebSocket text；
- 收到的 WebSocket text 必须能解析为 JSON object；
- 连接成功后自动发送首条用户消息；
- 收到 `tool-calls` 时打印完整 JSON；
- 收到 `drop_net_mock` 时自动返回当前 mock 版图路径作为 `droppedBoardDataFilePath`；
- 收到 `body.rerouteResult` 或 `body.routingResult` 时退出码为 `0`。

## 复现步骤

### 1. 安装项目

在 WSL 中执行：

```bash
cd /mnt/e/Program/hermes-agent-PCB
python3 -m pip install --user --break-system-packages -e '.[messaging]'
```

### 2. 配置模型

`config.ini` 保持如下配置：

```ini
[model]
api_key  =
model    = deepseek-chat
base_url = https://api.deepseek.com
board_data_use_file_path = 0
```

建议不要把 API key 写入 `config.ini`。测试时通过环境变量注入：

```bash
OPENAI_API_KEY='<your-deepseek-api-key>'
```

### 3. 启动 gateway

终端 1：

```bash
cd /mnt/e/Program/hermes-agent-PCB
OPENAI_API_KEY='<your-deepseek-api-key>' python3 -m gateway.run
```

如果希望测试结束后自动退出：

```bash
cd /mnt/e/Program/hermes-agent-PCB
OPENAI_API_KEY='<your-deepseek-api-key>' timeout 300s python3 -m gateway.run
```

### 4. 启动 mock 客户端

终端 2：

```bash
cd /mnt/e/Program/hermes-agent-PCB
python3 test_client/reroute_mock_client.py --timeout 240 --connect-retries 80 --connect-retry-delay 1
```

可选：测试多个 net。

```bash
python3 test_client/reroute_mock_client.py \
  --nets net13,net17 \
  --timeout 240 \
  --connect-retries 80 \
  --connect-retry-delay 1
```

可选：覆盖首条 prompt。

```bash
python3 test_client/reroute_mock_client.py \
  --prompt "请帮我重布线 net17，版图数据文件地址为 /mnt/e/Program/hermes-agent-PCB/test_client/mock_reroute_board.s_expr"
```

### 5. 预期结果

客户端输出中应出现：

```text
[send message]
[recv tool-calls]
[send tool-results]
[done] 收到 rerouteResult，重布线流程闭环完成。
```

客户端退出码应为 `0`。

## 测试覆盖

对应测试文件：

```text
tests/tools/test_pcb_tools_mode_guard.py
tests/gateway/test_websocket_pcb_flow.py
tests/test_toolsets.py
```

覆盖点：

- net 名称提取；
- `drop_net` 在 chat mode 下拒绝执行；
- `drop_net` 在 PCB mode 下调用客户端 `drop_net_mock`；
- `drop_net` 支持客户端返回 `droppedBoardDataFilePath` 并读取文件；
- `reroute` 使用 session 缓存生成 `rerouteResult`；
- `reroute` 可在模型可用时调用模型生成结果；
- WebSocket 透传 `rerouteResult/checkReport/explanation`；
- WebSocket 路由将拆线重布请求绑定到 `hardware/pcb-reroute`；
- LLM 意图识别优先，关键词只作为兜底。

验证命令：

```bash
cd /mnt/e/Program/hermes-agent-PCB
python3 -m pytest tests/tools/test_pcb_tools_mode_guard.py tests/gateway/test_websocket_pcb_flow.py tests/test_toolsets.py -q
```

最近一次验证结果：

```text
68 passed, 12 warnings
```

warnings 均为测试夹具中的 `asyncio.get_event_loop()` deprecation warning。

## 常见问题

- gateway 报 `MissingSectionHeaderError`：检查 `config.ini` 是否被保存成带 BOM 的 UTF-8，需要保存为无 BOM UTF-8。
- 客户端一直 `ConnectionRefusedError`：gateway 未启动成功，或端口不是 `8765`。
- 模型服务返回 401/403：`OPENAI_API_KEY` 不可用、额度不足或权限不够。
- 没有出现 `tool-calls/drop_net_mock`：模型未识别为重布线意图，检查 prompt 是否包含明确的 `重布线/reroute` 和 `net` 名称。
- 出现 `rerouteResult` 但内容较粗糙：当前仍是流程验证级模型生成，未调用真实几何布线器或 DRC 求解器。
