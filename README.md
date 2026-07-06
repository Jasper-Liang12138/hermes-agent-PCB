# PCB Agent LangGraph

基于 LangGraph 重构的 PCB 智能体，目标是替代 Hermes Agent + SWSD 的流程控制层，同时保留已验证的前端 WebSocket 协议、PCB 工具调用方式和外部布线工具边界。

## 能力范围

- PCB 工程问答
- Global Fanout 工作流
- 局部拆线重布 Reroute 工作流
- WebSocket 协议兼容 `message` / `tool-calls` / `tool-results` / `error`
- 统一 `PCBState`，节点之间只通过 State 交换信息
- Live Evaluation 入口，直接调用正式 LangGraph Agent
- Trace Logging 与 Replay 数据结构预留

## 运行

```powershell
cd F:\doctor\hermes-agent\PCB_AGENT_LangGraph
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m pcb_agent_langgraph.websocket.server
```

默认读取当前项目目录下的 `config.ini`。如需显式指定其它配置文件：

```powershell
.\.venv\Scripts\python.exe -m pcb_agent_langgraph.websocket.server --config .\config.ini
```

## 模型调用

系统只使用一个模型，运行时统一命名为 `pcb-model`。

模型参数从本项目 `config.ini` 的 `[reroute-model]` 读取：

```ini
[reroute-model]
api_key = <your-api-key>
model = <your-model-id>
base_url = <openai-compatible-base-url>
max_tokens = 65536
```

模型接口采用 OpenAI-compatible `/chat/completions`：

```text
POST {base_url}/chat/completions
Authorization: Bearer {api_key}
model: {model}
```

可复制 `config.example.ini` 为 `config.ini` 后填入真实模型配置。本项目默认只读取 `PCB_AGENT_LangGraph\config.ini`；也可以通过 `--config` 显式指定配置文件。不会自动回退到旧 Hermes 目录。

## Live Evaluation

真实/普通评测：

```powershell
.\.venv\Scripts\python.exe -m pcb_agent_langgraph.evaluation.runner .\eval_dataset_live.json --config .\config.ini --output-dir eval_runs\live
```

模拟端到端评测，使用正式 LangGraph Agent，但模拟 EDA 前端和外部 PCB 工具返回：

```powershell
.\.venv\Scripts\python.exe -m pcb_agent_langgraph.evaluation.runner .\eval_dataset_sim_e2e.json --config .\config.ini --output-dir eval_runs\sim_e2e --simulate-tools
```

## 目录

```text
pcb_agent_langgraph/
  graph/          LangGraph State、节点和图构建
  planner/        意图识别和工具规划
  models/         pcb-model 统一封装
  tools/          LangChain Tool 风格工具封装
  websocket/      EDA 前端 WebSocket 协议适配
  evaluation/     Live Evaluation、Trace、Replay、Report
  utils/          配置、日志、JSON 工具
tests/
```

## 配置

外部程序路径读取 `config.ini`：

- `[router] work_dir`
- `[router] arc_dir`
- `[router] 135_dir`
- `[router] rl_root_dir`

若真实可执行程序未配置，工具会返回明确失败状态，不会伪造成功结果。


## 真实工具资产

### DRC / AI-PCB-Eval

`vendor/AI-PCB-Eval` 是独立 DRC 与 PCB 评测工具链，不是旧 Hermes Agent 流程代码。当前 LangGraph 项目通过 `tools/pcb_reroute_drc.py` 调用其中的：

- `patch_kicad_from_raw_standalone.py`：把模型/布线器输出回填为完整 `.kicad_pcb`
- `drc_backend/api.py`：执行 hard-rule DRC 检查并返回结构化结果

配置入口：

```ini
[drc]
enabled = 1
tool_path = .\tools\pcb_reroute_drc.py
eval_root = .\vendor\AI-PCB-Eval
```

### 可解释性模型

可解释性模型资产放在：

```text
explain_model/
  model/best.pt
  explain_code/
```

可解释性模型需要一个带 `torch/torchvision/pillow/numpy/matplotlib` 的 Python runtime。`python_executable` 可以临时指向本机已有环境，但不要把个人机器路径当成交付默认值：

```ini
[explain_model]
enabled = 1
# 示例路径，仅限本机；远端开发者需要改成自己的 runtime，或用下面脚本构建项目内 runtime。
python_executable = F:\PCB_QYF\PCB_Builder\cust_tools\PCBCopilot_dev\PCB-AGENT\python_runtime\python.exe
code_dir = .\explain_model\explain_code
checkpoint_path = .\explain_model\model\best.pt
```

封装 exe 或离线交付时，建议把 runtime 构建到项目目录。两种方式：

方式一：从已有 runtime 复制。`-SourceRuntime` 是机器本地路径，GitHub 拉取代码的人员必须改成自己的路径。

```powershell
.\scripts\build_explain_runtime.ps1 `
  -SourceRuntime <你的可解释性模型Python运行环境路径> `
  -TargetRuntime .\runtime\explain_python `
  -Force
```

方式二：从源码创建 venv 并安装 `requirements-explain.txt`。这是远端开发者更可移植的方式，但可能需要网络或本地 pip wheel 源。

```powershell
.\scripts\build_explain_runtime.ps1 `
  -CreateVenv `
  -Python python `
  -TargetRuntime .\runtime\explain_python `
  -Force
```

构建完成后在 `config.ini` 中使用项目内 runtime：

```ini
[explain_model]
python_executable = .\runtime\explain_python\python.exe
```

