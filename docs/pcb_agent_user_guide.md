# PCB Agent 使用指南

更新时间：2026-05-13

本文说明当前 PCB Agent 的使用方式。当前智能体支持两条独立链路：

1. BGA 扇出布线：选择 BGA，选择 `arc` 或 `135` 布线器，生成扇出参数，确认后执行布线。
2. 选中走线拆线重布：用户在 PCB 前端框选走线，Agent 删除选中走线，刷新版图数据，生成局部重布结果。

两条链路不要混用。`arc` 和 `135` 只用于 BGA 扇出布线；拆线重布不要求用户选择 `arc` 或 `135`。

## 运行方式

源码运行时使用仓库根目录：

```powershell
cd F:\doctor\hermes-agent\hermes-agent-PCB
.\.venv311\Scripts\python.exe delivery_gateway_main.py
```

交付包运行时使用 delivery 目录：

```powershell
cd F:\PCB_QYF\PCB_Builder\cust_tools\PCBCopilot_dev\PCB-AGENT
.\start.bat
```

默认 WebSocket 端口以 `config.ini` 为准。当前 delivery 常用端口是 `7073`，源码 demo 默认端口可能是 `8765`。前端必须连接到同一个端口。

## 配置项

主要配置文件：

- 源码版：`F:\doctor\hermes-agent\hermes-agent-PCB\config.ini`
- delivery 版：`F:\PCB_QYF\PCB_Builder\cust_tools\PCBCopilot_dev\PCB-AGENT\config.ini`

关键配置：

```ini
[model]
api_key  =
model    =
base_url =
board_data_use_file_path = 1

[pcb]
use_long_context_module = true

[router]
work_dir =
arc_dir =
135_dir =

[server]
host = 0.0.0.0
port = 7073
```

说明：

- `board_data_use_file_path = 1` 表示前端的 `getProjectData` 可以返回版图数据文件路径，Agent 会读取文件内容。
- `arc_dir` 和 `135_dir` 是 BGA 扇出布线器目录。拆线重布链路不使用这两个布线器。
- delivery 包中通常配置为相对路径：`routers\arc` 和 `routers\135`。

## 链路一：BGA 扇出布线

适用场景：

- “帮我做 BGA 逃逸布线”
- “对 U22 做 BGA fanout”
- “用 135 给 U22 扇出”
- “用 arc 走线”

标准流程：

1. 用户发起 BGA 扇出布线请求。
2. Agent 调用前端 `getProjectData` 获取当前版图数据。
3. Agent 从版图中识别 BGA 列表。
4. 如果有多个 BGA，Agent 返回选择列表，用户选择目标 BGA。
5. Agent 要求用户选择布线器：`arc` 或 `135`。
6. Agent 生成 `fanoutParams`，展示给用户确认。
7. 用户回复“确认”后，Agent 在本地调用对应布线器。
8. 布线器输出 `routing_input.txt`。
9. Agent 通过 WebSocket 返回 `routingResult`，其值是 `routing_input.txt` 的绝对路径。
10. 前端/PCB Builder 根据该路径导入布线结果。

推荐用户话术：

```text
帮我做 BGA 逃逸布线
```

如果 Agent 返回多个 BGA：

```text
选择 U22
```

选择布线器：

```text
135
```

确认执行：

```text
确认
```

注意：

- 当前真实可选布线器只有两个：`arc` 和 `135`。
- 不存在第三个 `router.exe` 或 `pcb_fanout`。
- `routingResult` 是文件路径，不是大段 S-expression 内容。
- 如果前端看到 `routingResult` 但版图没有变化，需要检查前端是否导入该路径、PCB Builder 是否处于 busy 状态、导入目录是否有权限。

## 链路二：选中走线拆线重布

适用场景：

- “把我框选的走线拆掉后重布”
- “拆线重布”
- “reroute selected traces”
- “对选中的几根线 delete 后重新走”
- “局部拆线重布”

