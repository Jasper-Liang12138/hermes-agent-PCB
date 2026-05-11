# 拆线重布 Mock 客户端测试指南

本文档说明如何在 WSL 环境中执行 mock 客户端，测试当前拆线重布流程：

```text
getSelectedElements(PFindType=TRACES)
  -> deleteTracesById(ids)
  -> getProjectData()
  -> reroute()
  -> 返回 rerouteResult
```

## 前置条件

在 WSL 中进入项目目录：

```bash
cd /mnt/e/Program/hermes-agent-PCB
```

确认系统级 Python 已有 pytest/aiohttp 等依赖：

```bash
python3 -m pytest --version
python3 -c "import aiohttp; print(aiohttp.__version__)"
```

## 推荐方式：一条命令跑完整闭环

使用项目内的闭环 harness。它会自动：

1. 启动本地 WebSocketAdapter。
2. 启动 mock client 连接 WebSocket。
3. 发送一条“拆线重布”用户消息。
4. mock 前端依次响应：
   - `getSelectedElements`
   - `deleteTracesById`
   - `getProjectData`
5. 执行 `reroute()` 和 mock DRC 迭代。
6. 把完整交互写入 JSONL 文件。

执行：

```bash
python3 test_client/reroute_drc_flow_harness.py \
  --log-file test_client/reroute_selected_trace_flow_review.jsonl \
  --timeout 120 \
  --connect-retries 20 \
  --connect-retry-delay 0.2
```

预期结果：

```text
[recv tool-calls] getSelectedElements
[send tool-results] {"ids": ["2386476278", "3424247826"]}
[recv tool-calls] deleteTracesById
[send tool-results] "已成功删除"
[recv tool-calls] getProjectData
[send tool-results] "(kicad_pcb ...)"
[done] 收到 rerouteResult，重布线流程闭环完成。
```

> 注：如果 PowerShell 显示中文乱码，以 JSONL 文件内容为准。文件按 UTF-8 写入。

## 查看交互日志

完整 WebSocket 与服务端内部事件在：

```text
test_client/reroute_selected_trace_flow_review.jsonl
```

快速查看事件顺序：

```bash
python3 - <<'PY'
import json
from pathlib import Path

path = Path("test_client/reroute_selected_trace_flow_review.jsonl")
for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
    item = json.loads(line)
    payload = item.get("payload") or {}
    content = ((payload.get("body") or {}).get("content") or {}) if isinstance(payload.get("body"), dict) else {}
    tool = content.get("name") if isinstance(content, dict) else ""
    print(f"{index}. {item.get('side')} {item.get('direction')} {item.get('label')} {tool or ''}")
PY
```

期望顺序：

```text
1. client send message
2. server recv user_message
3. client recv message
4. client recv tool-calls getSelectedElements
5. client send tool-results
6. client recv tool-calls deleteTracesById
7. client send tool-results
8. client recv tool-calls getProjectData
9. client send tool-results
10. server internal drop_net_result
11. server internal reroute_generation_prompt
12. server internal mock_model_generate
13. server internal reroute_generation_prompt
14. server internal mock_model_generate
15. server internal drc_attempts_parsed
16. server internal reroute_result
17. server send final_message_fields
18. client recv message
```

## 修改 mock 框选 trace id

`reroute_drc_flow_harness.py` 调用 `test_client/reroute_mock_client.py`，默认 mock 框选 id 是：

```text
2386476278,3424247826
```

如果只想单独运行 mock client，可以通过参数指定：

```bash
python3 test_client/reroute_mock_client.py \
  --selected-trace-ids 2386476278,3424247826 \
  --log-file test_client/reroute_selected_trace_client_only.jsonl
```

单独运行 mock client 前，需要已有 WebSocket gateway 在监听默认端口 `8765`。

## 验证 40 条限制

当前工具层已覆盖“超过 40 条停止执行”。推荐用 pytest 验证：

```bash
python3 -m pytest tests/tools/test_pcb_tools_mode_guard.py -q
```

其中包含：

- 空选择停止。
- 非 JSON 格式选择结果停止。
- 41 条 trace id 停止，且不会调用 `deleteTracesById`。
- 1 到 40 条才进入删除与 `getProjectData`。

## 相关输出文件

完整测试报告：

```text
docs/pcb_reroute_selected_trace_test_report.md
```

闭环交互日志：

```text
test_client/reroute_selected_trace_flow_review.jsonl
```

闭环控制台输出：

```text
test_client/reroute_selected_trace_harness.log
```

工具层测试输出：

```text
test_client/reroute_selected_trace_pytest_tools.log
```

Gateway/toolset 回归输出：

```text
test_client/reroute_selected_trace_pytest_gateway_toolsets.log
```
