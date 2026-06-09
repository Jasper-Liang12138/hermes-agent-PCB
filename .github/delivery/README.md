# PCB-AGENT 交付包说明

这是 PCB Copilot 前端连接的 Agent 交付目录。请保持目录结构不变，直接在本目录启动。

## 目录结构

```text
PCB-AGENT
├─ agent.exe              # Agent 主程序
├─ _internal              # agent.exe 运行依赖
├─ config.ini             # 模型、端口、布线器路径配置
├─ routers                # 135/arc/RL 布线器
├─ python_runtime         # RL 模块专用 Python 运行环境
├─ model                  # 可选模型/权重目录
├─ memories               # 意图识别经验记忆
├─ skills                 # PCB skill
├─ logs                   # 运行日志，可清空内容
├─ router_work            # 布线临时工作目录，可清空内容
├─ start.bat              # 启动 Agent
├─ stop-agent-api.bat     # 停止 Agent
└─ README.md              # 本说明
```

## 使用方法

1. 修改 `config.ini`

确认模型配置、WebSocket 端口、布线器路径正确。常用端口配置在：

```ini
[websocket]
port = 7074
```

RL 运行环境配置：

```ini
[router]
rl_python = .\python_runtime\python.exe
rl_device = cpu
```

2. 启动 Agent

双击：

```text
start.bat
```

或在当前目录执行：

```powershell
.\start.bat
```

3. 停止 Agent

双击：

```text
stop-agent-api.bat
```

或执行：

```powershell
.\stop-agent-api.bat
```

4. 前端连接

前端连接本机 WebSocket：

```text
ws://127.0.0.1:7074
```

端口以 `config.ini` 为准。

## 日志和临时文件

下面两个目录是运行产物，可以在停止 Agent 后清空里面的内容：

```text
logs
router_work
```

不要删除目录本身。

## RL 模块说明

选择 `135 + RL` 或 `arc + RL` 时会使用：

```text
python_runtime\python.exe
```

该 Python 需要能导入：

```text
numpy
torch
```

验证命令：

```powershell
.\python_runtime\python.exe -c "import numpy, torch; print(numpy.__version__); print(torch.__version__)"
```

如果缺少 `python_runtime` 或其中没有 `torch/numpy`，普通非 RL 功能可能可用，但 RL 层分配和逃逸顺序搜索会失败。