标准流程：

1. 用户先在 PCB 前端框选要拆线重布的走线。
2. 用户向 Agent 发送拆线重布请求。
3. Agent 识别为 `pcb_reroute_selected`，加载 `hardware/pcb-reroute`。
4. Agent 调用前端 `getSelectedElements(PFindType="TRACES")` 获取选中走线 ID。
5. 如果没有选中走线，Agent 提示用户先框选。
6. 如果选中超过 40 条，Agent 提示减少选择范围。
7. Agent 调用前端 `deleteTracesById(ids)` 删除选中走线。
8. Agent 调用 `getProjectData` 获取删除后的新版图数据。
9. Agent 生成局部重布结果和检查报告。
10. Agent 返回 `rerouteResult`、`checkReport`、`explanation`，如果生成了回填文件，还会返回 `routedBoardDataFilePath`。

推荐用户话术：

```text
拆线重布
```

或者：

```text
把我框选的走线拆掉后重布
```

注意：

- 拆线目标必须来自前端选中结果，Agent 不会只根据自然语言中的 net 名称删除走线。
- 拆线重布不走 `route` 工具，也不调用 `arc` / `135` BGA 布线器。
- 如果前面刚做过 BGA 扇出，用户再说“拆线重布”，当前逻辑会打断旧的 fanout 确认状态，切换到 reroute。
- 前端需要支持 `getSelectedElements`、`deleteTracesById` 和 `getProjectData`。

## 意图识别规则

普通聊天不会调用 PCB 工具。例如：

```text
BGA 和 QFP 有什么区别？
```

会被当成普通问答。

BGA 扇出布线需要有明确执行意图和 PCB 领域词。例如：

```text
帮我做 BGA 逃逸布线
开始 PCB 布线
对 U22 扇出
```

拆线重布关键词包括：

```text
拆线
重布
重新布
重走
reroute
ripup
rip-up
删除 net
删 net
```

如果当前已经处于 PCB 会话中，用户只说“拆线重布”也会进入 reroute 链路。

## 常见问题

### 1. 为什么提示 135 adapter 不可用？

这通常说明当前请求被识别成 BGA 扇出布线，并尝试调用 `135` 布线器，但运行环境没有找到对应 adapter 或目录配置错误。

如果你本来想做拆线重布，应使用“拆线重布”“把框选走线拆掉后重布”等表达，并先在前端框选走线。

### 2. 布线完成但版图没变化怎么办？

先确认 Agent 是否返回了：

```json
{"routingResult": "F:\\...\\routing_input.txt"}
```

如果返回了，说明 Agent 和布线器侧已经生成输出。后续要看前端/PCB Builder 是否成功导入该文件路径。

### 3. 拆线重布没有动作怎么办？

检查三点：

- 前端是否已经框选了走线。
- `getSelectedElements(PFindType="TRACES")` 是否返回非空 ID。
- `deleteTracesById(ids)` 是否成功。

### 4. 两条链路的最终输出有什么区别？

BGA 扇出布线输出：

```json
{
  "routingResult": "F:\\...\\router_work\\routing_input.txt"
}
```

拆线重布输出：

```json
{
  "rerouteResult": {...},
  "routedBoardDataFilePath": "F:\\...\\.hermes_reroute\\xxx.kicad_pcb",
  "checkReport": {...},
  "explanation": "..."
}
```

## 建议测试顺序

1. 启动 Agent，确认端口监听。
2. 前端连接 WebSocket。
3. 先测普通聊天，确认不会误调用工具。
4. 测 BGA 扇出链路：`getProjectData` → `selection` → `fanoutParams` → `routingResult`。
5. 测拆线重布链路：框选走线 → `getSelectedElements` → `deleteTracesById` → `getProjectData` → `rerouteResult`。
6. 查看 trace 日志确认字段是否下发：

```powershell
Get-Content .\logs\pcb_websocket_trace.jsonl -Tail 20
```

